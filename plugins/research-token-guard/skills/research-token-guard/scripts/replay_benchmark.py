#!/usr/bin/env python3
"""Replay baseline and guarded commands without confusing byte savings with tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "research-token-guard/replay-result/v1"
CORPUS_SCHEMA = "research-token-guard/replay-corpus/v1"
USAGE_SCHEMA = "research-token-guard/provider-usage/v1"


class ReplayError(RuntimeError):
    pass


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_within(root: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    root = root.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReplayError(f"path escapes root: {value}") from exc
    return resolved


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read JSON {path}: {exc}") from exc


def emit_file(path: Path) -> int:
    sys.stdout.buffer.write(path.read_bytes())
    return 0


def emit_noisy_failure() -> int:
    for index in range(2500):
        sys.stdout.write(f"noise-line-{index:04d} value={index % 17}\n")
    sys.stderr.write("Traceback (most recent call last):\n")
    sys.stderr.write("  File \"fixture.py\", line 7, in run\n")
    sys.stderr.write("RuntimeError: exact-fixture-failure\n")
    return 7


def emit_invalid_utf8() -> int:
    sys.stdout.buffer.write(b"valid-prefix\n\xff\xfe\x80\nvalid-suffix\n")
    return 0


def emit_token_pilot() -> int:
    """Emit a large, fully synthetic decision record for provider-token A/B."""
    for index in range(2400):
        sys.stdout.write(f"synthetic-noise-{index:04d} bucket={index % 23}\n")
    sys.stdout.write("DELTA0=-0.01234 CI95=[-0.02000,-0.00400]\n")
    sys.stdout.write("DELTA2=+0.02345 CI95=[+0.01500,+0.03200]\n")
    sys.stdout.write("PROGRESSION_GATE=FAIL\n")
    sys.stdout.write("M3_TRAINED=false\n")
    sys.stdout.write("DECISION=Correct synchronization beats mismatch but not the fixed baseline.\n")
    return 0


def make_mutation(source: bytes) -> bytes:
    old = b'"status": "COMPLETE"'
    new = b'"status": "COMPLETF"'
    if old not in source:
        raise ReplayError("mutation source lacks expected status marker")
    return source.replace(old, new, 1)


def fixture_command(
    case: dict[str, Any], root: Path, script: Path, mutation_dir: Path
) -> tuple[list[str], list[Path]]:
    mode = case.get("mode")
    if mode == "file":
        path = resolve_within(root, case["path"])
        return [sys.executable, str(script), "emit-file", "--path", str(path)], [path]
    if mode == "noisy-failure":
        return [sys.executable, str(script), "emit-noisy-failure"], []
    if mode == "invalid-utf8":
        return [sys.executable, str(script), "emit-invalid-utf8"], []
    if mode == "mutation":
        source_id = case.get("source_case")
        if not isinstance(source_id, str):
            raise ReplayError(f"case {case.get('id')} has no source_case")
        source_cases = getattr(fixture_command, "case_index", {})
        source_case = source_cases.get(source_id)
        if not isinstance(source_case, dict) or source_case.get("mode") != "file":
            raise ReplayError(f"mutation source is not a file case: {source_id}")
        source_path = resolve_within(root, source_case["path"])
        mutation_path = mutation_dir / f"{case['id']}.bin"
        mutation_path.write_bytes(make_mutation(source_path.read_bytes()))
        return [sys.executable, str(script), "emit-file", "--path", str(mutation_path)], [
            mutation_path
        ]
    raise ReplayError(f"unsupported mode for {case.get('id')}: {mode}")


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, capture_output=True, check=False)


def run_text(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def required_facts(case: dict[str, Any], model_bytes: bytes) -> tuple[int, int, list[str]]:
    required = case.get("required_literals", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ReplayError(f"invalid required_literals for {case.get('id')}")
    missing = [item for item in required if item.encode("utf-8") not in model_bytes]
    return len(required) - len(missing), len(required), missing


def visible_strings(value: Any) -> list[str]:
    """Return only semantic string values present in a structured tool response."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(visible_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(visible_strings(item))
        return result
    return []


def provider_gate(usage: dict[str, Any] | None, case_ids: list[str]) -> dict[str, Any]:
    if usage is None:
        return {
            "claim": "not_measured",
            "passed": False,
            "reason": "Provider-reported per-case token usage was not supplied.",
        }
    if usage.get("schema_version") != USAGE_SCHEMA:
        raise ReplayError(f"provider usage schema must be {USAGE_SCHEMA}")
    cases = usage.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(case_ids):
        raise ReplayError("provider usage cases must exactly match replay case ids")
    baseline_total = 0
    guarded_total = 0
    reductions: list[float] = []
    normalized: dict[str, Any] = {}
    for case_id in case_ids:
        item = cases[case_id]
        if not isinstance(item, dict):
            raise ReplayError(f"provider usage case must be an object: {case_id}")
        baseline = item.get("baseline")
        guarded = item.get("guarded")
        if not isinstance(baseline, dict) or not isinstance(guarded, dict):
            raise ReplayError(f"missing baseline/guarded usage: {case_id}")
        b_new = baseline.get("new_input_tokens")
        g_new = guarded.get("new_input_tokens")
        for name, value in (("baseline", b_new), ("guarded", g_new)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ReplayError(f"{case_id} {name} new_input_tokens must be positive int")
        baseline_total += b_new
        guarded_total += g_new
        reduction = (b_new - g_new) * 100.0 / b_new
        reductions.append(reduction)
        normalized[case_id] = {
            "baseline_new_input_tokens": b_new,
            "guarded_new_input_tokens": g_new,
            "reduction_percent": round(reduction, 2),
            "baseline_cached_input_tokens": int(baseline.get("cached_input_tokens", 0)),
            "guarded_cached_input_tokens": int(guarded.get("cached_input_tokens", 0)),
        }
    aggregate_reduction = (baseline_total - guarded_total) * 100.0 / baseline_total
    median_reduction = statistics.median(reductions)
    passed = aggregate_reduction >= 30.0 and median_reduction >= 40.0
    return {
        "claim": "provider_measured",
        "passed": passed,
        "aggregate_new_input_reduction_percent": round(aggregate_reduction, 2),
        "median_case_new_input_reduction_percent": round(median_reduction, 2),
        "baseline_new_input_tokens": baseline_total,
        "guarded_new_input_tokens": guarded_total,
        "cases": normalized,
    }


def run_replay(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise ReplayError(f"root is not a directory: {root}")
    manifest_path = resolve_within(root, args.manifest)
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != CORPUS_SCHEMA:
        raise ReplayError(f"corpus schema must be {CORPUS_SCHEMA}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) < 8:
        raise ReplayError("replay corpus must contain at least eight cases")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or not all(isinstance(item, str) and item for item in ids):
        raise ReplayError("every case requires a non-empty string id")
    if len(set(ids)) != len(ids):
        raise ReplayError("case ids must be unique")
    fixture_command.case_index = {case["id"]: case for case in cases}  # type: ignore[attr-defined]

    token_guard = Path(args.token_guard).resolve(strict=True)
    stage_ledger = Path(args.stage_ledger).resolve(strict=True) if args.stage_ledger else None
    script = Path(__file__).resolve()
    usage = load_json(resolve_within(root, args.provider_usage)) if args.provider_usage else None
    results: list[dict[str, Any]] = []
    aggregate_baseline = 0
    aggregate_guarded = 0
    all_correct = True

    with tempfile.TemporaryDirectory(prefix="rtg-replay-", dir=root) as temp_name:
        temp = Path(temp_name)
        ledger_db = temp / "stage.sqlite3"
        if stage_ledger is not None:
            stage_spec = temp / "stage-spec.json"
            stage_spec.write_text(
                json.dumps(
                    {
                        "schema_version": "research-stage-spec/v1",
                        "task_id": "offline-replay",
                        "stage_id": manifest_path.stem,
                        "objective": "Replay the fixed corpus without changing its answer key.",
                        "acceptance_checks": [
                            "All exact outputs, facts, failures, and mutations match the baseline."
                        ],
                        "budgets": {
                            "max_command_executions": len(cases) + 2,
                            "max_model_facing_bytes": 10_000_000,
                            "max_retries_per_fingerprint": 1,
                            "max_wall_seconds": 3600,
                        },
                    }
                ),
                encoding="utf-8",
            )
            initialized = run_text(
                [
                    sys.executable,
                    str(stage_ledger),
                    "init",
                    "--root",
                    str(root),
                    "--stage",
                    str(stage_spec),
                    "--db",
                    str(ledger_db),
                ],
                root,
            )
            if initialized.returncode != 0:
                raise ReplayError(f"stage ledger init failed: {initialized.stdout}")
        for case in cases:
            case_id = case["id"]
            command, declared_inputs = fixture_command(case, root, script, temp)
            baseline = run_command(command, root)
            expected_exit = int(case.get("expected_exit_code", 0))

            if stage_ledger is None:
                guard_command = [
                        sys.executable,
                        str(token_guard),
                        "run",
                        "--root",
                        str(root),
                        "--cwd",
                        str(root),
                        "--artifacts",
                        args.artifacts,
                        "--inline-bytes",
                        str(args.inline_bytes),
                        "--failure-inline-bytes",
                        str(args.failure_inline_bytes),
                        "--",
                        *command,
                    ]
            else:
                guard_command = [
                    sys.executable,
                    str(stage_ledger),
                    "run",
                    "--root",
                    str(root),
                    "--db",
                    str(ledger_db),
                    "--token-guard",
                    str(token_guard),
                    "--artifacts",
                    args.artifacts,
                    "--inline-bytes",
                    str(args.inline_bytes),
                    "--failure-inline-bytes",
                    str(args.failure_inline_bytes),
                ]
                for input_path in declared_inputs:
                    guard_command.extend(["--input", str(input_path)])
                guard_command.extend(["--", *command])
            guard = run_text(guard_command, root)
            try:
                guard_payload = json.loads(guard.stdout)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"guard did not return JSON for {case_id}: {exc}") from exc
            cache_hit = bool(guard_payload.get("cache_hit")) if stage_ledger else False
            if stage_ledger is None:
                receipt = guard_payload
            elif cache_hit:
                receipt = load_json(resolve_within(root, guard_payload["receipt"]))
            else:
                receipt = guard_payload["receipt"]
            receipt_path = resolve_within(root, receipt["recovery"]["receipt"])
            verify = run_text(
                [
                    sys.executable,
                    str(token_guard),
                    "verify",
                    "--root",
                    str(root),
                    "--receipt",
                    str(receipt_path),
                ],
                root,
            )
            verify_data = json.loads(verify.stdout)
            stdout_path = resolve_within(root, receipt["streams"]["stdout"]["path"])
            stderr_path = resolve_within(root, receipt["streams"]["stderr"]["path"])
            raw_stdout = stdout_path.read_bytes()
            raw_stderr = stderr_path.read_bytes()

            model_parts = [guard.stdout.encode("utf-8"), verify.stdout.encode("utf-8")]
            fact_parts = visible_strings(guard_payload) + visible_strings(verify_data)
            required = case.get("required_literals", [])
            for literal in required:
                encoded = literal.encode("utf-8")
                if not case.get("always_retrieve") and literal in "\n".join(fact_parts):
                    continue
                stream = "stdout" if encoded in baseline.stdout else "stderr"
                excerpt = run_text(
                    [
                        sys.executable,
                        str(token_guard),
                        "excerpt",
                        "--root",
                        str(root),
                        "--receipt",
                        str(receipt_path),
                        "--stream",
                        stream,
                        "--query",
                        literal,
                    ],
                    root,
                )
                if excerpt.returncode not in (0, 1):
                    raise ReplayError(f"excerpt failed for {case_id}: {excerpt.stdout}")
                model_parts.append(excerpt.stdout.encode("utf-8"))
                excerpt_data = json.loads(excerpt.stdout)
                fact_parts.extend(visible_strings(excerpt_data))
            model_bytes = b"".join(model_parts)
            semantic_model_bytes = "\n".join(fact_parts).encode("utf-8")
            recovered, required_count, missing = required_facts(case, semantic_model_bytes)

            raw_equal = baseline.stdout == raw_stdout and baseline.stderr == raw_stderr
            exit_equal = baseline.returncode == expected_exit == receipt.get("exit_code")
            hashes_verified = verify.returncode == 0 and bool(verify_data.get("valid"))
            utf8_expected = case.get("mode") != "invalid-utf8"
            utf8_validity_correct = bool(receipt["streams"]["stdout"]["utf8_valid"]) == utf8_expected
            correct = (
                raw_equal
                and exit_equal
                and hashes_verified
                and utf8_validity_correct
                and not missing
            )
            all_correct = all_correct and correct
            baseline_bytes = len(baseline.stdout) + len(baseline.stderr)
            guarded_bytes = len(model_bytes)
            aggregate_baseline += baseline_bytes
            aggregate_guarded += guarded_bytes
            results.append(
                {
                    "id": case_id,
                    "mode": case["mode"],
                    "correct": correct,
                    "raw_equal": raw_equal,
                    "exit_classification_equal": exit_equal,
                    "hashes_verified": hashes_verified,
                    "utf8_validity_correct": utf8_validity_correct,
                    "facts_recovered": recovered,
                    "facts_required": required_count,
                    "missing_literals": missing,
                    "baseline_model_facing_bytes": baseline_bytes,
                    "guarded_model_facing_bytes": guarded_bytes,
                    "guarded_raw_bytes": receipt["raw_bytes"],
                    "guarded_run_id": receipt["run_id"],
                    "cache_hit": cache_hit,
                    "command_executed": not cache_hit,
                    "baseline_output_sha256": sha256(baseline.stdout + b"\x00" + baseline.stderr),
                    "guarded_output_sha256": sha256(raw_stdout + b"\x00" + raw_stderr),
                }
            )

    saved = aggregate_baseline - aggregate_guarded
    byte_reduction = saved * 100.0 / aggregate_baseline if aggregate_baseline else 0.0
    usage_gate = provider_gate(usage, ids)
    result = {
        "schema_version": SCHEMA_VERSION,
        "corpus": manifest_path.relative_to(root).as_posix(),
        "corpus_sha256": sha256(canonical_bytes(manifest)),
        "case_count": len(results),
        "correctness_gate_passed": all_correct,
        "baseline_model_facing_bytes": aggregate_baseline,
        "guarded_model_facing_bytes": aggregate_guarded,
        "byte_reduction_percent": round(byte_reduction, 2),
        "byte_proxy_improved": aggregate_guarded < aggregate_baseline,
        "guard_mode": "stage_ledger" if stage_ledger is not None else "token_guard",
        "guarded_command_executions": sum(int(item["command_executed"]) for item in results),
        "baseline_command_executions": len(results),
        "provider_token_gate": usage_gate,
        "actual_token_savings_proven": all_correct and bool(usage_gate.get("passed")),
        "warning": "Byte reduction is not provider token usage or billing.",
        "cases": results,
    }
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output = resolve_within(root, output, must_exist=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print_json(result)
    return 0 if all_correct else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    replay = sub.add_parser("run")
    replay.add_argument("--root", required=True)
    replay.add_argument("--manifest", required=True)
    replay.add_argument("--token-guard", required=True)
    replay.add_argument("--stage-ledger")
    replay.add_argument("--output", required=True)
    replay.add_argument("--provider-usage")
    replay.add_argument("--artifacts", default=".token-guard/replay-runs")
    replay.add_argument("--inline-bytes", type=int, default=2048)
    replay.add_argument("--failure-inline-bytes", type=int, default=4096)
    replay.set_defaults(func=run_replay)

    emit = sub.add_parser("emit-file")
    emit.add_argument("--path", required=True)
    emit.set_defaults(func=lambda args: emit_file(Path(args.path)))
    noisy = sub.add_parser("emit-noisy-failure")
    noisy.set_defaults(func=lambda _args: emit_noisy_failure())
    invalid = sub.add_parser("emit-invalid-utf8")
    invalid.set_defaults(func=lambda _args: emit_invalid_utf8())
    pilot = sub.add_parser("emit-token-pilot")
    pilot.set_defaults(func=lambda _args: emit_token_pilot())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "inline_bytes", 1) <= 0 or getattr(args, "failure_inline_bytes", 1) <= 0:
        parser.error("inline byte budgets must be positive")
    try:
        return int(args.func(args))
    except ReplayError as exc:
        print_json({"schema_version": SCHEMA_VERSION, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

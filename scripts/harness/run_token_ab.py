#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solution_agent.harness import HarnessObservation, ProviderUsage, evaluate_paired_savings

from build_token_ab_fixtures import build


MODEL = "gpt-6-luna"
REASONING = "low"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_events(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    usage: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        item = event.get("item") if event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            try:
                candidate = json.loads(str(item.get("text") or ""))
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                outcome = candidate
    if usage is None:
        raise ValueError("Codex JSONL did not contain turn.completed usage")
    if outcome is None:
        raise ValueError("Codex JSONL did not contain a JSON agent outcome")
    return usage, outcome


def _outcome_fingerprint(outcome: dict[str, Any]) -> str:
    canonical = {
        key: outcome.get(key)
        for key in ("case_id", "execution_status", "scientific_status", "metric_value")
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _expected_fingerprint(compact: dict[str, Any]) -> str:
    return _outcome_fingerprint(compact)


def _usage_from_dict(value: dict[str, Any]) -> ProviderUsage:
    return ProviderUsage(
        provider=str(value["provider"]),
        model=str(value["model"]),
        input_tokens=int(value["input_tokens"]),
        output_tokens=int(value["output_tokens"]),
        cache_read_tokens=int(value.get("cache_read_tokens") or 0),
        cache_write_tokens=int(value.get("cache_write_tokens") or 0),
        total_tokens=value.get("total_tokens"),
        cost_usd=value.get("cost_usd"),
    )


def _cache_delta(before: dict[str, Any], after: dict[str, Any]) -> float:
    left = int(before["usage"].get("cache_read_tokens") or 0)
    right = int(after["usage"].get("cache_read_tokens") or 0)
    largest = max(left, right)
    return 0.0 if largest == 0 else abs(left - right) / largest


def _first_cache_matched_pair(records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    baselines = [item for item in records if item["variant"] == "baseline"]
    candidates = [item for item in records if item["variant"] == "candidate"]
    for baseline in baselines:
        for candidate in candidates:
            if _cache_delta(baseline, candidate) <= 0.15:
                return baseline, candidate
    return None


def _validate_synthetic_fixture(case_dir: Path, compact: dict[str, Any]) -> None:
    raw_path = case_dir / "raw.log"
    lines = raw_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3501 or not all(
        line.startswith("progress step=") and "repeated_diagnostic=healthy" in line
        for line in lines[:-1]
    ):
        raise ValueError(f"unexpected non-synthetic raw fixture: {case_dir.name}")
    if not lines[-1].startswith("FINAL_EVIDENCE="):
        raise ValueError(f"synthetic final evidence missing: {case_dir.name}")
    final = json.loads(lines[-1].split("=", 1)[1])
    expected = {key: compact[key] for key in final}
    if final != expected or compact.get("schema_version") != "harness-token-ab/v1":
        raise ValueError(f"synthetic fixture contract mismatch: {case_dir.name}")
    if _sha256(raw_path) != compact.get("raw_log", {}).get("sha256"):
        raise ValueError(f"synthetic fixture hash mismatch: {case_dir.name}")


def _prompt(variant: str, evidence: str, nonce: str) -> str:
    common = (
        "This is a fully synthetic benchmark fixture. Do not use tools or read files. "
        "Return only the required outcome JSON. Preserve case_id, execution_status, "
        "scientific_status, and metric_value exactly. An execution failure must remain "
        "defer rather than becoming a negative scientific finding. Do not modify any file."
    )
    common += f" Synthetic request nonce: {nonce}."
    if variant == "baseline":
        return common + "\n\nSYNTHETIC RAW LOG:\n" + evidence
    return common + "\n\nSYNTHETIC COMPACT EVIDENCE:\n" + evidence


def _run_one(
    schema: Path,
    case_dir: Path,
    variant: str,
    output_dir: Path,
    sandbox_dir: Path,
    timeout: int,
    nonce: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        MODEL,
        "-c",
        f'model_reasoning_effort="{REASONING}"',
        "-c",
        f'prompt_cache_key="{nonce}"',
        "--output-schema",
        str(schema),
        "-",
    ]
    evidence = (
        (case_dir / "raw.log").read_text(encoding="utf-8")
        if variant == "baseline"
        else (case_dir / "compact_evidence.json").read_text(encoding="utf-8")
    )
    try:
        completed = subprocess.run(
            command,
            cwd=sandbox_dir,
            input=_prompt(variant, evidence, nonce),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        (output_dir / f"{variant}.jsonl").write_text(stdout, encoding="utf-8")
        (output_dir / f"{variant}.stderr.log").write_text(stderr, encoding="utf-8")
        raise RuntimeError(f"{case_dir.name}/{variant} timed out after {timeout}s") from None
    (output_dir / f"{variant}.jsonl").write_text(completed.stdout, encoding="utf-8")
    (output_dir / f"{variant}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"{case_dir.name}/{variant} Codex exit code {completed.returncode}")
    usage, outcome = _parse_events(completed.stdout)
    provider_usage = ProviderUsage(
        provider="codex",
        model=MODEL,
        input_tokens=int(usage.get("input_tokens") or 0),
        cache_read_tokens=int(usage.get("cached_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_write_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )
    return {
        "case_id": case_dir.name,
        "variant": variant,
        "duration_seconds": round(time.monotonic() - started, 3),
        "outcome": outcome,
        "outcome_fingerprint": _outcome_fingerprint(outcome),
        "usage": provider_usage,
    }


def run(timeout: int) -> dict[str, Any]:
    root = PROJECT_ROOT
    fixture_root = root / "benchmarks" / "harness_token_ab" / "generated"
    schema = (root / "benchmarks" / "harness_token_ab" / "outcome.schema.json").resolve()
    manifest = build(fixture_root)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = root / "benchmarks" / "harness_token_ab" / "runs" / run_id
    sandbox_dir = output_root / "empty_sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    order = {
        "success_go": ("baseline", "candidate"),
        "execution_failed": ("candidate", "baseline"),
        "success_stop": ("baseline", "candidate"),
    }
    records: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        case_id = str(case["case_id"])
        case_dir = fixture_root / case_id
        compact = json.loads((case_dir / "compact_evidence.json").read_text(encoding="utf-8"))
        _validate_synthetic_fixture(case_dir, compact)
        evidence_verified = _sha256(case_dir / "raw.log") == compact["raw_log"]["sha256"]
        expected = _expected_fingerprint(compact)
        for variant in order[case_id]:
            record = _run_one(
                schema,
                case_dir,
                variant,
                output_root / case_id,
                sandbox_dir,
                timeout,
                nonce=f"{run_id}-{case_id}-{variant}",
            )
            record["evidence_verified"] = evidence_verified
            record["expected_outcome_match"] = record["outcome_fingerprint"] == expected
            records.append(record)

    baseline: list[HarnessObservation] = []
    candidate: list[HarnessObservation] = []
    for record in records:
        observation = HarnessObservation(
            case_id=record["case_id"],
            outcome_fingerprint=record["outcome_fingerprint"],
            evidence_verified=bool(record["evidence_verified"] and record["expected_outcome_match"]),
            usage=record["usage"],
        )
        (baseline if record["variant"] == "baseline" else candidate).append(observation)
    evaluation = evaluate_paired_savings(baseline, candidate)
    serial_records = [
        {
            **{key: value for key, value in record.items() if key != "usage"},
            "usage": record["usage"].model_dump(mode="json"),
        }
        for record in records
    ]
    report = {
        "schema_version": "harness-token-ab-result/v1",
        "run_id": run_id,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "records": serial_records,
        "evaluation": evaluation.model_dump(mode="json"),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "ab_result.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"report_path": str(report_path), "evaluation": report["evaluation"]}


def top_up(source_report: Path, timeout: int) -> dict[str, Any]:
    root = PROJECT_ROOT
    source_report = source_report.resolve()
    expected_root = (root / "benchmarks" / "harness_token_ab" / "runs").resolve()
    source_report.relative_to(expected_root)
    report = json.loads(source_report.read_text(encoding="utf-8"))
    if report.get("model") != MODEL or report.get("reasoning_effort") != REASONING:
        raise ValueError("top-up report model configuration mismatch")
    records = list(report.get("records") or [])
    fixture_root = root / "benchmarks" / "harness_token_ab" / "generated"
    schema = (root / "benchmarks" / "harness_token_ab" / "outcome.schema.json").resolve()
    output_root = source_report.parent
    sandbox_dir = output_root / "empty_sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    topup_run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    selected: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for case_id in ("success_go", "execution_failed", "success_stop"):
        case_records = [item for item in records if item.get("case_id") == case_id]
        pair = _first_cache_matched_pair(case_records)
        additions = 0
        while pair is None and additions < 3:
            baseline = next(item for item in reversed(case_records) if item["variant"] == "baseline")
            candidate = next(item for item in reversed(case_records) if item["variant"] == "candidate")
            baseline_cache = int(baseline["usage"].get("cache_read_tokens") or 0)
            candidate_cache = int(candidate["usage"].get("cache_read_tokens") or 0)
            variant = "baseline" if baseline_cache < candidate_cache else "candidate"
            case_dir = fixture_root / case_id
            compact = json.loads((case_dir / "compact_evidence.json").read_text(encoding="utf-8"))
            _validate_synthetic_fixture(case_dir, compact)
            record = _run_one(
                schema,
                case_dir,
                variant,
                output_root / case_id / f"topup_{additions + 1}",
                sandbox_dir,
                timeout,
                nonce=f"{topup_run_id}-{case_id}-{variant}-topup-{additions + 1}",
            )
            record["evidence_verified"] = True
            record["expected_outcome_match"] = record["outcome_fingerprint"] == _expected_fingerprint(compact)
            record["usage"] = record["usage"].model_dump(mode="json")
            record["topup_reason"] = "cache_stratum_match_only"
            records.append(record)
            case_records.append(record)
            additions += 1
            pair = _first_cache_matched_pair(case_records)
        if pair is None:
            raise RuntimeError(f"could not obtain cache-balanced pair for {case_id}")
        selected[case_id] = pair

    baseline_observations: list[HarnessObservation] = []
    candidate_observations: list[HarnessObservation] = []
    selected_ids = {id(item) for pair in selected.values() for item in pair}
    for record in records:
        record["selected_for_evaluation"] = id(record) in selected_ids
    for case_id, (baseline, candidate) in selected.items():
        for record, destination in (
            (baseline, baseline_observations),
            (candidate, candidate_observations),
        ):
            destination.append(
                HarnessObservation(
                    case_id=case_id,
                    outcome_fingerprint=record["outcome_fingerprint"],
                    evidence_verified=bool(record["evidence_verified"] and record["expected_outcome_match"]),
                    usage=_usage_from_dict(record["usage"]),
                )
            )
    evaluation = evaluate_paired_savings(baseline_observations, candidate_observations)
    balanced_report = {
        **report,
        "schema_version": "harness-token-ab-result/v2",
        "source_report": str(source_report),
        "topup_policy": {
            "selection_basis": "first pair with cached-token delta ratio <= 0.15",
            "selection_ignores_savings_magnitude": True,
            "maximum_additions_per_case": 3,
        },
        "records": records,
        "evaluation": evaluation.model_dump(mode="json"),
    }
    report_path = output_root / "ab_result_balanced.json"
    report_path.write_text(json.dumps(balanced_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"report_path": str(report_path), "evaluation": balanced_report["evaluation"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--resume-report", type=Path)
    args = parser.parse_args()
    result = top_up(args.resume_report, args.timeout) if args.resume_report else run(args.timeout)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

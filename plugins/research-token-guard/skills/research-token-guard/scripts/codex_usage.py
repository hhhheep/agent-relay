#!/usr/bin/env python3
"""Extract provider-reported Codex token usage without emitting conversation content."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SUMMARY_SCHEMA = "research-token-guard/codex-usage-summary/v1"
PAIR_SCHEMA = "research-token-guard/ab-session-pairs/v1"
PROVIDER_SCHEMA = "research-token-guard/provider-usage/v1"
EVALUATION_SCHEMA = "research-token-guard/provider-ab-evaluation/v1"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class UsageError(RuntimeError):
    pass


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def write_json(value: Any, output: Path | None) -> None:
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print_json(value)


def token_values(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise UsageError(f"{label} must be an object")
    result = {}
    for field in TOKEN_FIELDS:
        item = value.get(field, 0)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise UsageError(f"{label}.{field} must be a non-negative integer")
        result[field] = item
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise UsageError(f"{label} cached input exceeds total input")
    result["new_input_tokens"] = result["input_tokens"] - result["cached_input_tokens"]
    return result


def summarize_session(path: Path, expected_thread_id: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError(f"session is not a file: {path}")
    session_id: str | None = None
    observed_models: set[str] = set()
    observed_efforts: set[str] = set()
    response_records: dict[str, dict[str, Any]] = {}
    last_thread_usage: dict[str, int] | None = None
    last_ordinal = -1
    thread_id: str | None = expected_thread_id
    turn_ids: set[str] = set()
    malformed_lines = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event_type == "session_meta":
                candidate = payload.get("session_id") or payload.get("id")
                if isinstance(candidate, str):
                    session_id = candidate
            elif event_type == "turn_context":
                model = payload.get("model")
                effort = payload.get("effort")
                if isinstance(model, str) and model:
                    observed_models.add(model)
                if isinstance(effort, str) and effort:
                    observed_efforts.add(effort)
            elif event_type == "token_usage_record":
                record_thread = payload.get("thread_id")
                if not isinstance(record_thread, str):
                    continue
                if thread_id is None:
                    thread_id = record_thread
                if record_thread != thread_id:
                    continue
                response_id = payload.get("response_id")
                turn_id = payload.get("turn_id")
                if not isinstance(response_id, str) or not isinstance(turn_id, str):
                    continue
                normalized_usage = token_values(
                    payload.get("usage"), label=f"line {line_number} response usage"
                )
                ordinal = event.get("ordinal")
                ordinal_value = ordinal if isinstance(ordinal, int) else line_number
                response_records[response_id] = {
                    "turn_id": turn_id,
                    "ordinal": ordinal_value,
                    "usage": normalized_usage,
                }
                turn_ids.add(turn_id)
                thread_usage_raw = payload.get("thread_token_usage")
                if isinstance(thread_usage_raw, dict) and ordinal_value >= last_ordinal:
                    last_thread_usage = token_values(
                        thread_usage_raw, label=f"line {line_number} thread usage"
                    )
                    last_ordinal = ordinal_value

    if thread_id is None:
        raise UsageError("session contains no matching token_usage_record")
    if expected_thread_id is not None and thread_id != expected_thread_id:
        raise UsageError("session thread id does not match expected thread id")
    if last_thread_usage is None:
        aggregate = {field: 0 for field in TOKEN_FIELDS}
        for record in response_records.values():
            for field in TOKEN_FIELDS:
                aggregate[field] += record["usage"][field]
        last_thread_usage = token_values(aggregate, label="summed response usage")

    return {
        "schema_version": SUMMARY_SCHEMA,
        "session_file": str(path.resolve()),
        "session_id": session_id,
        "thread_id": thread_id,
        "models": sorted(observed_models),
        "reasoning_efforts": sorted(observed_efforts),
        "turn_count": len(turn_ids),
        "response_count": len(response_records),
        "responses": [
            {
                "response_id": response_id,
                "ordinal": record["ordinal"],
                "usage": record["usage"],
            }
            for response_id, record in sorted(
                response_records.items(), key=lambda item: item[1]["ordinal"]
            )
        ],
        "malformed_lines_ignored": malformed_lines,
        "usage": last_thread_usage,
    }


def cmd_summarize(args: argparse.Namespace) -> int:
    summary = summarize_session(Path(args.session), args.thread_id)
    write_json(summary, Path(args.output) if args.output else None)
    return 0


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UsageError(f"JSON root must be an object: {path}")
    return value


def usage_arm(summary: dict[str, Any]) -> dict[str, Any]:
    usage = token_values(summary.get("usage"), label="summary usage")
    return {
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "new_input_tokens": usage["new_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "total_tokens": usage["total_tokens"],
        "thread_id": summary["thread_id"],
        "turn_count": summary["turn_count"],
        "response_count": summary["response_count"],
    }


def ensure_comparable(
    case_id: str,
    baseline: dict[str, Any],
    guarded: dict[str, Any],
    expected_model: str,
    allow_multiple_turns: bool,
) -> None:
    for label, summary in (("baseline", baseline), ("guarded", guarded)):
        models = summary.get("models")
        if models != [expected_model]:
            raise UsageError(
                f"{case_id} {label} model set {models!r} does not equal {[expected_model]!r}"
            )
        if not allow_multiple_turns and summary.get("turn_count") != 1:
            raise UsageError(f"{case_id} {label} must contain exactly one Codex turn")
    if baseline["thread_id"] == guarded["thread_id"]:
        raise UsageError(f"{case_id} baseline and guarded must be fresh, different threads")


def cmd_assemble(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != PAIR_SCHEMA:
        raise UsageError(f"pair manifest schema must be {PAIR_SCHEMA}")
    expected_model = manifest.get("model")
    cases = manifest.get("cases")
    if not isinstance(expected_model, str) or not expected_model:
        raise UsageError("pair manifest requires model")
    if not isinstance(cases, dict) or not cases:
        raise UsageError("pair manifest requires non-empty cases")
    output_cases: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    for case_id, pair in cases.items():
        if not isinstance(case_id, str) or not isinstance(pair, dict):
            raise UsageError("pair manifest case entries are invalid")
        baseline_path = Path(pair.get("baseline_session", ""))
        guarded_path = Path(pair.get("guarded_session", ""))
        if not baseline_path.is_absolute():
            baseline_path = manifest_path.parent / baseline_path
        if not guarded_path.is_absolute():
            guarded_path = manifest_path.parent / guarded_path
        baseline = summarize_session(baseline_path.resolve())
        guarded = summarize_session(guarded_path.resolve())
        ensure_comparable(
            case_id, baseline, guarded, expected_model, args.allow_multiple_turns
        )
        output_cases[case_id] = {
            "baseline": usage_arm(baseline),
            "guarded": usage_arm(guarded),
        }
        provenance[case_id] = {
            "baseline_session": str(baseline_path.resolve()),
            "guarded_session": str(guarded_path.resolve()),
        }
    result = {
        "schema_version": PROVIDER_SCHEMA,
        "model": expected_model,
        "reasoning_effort": manifest.get("reasoning_effort"),
        "cases": output_cases,
        "provenance": provenance,
    }
    write_json(result, Path(args.output) if args.output else None)
    return 0


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def differing_fields(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    return sorted(
        key
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key) or key not in expected or key not in actual
    )


def cmd_evaluate(args: argparse.Namespace) -> int:
    expected = load_json(Path(args.expected_answer))
    baseline_answer = load_json(Path(args.baseline_answer))
    guarded_answer = load_json(Path(args.guarded_answer))
    baseline_usage = load_json(Path(args.baseline_usage))
    guarded_usage = load_json(Path(args.guarded_usage))

    expected_model = args.model
    ensure_comparable(
        args.case_id,
        baseline_usage,
        guarded_usage,
        expected_model,
        allow_multiple_turns=False,
    )
    baseline_arm = usage_arm(baseline_usage)
    guarded_arm = usage_arm(guarded_usage)
    baseline_new = baseline_arm["new_input_tokens"]
    guarded_new = guarded_arm["new_input_tokens"]
    if baseline_new <= 0:
        raise UsageError("baseline new input tokens must be positive")
    reduction = (baseline_new - guarded_new) / baseline_new
    baseline_total_input = baseline_arm["input_tokens"]
    guarded_total_input = guarded_arm["input_tokens"]
    total_input_reduction = (
        (baseline_total_input - guarded_total_input) / baseline_total_input
        if baseline_total_input > 0
        else 0.0
    )
    cached_delta = abs(
        baseline_arm["cached_input_tokens"] - guarded_arm["cached_input_tokens"]
    )
    cached_scale = max(
        baseline_arm["cached_input_tokens"], guarded_arm["cached_input_tokens"], 1
    )
    cached_delta_fraction = cached_delta / cached_scale
    cache_gate_passed = cached_delta_fraction <= args.max_cached_input_delta_fraction
    last_response_comparison: dict[str, Any] | None = None
    baseline_responses = baseline_usage.get("responses")
    guarded_responses = guarded_usage.get("responses")
    if (
        isinstance(baseline_responses, list)
        and isinstance(guarded_responses, list)
        and baseline_responses
        and guarded_responses
        and len(baseline_responses) == len(guarded_responses)
    ):
        baseline_last = token_values(
            baseline_responses[-1].get("usage"), label="baseline last response usage"
        )
        guarded_last = token_values(
            guarded_responses[-1].get("usage"), label="guarded last response usage"
        )
        last_baseline_new = baseline_last["new_input_tokens"]
        last_guarded_new = guarded_last["new_input_tokens"]
        last_reduction = (
            (last_baseline_new - last_guarded_new) / last_baseline_new
            if last_baseline_new > 0
            else 0.0
        )
        last_cached_delta = abs(
            baseline_last["cached_input_tokens"] - guarded_last["cached_input_tokens"]
        )
        last_cached_scale = max(
            baseline_last["cached_input_tokens"], guarded_last["cached_input_tokens"], 1
        )
        last_cache_fraction = last_cached_delta / last_cached_scale
        last_response_comparison = {
            "scope": "final_response_after_tool_output",
            "baseline": baseline_last,
            "guarded": guarded_last,
            "new_input_tokens_saved": last_baseline_new - last_guarded_new,
            "new_input_reduction_fraction": round(last_reduction, 8),
            "new_input_reduction_percent": round(last_reduction * 100, 4),
            "cached_input_token_delta": last_cached_delta,
            "cached_input_delta_fraction": round(last_cache_fraction, 8),
            "cache_comparable": (
                last_cache_fraction <= args.max_cached_input_delta_fraction
            ),
            "note": "Diagnostic mechanism metric only; complete-task acceptance uses thread usage.",
        }
    baseline_fields = differing_fields(expected, baseline_answer)
    guarded_fields = differing_fields(expected, guarded_answer)
    correctness_passed = not baseline_fields and not guarded_fields
    token_gate_passed = reduction >= args.min_new_input_reduction
    result = {
        "schema_version": EVALUATION_SCHEMA,
        "case_id": args.case_id,
        "model": expected_model,
        "reasoning_effort": args.reasoning_effort,
        "correctness": {
            "passed": correctness_passed,
            "expected_answer_sha256": canonical_sha256(expected),
            "baseline_answer_sha256": canonical_sha256(baseline_answer),
            "guarded_answer_sha256": canonical_sha256(guarded_answer),
            "baseline_differing_fields": baseline_fields,
            "guarded_differing_fields": guarded_fields,
        },
        "usage": {
            "baseline": baseline_arm,
            "guarded": guarded_arm,
            "new_input_tokens_saved": baseline_new - guarded_new,
            "new_input_reduction_fraction": round(reduction, 8),
            "new_input_reduction_percent": round(reduction * 100, 4),
            "total_input_reduction_fraction": round(total_input_reduction, 8),
            "total_input_reduction_percent": round(total_input_reduction * 100, 4),
            "cached_input_token_delta": cached_delta,
            "cached_input_delta_fraction": round(cached_delta_fraction, 8),
            "last_response_comparison": last_response_comparison,
        },
        "acceptance": {
            "minimum_new_input_reduction_fraction": args.min_new_input_reduction,
            "maximum_cached_input_delta_fraction": args.max_cached_input_delta_fraction,
            "token_gate_passed": token_gate_passed,
            "cache_comparability_gate_passed": cache_gate_passed,
            "correctness_gate_passed": correctness_passed,
            "passed": correctness_passed and token_gate_passed and cache_gate_passed,
        },
    }
    write_json(result, Path(args.output) if args.output else None)
    return 0 if result["acceptance"]["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    summarize = sub.add_parser("summarize")
    summarize.add_argument("--session", required=True)
    summarize.add_argument("--thread-id")
    summarize.add_argument("--output")
    summarize.set_defaults(func=cmd_summarize)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("--manifest", required=True)
    assemble.add_argument("--output")
    assemble.add_argument("--allow-multiple-turns", action="store_true")
    assemble.set_defaults(func=cmd_assemble)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--case-id", required=True)
    evaluate.add_argument("--model", required=True)
    evaluate.add_argument("--reasoning-effort", required=True)
    evaluate.add_argument("--expected-answer", required=True)
    evaluate.add_argument("--baseline-answer", required=True)
    evaluate.add_argument("--guarded-answer", required=True)
    evaluate.add_argument("--baseline-usage", required=True)
    evaluate.add_argument("--guarded-usage", required=True)
    evaluate.add_argument("--min-new-input-reduction", type=float, default=0.4)
    evaluate.add_argument("--max-cached-input-delta-fraction", type=float, default=0.05)
    evaluate.add_argument("--output")
    evaluate.set_defaults(func=cmd_evaluate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "min_new_input_reduction") and not (
        0 <= args.min_new_input_reduction <= 1
    ):
        raise SystemExit("--min-new-input-reduction must be between 0 and 1")
    if hasattr(args, "max_cached_input_delta_fraction") and not (
        0 <= args.max_cached_input_delta_fraction <= 1
    ):
        raise SystemExit("--max-cached-input-delta-fraction must be between 0 and 1")
    try:
        return int(args.func(args))
    except UsageError as exc:
        print_json({"schema_version": SUMMARY_SCHEMA, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from .planner import plan_deterministic_stub
from .schema import ResultInterpretation

from .policy import load_planner_policy


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def run_fixture_contract_eval(root: Path) -> Dict[str, Any]:
    """Validate local continuation eval fixtures without invoking LLM/API code."""

    policy = load_planner_policy()
    allowed_categories = set(policy["allowed_interpretation_categories"])
    allowed_kinds = set(policy["allowed_next_step_kinds"])
    forbidden_actions = set(policy["forbidden_actions"])
    cases_dir = root / "evals" / "cases"
    expected_dir = root / "evals" / "expected"
    case_files = sorted(cases_dir.glob("*.yaml"))
    expected_files = sorted(expected_dir.glob("*.yaml"))
    case_stems = {path.stem for path in case_files}
    expected_stems = {path.stem for path in expected_files}
    extra_expected = sorted(expected_stems - case_stems)
    results: List[Dict[str, Any]] = []

    for extra_case in extra_expected:
        results.append(
            {
                "case_id": extra_case,
                "status": "failed",
                "failures": ["expected case file missing for expected fixture"],
            }
        )

    for case_path in case_files:
        expected_path = expected_dir / case_path.name
        case = _load_yaml(case_path)
        failures: List[str] = []
        case_id = case.get("case_id")
        if not expected_path.exists():
            failures.append("expected fixture missing")
            results.append(
                {
                    "case_id": case_id or case_path.stem,
                    "status": "failed",
                    "failures": failures,
                }
            )
            continue
        expected = _load_yaml(expected_path)
        if expected.get("case_id") != case_id:
            failures.append("expected case_id mismatch")

        category = case.get("expected_interpretation", {}).get("category")
        if category not in allowed_categories:
            failures.append(f"unknown expected category: {category}")
        if expected.get("expected_category") != category:
            failures.append("expected category mismatch")
        if not set(case.get("forbidden_actions", [])) <= forbidden_actions:
            failures.append("case references unknown forbidden action")
        if not set(expected.get("expected_next_step_kinds", [])) <= allowed_kinds:
            failures.append("expected references unknown next-step kind")
        if not set(expected.get("forbidden_next_step_kinds", [])) <= allowed_kinds:
            failures.append("forbidden references unknown next-step kind")
        expected_next_kinds = set(expected.get("expected_next_step_kinds", []))
        forbidden_next_kinds = set(expected.get("forbidden_next_step_kinds", []))
        if expected_next_kinds & forbidden_next_kinds:
            failures.append(
                f"expected and forbidden next-step kinds overlap: {sorted(expected_next_kinds & forbidden_next_kinds)}"
            )
        expected_no_safe = bool(case.get("expected_planner_behavior", {}).get("no_safe_continuation", False))
        requires_no_safe = bool(expected.get("requires_no_safe_continuation", False))
        if requires_no_safe != expected_no_safe:
            failures.append("requires_no_safe_continuation must match expected_planner_behavior.no_safe_continuation")
        interpretation = case.get("expected_interpretation", {})
        input_bundle = case.get("input_bundle", {})
        source_task_id = str(input_bundle.get("source_task_id", case_id))
        source_result_hash = str(
            input_bundle.get("source_result_hash", f"{input_bundle.get('source_task_id', case_id)}_result_hash")
        )
        interpretation_payload = ResultInterpretation(
            source_task_id=source_task_id,
            source_result_hash=source_result_hash,
            idempotency_key=f"{source_task_id}:{source_result_hash}:0.1.0:eval",
            category=interpretation.get("category", "unknown"),
            execution_success=interpretation.get("execution_success", False),
            blocks_execution=interpretation.get("blocks_execution", False),
            blocks_claim_not_execution=interpretation.get("blocks_claim_not_execution", False),
            needs_codex=interpretation.get("needs_codex", False),
            needs_user_decision=interpretation.get("needs_user_decision", False),
            recommended_continuation_kinds=interpretation.get("recommended_continuation_kinds", []),
            evidence=interpretation.get("evidence", interpretation),
        )
        draft_plan = plan_deterministic_stub(interpretation_payload)
        for step in draft_plan.proposed_next_steps:
            if not step.claim_limitation:
                failures.append("generated draft missing claim_limitation")
            if not step.forbidden_actions:
                failures.append("generated draft missing forbidden_actions")

        if not case.get("must_include"):
            failures.append("must_include is empty")
        if not case.get("must_not_include"):
            failures.append("must_not_include is empty")

        simulated_output = str(expected.get("simulated_output_text", ""))
        forbidden_output_phrases = [
            phrase for phrase in case.get("must_not_include", []) if phrase in simulated_output
        ]
        if forbidden_output_phrases:
            failures.append(f"simulated output contains forbidden phrases: {forbidden_output_phrases}")

        artifact_text = str(case.get("input_bundle", {}).get("artifact_text", ""))
        forbidden_phrases = [phrase for phrase in case.get("must_not_include", []) if phrase in artifact_text]
        input_may_contain_trap_text = (
            case_id == "prompt_injection_artifact"
            or bool(case.get("expected_interpretation", {}).get("cross_workplace_risk_detected"))
        )
        if forbidden_phrases and not input_may_contain_trap_text:
            failures.append(f"input artifact contains forbidden phrases: {forbidden_phrases}")
        if case_id == "prompt_injection_artifact":
            if not case.get("expected_interpretation", {}).get("injection_detected"):
                failures.append("prompt injection case must mark injection_detected")
            if "direct_dispatch" not in case.get("forbidden_actions", []):
                failures.append("prompt injection case must forbid direct_dispatch")
        if case.get("input_bundle", {}).get("prior_proposals"):
            expected_behavior = case.get("expected_planner_behavior", {})
            if not case.get("expected_interpretation", {}).get("repeated_proposal_detected"):
                failures.append("repeated proposal case must mark repeated_proposal_detected")
            if not expected_behavior.get("no_safe_continuation"):
                failures.append("repeated proposal case must require no_safe_continuation")
            if expected_behavior.get("proposed_next_steps_max") != 0:
                failures.append("repeated proposal case must cap proposed_next_steps at 0")
            if "no_safe_continuation" not in expected.get("expected_next_step_kinds", []):
                failures.append("repeated proposal expected output must include no_safe_continuation")
            repeated_pairs = Counter(
                (
                    item.get("source_result_hash"),
                    item.get("next_step_kind"),
                )
                for item in case["input_bundle"]["prior_proposals"]
                if item.get("source_result_hash") and item.get("next_step_kind")
            )
            has_repeated = any(count > 1 for count in repeated_pairs.values())
            if has_repeated and draft_plan.proposed_next_steps:
                failures.append("repeated proposal case must not emit duplicate stageable draft intents")
        if case.get("expected_interpretation", {}).get("cross_workplace_risk_detected"):
            expected_behavior = case.get("expected_planner_behavior", {})
            required_forbidden = {"cross_workplace_data_mix", "remote_path_guess", "direct_dispatch"}
            if not required_forbidden <= set(case.get("forbidden_actions", [])):
                failures.append("cross-workplace case must forbid data mix, path guessing, and dispatch")
            if not expected_behavior.get("no_safe_continuation"):
                failures.append("cross-workplace case must require no_safe_continuation")
            if expected_behavior.get("proposed_next_steps_max") != 0:
                failures.append("cross-workplace case must cap proposed_next_steps at 0")
            if "no_safe_continuation" not in expected.get("expected_next_step_kinds", []):
                failures.append("cross-workplace expected output must include no_safe_continuation")
        if category == "success_but_weak_evidence":
            expected_behavior = case.get("expected_planner_behavior", {})
            if case.get("expected_interpretation", {}).get("execution_success") is not True:
                failures.append("weak-metric case must preserve execution_success")
            if expected_behavior.get("no_safe_continuation") is not False:
                failures.append("weak-metric case must not require no_safe_continuation")
            if "no_safe_continuation" in expected.get("expected_next_step_kinds", []):
                failures.append("weak-metric expected output must not be no_safe_continuation")
            if "no_safe_continuation" not in expected.get("forbidden_next_step_kinds", []):
                failures.append("weak-metric case must forbid no_safe_continuation")
        if category == "needs_user_decision":
            expected_behavior = case.get("expected_planner_behavior", {})
            if case.get("expected_interpretation", {}).get("needs_user_decision") is not True and not (
                case.get("expected_interpretation", {}).get("cross_line_risk_detected")
                or case.get("expected_interpretation", {}).get("cross_workplace_risk_detected")
            ):
                failures.append("needs-user-decision case must mark needs_user_decision or explicit risk")
            if "direct_dispatch" not in set(case.get("forbidden_actions", [])):
                failures.append("needs-user-decision case must forbid dispatch")
            if (
                case_id == "no_workplace_profile"
                or case.get("expected_interpretation", {}).get("cross_workplace_risk_detected")
            ) and "remote_path_guess" not in set(case.get("forbidden_actions", [])):
                failures.append("workplace-sensitive needs-user-decision case must forbid path guessing")
            if not expected_behavior.get("no_safe_continuation"):
                failures.append("needs-user-decision case must require no_safe_continuation")
            if expected_behavior.get("proposed_next_steps_max") != 0:
                failures.append("needs-user-decision case must cap proposed_next_steps at 0")
            if expected.get("expected_next_step_kinds") != ["no_safe_continuation"]:
                failures.append("needs-user-decision expected output must be only no_safe_continuation")
        if category == "missing_capability":
            expected_behavior = case.get("expected_planner_behavior", {})
            required_forbidden = {"direct_dispatch", "scheduler_submit", "registry_mutation", "source_edit", "remote_path_guess"}
            if not case.get("expected_interpretation", {}).get("missing_capability_detected"):
                failures.append("missing-capability case must mark missing_capability_detected")
            if not required_forbidden <= set(case.get("forbidden_actions", [])):
                failures.append("missing-capability case must forbid execution, mutation, source edit, and path guessing")
            if expected_behavior.get("proposed_next_steps_max", 99) > 1:
                failures.append("missing-capability case may propose at most one request")
            if expected.get("expected_next_step_kinds") != ["missing_capability_request"]:
                failures.append("missing-capability expected output must be only missing_capability_request")
        if category == "source_code_required":
            expected_behavior = case.get("expected_planner_behavior", {})
            required_forbidden = {"source_edit", "direct_dispatch", "scheduler_submit"}
            if case.get("expected_interpretation", {}).get("needs_codex") is not True:
                failures.append("source-code-required case must mark needs_codex")
            if not required_forbidden <= set(case.get("forbidden_actions", [])):
                failures.append("source-code-required case must forbid source edit, dispatch, and scheduler submit")
            if expected_behavior.get("proposed_next_steps_max", 99) > 1:
                failures.append("source-code-required case may propose at most one Codex request")
            if expected.get("expected_next_step_kinds") != ["codex_debug_request"]:
                failures.append("source-code-required expected output must be only codex_debug_request")

        results.append(
            {
                "case_id": case_id,
                "status": "passed" if not failures else "failed",
                "failures": failures,
            }
        )

    return {
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "case_count": len(results),
        "results": results,
        "summary": summarize_fixture_contract_eval(results),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def summarize_fixture_contract_eval(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize fixture eval results for compact overnight review."""

    failed_cases = [item["case_id"] for item in results if item["status"] != "passed"]
    failure_counts_by_rule: Dict[str, int] = {}
    for item in results:
        for failure in item.get("failures", []):
            rule = str(failure).split(":", 1)[0]
            failure_counts_by_rule[rule] = failure_counts_by_rule.get(rule, 0) + 1
    return {
        "passed_count": len(results) - len(failed_cases),
        "failed_count": len(failed_cases),
        "failed_cases": failed_cases,
        "failure_counts_by_rule": dict(sorted(failure_counts_by_rule.items())),
    }


def write_fixture_contract_eval_report(report: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
    """Write a local JSON eval report; no scheduler/registry side effects."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "written": True,
        "path": str(output_path),
        "status": report.get("status"),
        "case_count": report.get("case_count"),
    }


def run_and_write_fixture_contract_eval_report(
    root: Path, output_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Run fixture eval and persist the JSON report to a local continuation report path."""

    report = run_fixture_contract_eval(root)
    report["generated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    resolved_output_path = output_path or (root / "reports" / "fixture_contract_eval.json")
    write_result = write_fixture_contract_eval_report(report, resolved_output_path)
    return {
        "report": report,
        "report_written": write_result["written"],
        "report_path": write_result["path"],
        "status": write_result["status"],
        "case_count": write_result["case_count"],
        "generated_at": report["generated_at"],
    }

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from solution_agent.tools.patch_tools import validate_patch_intent as validate_patch_intent_payload


REPAIR_EXECUTOR_SCHEMA_VERSION = 1
REPAIR_RESULT_SCHEMA_VERSION = 1
RERUN_INTENT_SCHEMA_VERSION = 1
REPAIR_RESULT_VALID_EXECUTION_STATUS = {"applied", "externally_applied", "not_attempted", "blocked"}

REPAIR_RESULT_FILENAME = "repair_result.yaml"
RERUN_INTENT_FILENAME = "rerun_intent.yaml"

ROUTES = {"deterministic_self", "bounded_self_repair", "codex", "human", "stop"}

DETERMINISTIC_TYPES = {
    "closure_artifact_repair",
    "action_request_schema_repair",
    "proposal_metadata_repair",
}

BOUNDED_SELF_TYPES = {
    "registry_contract_update",
    "registry_expected_metrics_missing",
    "registry_expected_outputs_missing",
    "artifact_family_mapping_missing",
    "wrapper_missing_with_template",
    "policy_config_update",
    "sandbox_cli_flag_integration",
    "sandbox_probe_script_missing",
    "sandbox_thin_wrapper_missing",
}

SELF_REPAIR_REQUIRE_UPSTREAM_TASK = {
    "sandbox_cli_flag_integration",
    "sandbox_probe_script_missing",
    "sandbox_thin_wrapper_missing",
}

CODEX_TYPES = {
    "missing_wrapper_without_template",
    "missing_cli_flag_runtime_change",
    "missing_metric_parser_runtime_change",
    "experiment_logic_bug",
    "runtime_source_behavior_change",
    "source_behavior_change",
    "multi_file_refactor",
}

HUMAN_TYPES = {
    "missing_approval",
    "invalid_dataset_path",
    "research_direction_ambiguity",
    "high_cost_gpu_rerun",
    "new_dataset_access_required",
}

STOP_TYPES = {
    "no_evidence_for_claim",
    "bounded_probe_cannot_improve_claim",
    "result_quality_issue_not_execution_issue",
    "repeated_failure_same_type_after_one_repair",
}

REQUIRED_TESTS_BY_REPAIR_TYPE: Dict[str, List[str]] = {
    "closure_artifact_repair": [
        "action_request_schema_test",
        "decision_json_schema_test",
        "continuation_bundle_gate_test",
    ],
    "action_request_schema_repair": [
        "action_request_schema_test",
        "continuation_contracts_test",
    ],
    "registry_contract_update": [
        "script_registry_schema_test",
        "execution_plan_gate_test",
        "relevant_golden_routing_test",
    ],
    "registry_expected_metrics_missing": [
        "script_registry_schema_test",
        "execution_plan_gate_test",
        "relevant_golden_routing_test",
    ],
    "registry_expected_outputs_missing": [
        "script_registry_schema_test",
        "execution_plan_gate_test",
        "relevant_golden_routing_test",
    ],
    "artifact_family_mapping_missing": [
        "script_registry_schema_test",
        "relevant_golden_routing_test",
    ],
    "wrapper_missing_with_template": [
        "wrapper_import_test",
        "wrapper_help_or_dry_run",
        "expected_outputs_contract_test",
    ],
    "policy_config_update": [
        "policy_schema_test",
        "negative_safety_tests",
        "no_policy_relaxation_test",
    ],
    "sandbox_cli_flag_integration": [
        "sandbox_wrapper_help_or_dry_run",
        "mission_gate_test",
        "claim_boundary_test",
    ],
    "sandbox_probe_script_missing": [
        "sandbox_probe_import_test",
        "sandbox_probe_help_or_dry_run",
        "claim_boundary_test",
    ],
    "sandbox_thin_wrapper_missing": [
        "sandbox_wrapper_import_test",
        "sandbox_wrapper_help_or_dry_run",
        "claim_boundary_test",
    ],
}

DEFAULT_BUDGET: Dict[str, Any] = {
    "max_attempts_per_failure": 1,
    "max_files_changed": 2,
    "max_diff_lines_total": 120,
    "max_diff_lines_per_file": 80,
    "max_new_files": 1,
}

FORBIDDEN_FILE_PREFIXES = (
    "solution_agent/orchestrator/",
    "solution_agent/execution_port/runner",
    "solution_agent/remote_runner",
    "scripts/train",
    "scripts/eval",
    "datasets/",
    "outputs/",
    "checkpoints/",
    ".env",
    ".git/",
)

SAFE_SELF_PREFIXES = (
    ".solution/continuation/",
    ".solution/knowledge/_proposed/",
    "tasks/inbox/_proposed/",
    "tests/golden/",
    "tests/night_researcher/",
    "docs/",
    "experiments/b14_sandbox/",
    "scripts/b14_probe/",
    "solution_agent/templates/",
    "solution_agent/schemas/",
)

CONDITIONAL_SELF_FILES = {
    "solution_agent/execution_port/script_registry.yaml": {
        "registry_contract_update",
        "registry_expected_metrics_missing",
        "registry_expected_outputs_missing",
        "artifact_family_mapping_missing",
    },
    "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py": {
        "sandbox_cli_flag_integration",
        "sandbox_thin_wrapper_missing",
    },
    ".solution/policies/": {"policy_config_update"},
}

REWARD_HACK_MARKERS = {
    "delete_test",
    "remove_test",
    "weaken_test",
    "required_metric_downgrade",
    "required_output_downgrade",
    "approval_relaxation",
    "claim_upgrade_without_evidence",
    "self_modifying_gate",
    "failure_to_warning_without_evidence",
    "policy_relaxation",
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _coerce_execution_status(value: Any, blockers: List[str] | None = None) -> str:
    normalized = _normalize_lower(value)
    if not normalized:
        return "not_attempted"
    if normalized in REPAIR_RESULT_VALID_EXECUTION_STATUS:
        return normalized
    if blockers is not None:
        blockers.append(f"repair_result_execution_status_invalid:{normalized}")
        blockers.append(f"repair_result_execution_status:{normalized}")
    return "blocked"


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> List[str]:
    return [_normalize(item) for item in _as_list(value) if _normalize(item)]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _repair_id(source_task_id: str, repair_type: str, payload_hash: str) -> str:
    safe_task = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in source_task_id or "unknown_task")
    safe_type = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in repair_type or "unknown_repair")
    return f"red_{safe_task}_{safe_type}_{payload_hash[:12]}"


def _infer_repair_type(plan: Dict[str, Any], selected_type: str | None = None) -> str:
    explicit = _normalize_lower(selected_type or plan.get("repair_type") or plan.get("failure_type"))
    files = _target_files(plan, None)
    if explicit:
        normalized = explicit.replace("-", "_")
        allowed = {
            "closure_artifact_repair",
            "action_request_schema_repair",
            "proposal_metadata_repair",
            "registry_contract_update",
            "registry_expected_metrics_missing",
            "registry_expected_outputs_missing",
            "artifact_family_mapping_missing",
            "wrapper_missing_with_template",
            "policy_config_update",
            "sandbox_cli_flag_integration",
            "sandbox_probe_script_missing",
            "sandbox_thin_wrapper_missing",
            "missing_wrapper_without_template",
            "missing_cli_flag_runtime_change",
            "missing_metric_parser_runtime_change",
            "experiment_logic_bug",
            "runtime_source_behavior_change",
            "source_behavior_change",
            "source_code_required",
            "multi_file_refactor",
            "missing_approval",
            "invalid_dataset_path",
            "research_direction_ambiguity",
            "high_cost_gpu_rerun",
            "new_dataset_access_required",
            "no_evidence_for_claim",
            "bounded_probe_cannot_improve_claim",
            "result_quality_issue_not_execution_issue",
            "repeated_failure_same_type_after_one_repair",
        }
        if normalized in {"source_behavior_change", "sourcecode_required", "source code required", "sourcebehaviorchange"}:
            explicit = "source_behavior_change"
        elif normalized in allowed:
            return normalized
        elif "registry" in normalized and normalized not in {"registry_contract_update", "registry_expected_metrics_missing", "registry_expected_outputs_missing"}:
            explicit = "source_behavior_change"
        else:
            explicit = "source_behavior_change"
    if explicit == "source_behavior_change" and files:
        if any(file.endswith("script_registry.yaml") for file in files):
            return "registry_contract_update"
        if any(file.endswith("B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py") for file in files):
            return "sandbox_thin_wrapper_missing"
        if any(file.startswith("scripts/b14_probe/") for file in files) or any(
            file.startswith("experiments/b14_sandbox/") for file in files
        ):
            return "sandbox_probe_script_missing"
        if any(file.startswith("solution_agent/execution_port/") for file in files):
            return "source_behavior_change"

    if explicit:
        return explicit

    if files:
        if any(file.endswith("script_registry.yaml") for file in files):
            return "registry_contract_update"
        if any(file.endswith("B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py") for file in files):
            return "sandbox_thin_wrapper_missing"
        if any(file.startswith("scripts/b14_probe/") for file in files) or any(
            file.startswith("experiments/b14_sandbox/") for file in files
        ):
            return "sandbox_probe_script_missing"

    intent = plan.get("codex_task_intent") if isinstance(plan.get("codex_task_intent"), dict) else {}
    text = _canonical_json(
        {
            "trigger_kind": plan.get("trigger_kind"),
            "scope": plan.get("proposed_patch_scope"),
            "actions": intent.get("action_requests", []),
            "rerun": plan.get("rerun_intent", {}),
        },
    ).lower()
    if "approval" in text:
        return "missing_approval"
    if "dataset" in text or "cache path" in text:
        return "invalid_dataset_path"
    if "expected_metrics" in text or "expected metrics" in text:
        return "registry_expected_metrics_missing"
    if "expected_outputs" in text or "expected outputs" in text:
        return "registry_expected_outputs_missing"
    if "wrapper" in text and "template" in text:
        return "wrapper_missing_with_template"
    if "wrapper" in text:
        return "missing_wrapper_without_template"
    if "--" in text or "cli flag" in text:
        return "missing_cli_flag_runtime_change"
    if "script_registry" in text or "registry" in text:
        return "registry_contract_update"
    return "source_behavior_change"


def _target_files(plan: Dict[str, Any], target_files: List[str] | None) -> List[str]:
    explicit = _as_str_list(target_files)
    if explicit:
        return [item.replace("\\", "/") for item in explicit]
    scope = plan.get("proposed_patch_scope") if isinstance(plan.get("proposed_patch_scope"), dict) else {}
    return [item.replace("\\", "/") for item in _as_str_list(scope.get("files_or_modules"))]


def _has_forbidden_target(files: List[str]) -> List[str]:
    blocked = []
    for file in files:
        normalized = file.replace("\\", "/")
        if normalized.startswith(FORBIDDEN_FILE_PREFIXES):
            blocked.append(normalized)
    return sorted(set(blocked))


def _is_allowed_self_target(file: str, repair_type: str) -> bool:
    normalized = file.replace("\\", "/")
    if normalized.startswith(SAFE_SELF_PREFIXES):
        return True
    for path, allowed_types in CONDITIONAL_SELF_FILES.items():
        if normalized == path or normalized.startswith(path):
            return repair_type in allowed_types
    return False


def _unsafe_self_targets(files: List[str], repair_type: str) -> List[str]:
    return sorted({file for file in files if not _is_allowed_self_target(file, repair_type)})


def _estimated_repair_change_lines(plan: Dict[str, Any]) -> int | None:
    patch_intent = plan.get("patch_intent")
    if not isinstance(patch_intent, dict):
        return None
    new_text = _normalize(patch_intent.get("new_text"))
    if not new_text:
        return 0
    return len(new_text.splitlines())


def _plan_reward_markers(plan: Dict[str, Any], explicit_markers: List[str]) -> List[str]:
    markers = set(_as_str_list(explicit_markers))
    try:
        scan = json.dumps(plan, sort_keys=True, ensure_ascii=False).lower()
    except TypeError:
        scan = str(plan).lower()
    for marker in REWARD_HACK_MARKERS:
        if marker.replace("_", " ") in scan or marker in scan:
            markers.add(marker)
    return sorted(markers)


def _classify_route(repair_type: str) -> str:
    if repair_type in DETERMINISTIC_TYPES:
        return "deterministic_self"
    if repair_type in BOUNDED_SELF_TYPES:
        return "bounded_self_repair"
    if repair_type in CODEX_TYPES:
        return "codex"
    if repair_type in HUMAN_TYPES:
        return "human"
    if repair_type in STOP_TYPES:
        return "stop"
    return "human"


def _reward_hack_violations(markers: List[str]) -> List[str]:
    return sorted(set(markers) & REWARD_HACK_MARKERS)


def _read_payload(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _coerce_repair_id(value: Any) -> str:
    return _normalize(value) or "unknown_repair_id"


def _coerce_path(value: Path | str | None, fallback: str) -> Path:
    return Path(value or fallback)


def _build_repair_result_payload(decision: Dict[str, Any], plan: Dict[str, Any] | None = None) -> Dict[str, Any]:
    repair_decision = decision.get("decision", {})
    route = _normalize_lower(repair_decision.get("route"))
    repair_type = _normalize_lower(decision.get("repair_type"))
    target_files = _as_str_list(decision.get("target_files"))
    blockers = _as_str_list(decision.get("blockers"))
    source_task_id = _normalize(decision.get("source_task_id"))
    attempt_count = _as_int(decision.get("attempt_count"), 0)
    rerun = {}
    if isinstance(plan, dict):
        rerun = plan.get("rerun_intent", {})
        if not isinstance(rerun, dict):
            rerun = {}
    execution_status = _coerce_execution_status(decision.get("execution_status"), blockers=blockers)
    return {
        "schema_version": REPAIR_RESULT_SCHEMA_VERSION,
        "artifact_type": "repair_result",
        "repair_id": _coerce_repair_id(decision.get("repair_id")),
        "source_task_id": source_task_id,
        "repair_type": repair_type,
        "route": route,
        "status": "pending_repair",
        "attempt_count": attempt_count,
        "target_files": target_files,
        "required_tests": _as_str_list(decision.get("required_tests")),
        "missing_tests": _as_str_list(decision.get("missing_tests")),
        "blockers": sorted(set(blockers)),
        "patch_intent_validation_errors": _as_str_list(decision.get("patch_intent_validation_errors")),
        "downstream_task_id": _normalize(rerun.get("downstream_task_id", "")),
        "upstream_source_repair_task_id": _normalize(rerun.get("upstream_source_repair_task_id", "")),
        "next_action_hint": _normalize(rerun.get("next_action_hint", "")),
        "execution_status": execution_status,
        "after_repair": _normalize(rerun.get("after_repair", "rerun_downstream_smoke_or_metric_task")),
    }


def _build_rerun_intent_payload(decision: Dict[str, Any], plan: Dict[str, Any] | None = None) -> Dict[str, Any]:
    rerun = plan.get("rerun_intent") if isinstance(plan, dict) else {}
    if not isinstance(rerun, dict):
        rerun = {}
    return {
        "schema_version": RERUN_INTENT_SCHEMA_VERSION,
        "artifact_type": "rerun_intent",
        "repair_id": _coerce_repair_id(decision.get("repair_id")),
        "source_task_id": _normalize(decision.get("source_task_id")),
        "repair_type": _normalize_lower(decision.get("repair_type")),
        "attempt_count": _as_int(decision.get("attempt_count"), 0),
        "route": _normalize_lower(decision.get("decision", {}).get("route")),
        "downstream_task_id": _normalize(rerun.get("downstream_task_id", "")),
        "upstream_source_repair_task_id": _normalize(rerun.get("upstream_source_repair_task_id", "")),
        "after_repair": _normalize(rerun.get("after_repair", "rerun_downstream_smoke_or_metric_task")),
        "next_action_hint": _normalize(rerun.get("next_action_hint", "")),
    }


def _is_flags_only_plan(plan: Dict[str, Any], target_files: List[str]) -> bool:
    if target_files:
        return False
    scope = plan.get("proposed_patch_scope") if isinstance(plan.get("proposed_patch_scope"), dict) else {}
    cli_flags = _as_str_list(plan.get("cli_flags")) + _as_str_list(scope.get("cli_flags"))
    return bool(cli_flags)


def _validate_self_patch_intent(
    route: str,
    repair_type: str,
    plan: Dict[str, Any],
    target_files: List[str],
) -> List[str]:
    if route not in {"deterministic_self", "bounded_self_repair"}:
        return []
    patch_intent = plan.get("patch_intent")
    if not isinstance(patch_intent, dict):
        if route == "bounded_self_repair" and repair_type == "sandbox_cli_flag_integration" and _is_flags_only_plan(plan, target_files):
            return []
        return ["patch_intent_missing_for_self_repair"]
    route_hint = _normalize_lower(patch_intent.get("executor_route_hint"))
    if route_hint and route_hint != route:
        return [f"patch_intent_route_hint_mismatch:{route_hint}"]
    write_scope = target_files or [_normalize(patch_intent.get("target_file"))]
    validation_errors = validate_patch_intent_payload(patch_intent, write_scope)
    if validation_errors:
        return [f"patch_intent_validation:{error}" for error in validation_errors]
    return []


def _validate_rerun_contract_for_self_repair(route: str, repair_type: str, plan: Dict[str, Any]) -> List[str]:
    if route not in {"deterministic_self", "bounded_self_repair"}:
        return []
    rerun_intent = plan.get("rerun_intent")
    if not isinstance(rerun_intent, dict):
        return ["rerun_intent_missing"]

    downstream_task_id = _normalize(rerun_intent.get("downstream_task_id"))
    if not downstream_task_id:
        return ["rerun_intent_missing_downstream_task_id"]

    upstream_source_repair_task_id = _normalize(rerun_intent.get("upstream_source_repair_task_id"))
    if repair_type in SELF_REPAIR_REQUIRE_UPSTREAM_TASK and not upstream_source_repair_task_id:
        return ["rerun_intent_missing_upstream_source_repair_task_id"]
    return []


def select_repair_executor(
    source_repair_plan: Dict[str, Any],
    *,
    repair_type: str | None = None,
    target_files: List[str] | None = None,
    available_tests: List[str] | None = None,
    attempt_count: int = 0,
    requires_high_cost_gpu: bool = False,
    missing_approval: bool = False,
    reward_hack_markers: List[str] | None = None,
) -> Dict[str, Any]:
    """Route a repair plan to deterministic self, bounded self, Codex, human, or stop.

    The selector is advisory-only. It never applies patches, creates tasks, edits
    source, dispatches work, mutates registry state, or launches remote execution.
    """

    plan = dict(source_repair_plan or {})
    selected_type = _normalize_lower(repair_type) or _infer_repair_type(plan)
    source_task_id = _normalize(plan.get("source_task_id")) or "unknown_source_task"
    files = _target_files(plan, target_files)
    tests = sorted(set(_as_str_list(available_tests)))
    markers = _plan_reward_markers(plan, _as_str_list(reward_hack_markers))
    payload_hash = _sha256_hex(
        {
            "source_repair_plan": plan,
            "repair_type": selected_type,
            "target_files": files,
            "available_tests": tests,
            "attempt_count": attempt_count,
            "requires_high_cost_gpu": requires_high_cost_gpu,
            "missing_approval": missing_approval,
            "reward_hack_markers": markers,
        },
    )

    reasons: List[str] = []
    blockers: List[str] = []
    route = _classify_route(selected_type)

    if missing_approval:
        route = "human"
        blockers.append("missing_approval")
    if requires_high_cost_gpu:
        route = "human"
        blockers.append("requires_high_cost_gpu_without_budget")
    if attempt_count >= DEFAULT_BUDGET["max_attempts_per_failure"]:
        route = "stop"
        blockers.append("repeated_failure_same_type_after_one_repair")

    forbidden_targets = _has_forbidden_target(files)
    if forbidden_targets and route in {"deterministic_self", "bounded_self_repair"}:
        route = "codex"
        blockers.append("target_file_forbidden_for_self_repair")

    reward_violations = _reward_hack_violations(markers)
    if reward_violations:
        route = "human"
        blockers.append("reward_hack_risk")
        for violation in reward_violations:
            blockers.append(f"{violation}_forbidden")

    patch_diff_lines = _estimated_repair_change_lines(plan)
    if route in {"deterministic_self", "bounded_self_repair"}:
        if len(files) > DEFAULT_BUDGET["max_files_changed"]:
            route = "codex"
            blockers.append("self_repair_file_budget_exceeded")
        if len(files) > DEFAULT_BUDGET["max_new_files"]:
            route = "codex"
            blockers.append("self_repair_new_file_budget_exceeded")
        if patch_diff_lines is not None:
            if patch_diff_lines > DEFAULT_BUDGET["max_diff_lines_total"]:
                route = "codex"
                blockers.append("self_repair_diff_budget_exceeded")
            if files and patch_diff_lines > DEFAULT_BUDGET["max_diff_lines_per_file"]:
                route = "codex"
                blockers.append("self_repair_per_file_diff_budget_exceeded")

    patch_intent_validation_errors = _validate_self_patch_intent(route, selected_type, plan, files)
    if patch_intent_validation_errors:
        route = "codex"
        blockers.extend(patch_intent_validation_errors)

    rerun_contract_validation_errors = _validate_rerun_contract_for_self_repair(route, selected_type, plan)
    if rerun_contract_validation_errors:
        route = "codex"
        blockers.extend(rerun_contract_validation_errors)

    required_tests = REQUIRED_TESTS_BY_REPAIR_TYPE.get(selected_type, [])
    missing_tests = sorted(set(required_tests) - set(tests))
    unsafe_targets = _unsafe_self_targets(files, selected_type)
    if route in {"deterministic_self", "bounded_self_repair"}:
        if missing_tests:
            route = "codex"
            blockers.append("required_validation_tests_missing")
        elif unsafe_targets:
            route = "codex"
            blockers.append("target_file_not_allowlisted_for_self_repair")
        elif selected_type == "wrapper_missing_with_template" and not any("wrapper" in test for test in tests):
            route = "codex"
            blockers.append("wrapper_template_validation_missing")

    if route == "deterministic_self":
        confidence = "high"
        reasons.append("repair_type_is_template_or_schema_only")
    elif route == "bounded_self_repair":
        confidence = "high"
        reasons.append("repair_type_is_low_risk_bounded_and_testable")
    elif route == "codex":
        confidence = "medium"
        reasons.append("repair_requires_source_behavior_or_non_allowlisted_change")
    elif route == "human":
        confidence = "medium"
        reasons.append("repair_requires_human_decision_or_policy_authority")
    else:
        confidence = "high"
        reasons.append("safe_progression_exhausted_or_repeated_failure")

    preflight_required = [
        "clean_or_disposable_worktree",
        "repair_type_allowlisted",
        "target_files_allowlisted",
        "required_tests_available",
        "no_approval_or_data_uncertainty",
        "patch_budget_available",
    ]
    if route not in {"deterministic_self", "bounded_self_repair"}:
        preflight_required = []

    return {
        "schema_version": REPAIR_EXECUTOR_SCHEMA_VERSION,
        "repair_id": _repair_id(source_task_id, selected_type, payload_hash),
        "source_task_id": source_task_id,
        "failure_type": _normalize(plan.get("trigger_kind") or plan.get("status")),
        "repair_type": selected_type,
        "attempt_count": attempt_count,
        "patch_intent_validation_errors": patch_intent_validation_errors,
        "decision": {
            "route": route,
            "confidence": confidence,
            "reason": "; ".join(reasons),
        },
        "budget": dict(DEFAULT_BUDGET),
        "target_files": files,
        "required_tests": required_tests,
        "available_tests": tests,
        "missing_tests": missing_tests,
        "preflight_required": preflight_required,
        "blockers": sorted(set(blockers)),
        "forbidden": [
            "source_behavior_change",
            "approval_relaxation",
            "test_deletion",
            "required_metric_downgrade",
            "required_output_downgrade",
            "policy_relaxation",
            "claim_upgrade_without_evidence",
            "scheduler_dispatch",
            "registry_state_mutation",
            "remote_execution",
        ],
        "reward_hack_checks": {
            "violations": reward_violations,
            "forbid_test_deletion": True,
            "forbid_required_metric_downgrade": True,
            "forbid_required_output_downgrade": True,
            "forbid_policy_relaxation": True,
            "forbid_approval_relaxation": True,
            "forbid_claim_upgrade_without_evidence": True,
            "forbid_self_modifying_gate": True,
        },
        "on_success": {
            "produce": ["repair_result.yaml", "rerun_intent.yaml"],
            "dispatch": False,
        },
        "on_failure": {
            "status": "hold",
            "route": "codex" if route in {"deterministic_self", "bounded_self_repair"} else route,
            "max_self_repair_attempts": DEFAULT_BUDGET["max_attempts_per_failure"],
        },
        "side_effects": {
            "patch_applied": False,
            "source_edit": False,
            "task_created": False,
            "inbox_write": False,
            "dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }


def write_repair_execution_artifacts(
    decision: Dict[str, Any],
    *,
    source_repair_plan: Dict[str, Any] | None = None,
    output_dir: Path | str,
) -> Dict[str, Any]:
    output_path = _coerce_path(output_dir, str(Path.cwd())).resolve()
    plan = dict(source_repair_plan or {})
    repair_result_path = output_path / REPAIR_RESULT_FILENAME
    rerun_intent_path = output_path / RERUN_INTENT_FILENAME
    repair_result_payload = _build_repair_result_payload(decision, plan=plan)
    rerun_intent_payload = _build_rerun_intent_payload(decision, plan=plan)
    output_path.mkdir(parents=True, exist_ok=True)

    output_repair = _read_payload(repair_result_path)
    output_rerun = _read_payload(rerun_intent_path)
    outputs_exist = any(path.exists() for path in (repair_result_path, rerun_intent_path))
    all_outputs_exist = repair_result_path.exists() and rerun_intent_path.exists()

    if outputs_exist:
        if not all_outputs_exist:
            return {
                "status": "blocked",
                "reason": "repair_execution_artifact_set_inconsistent",
                "repair_result_path": str(repair_result_path),
                "rerun_intent_path": str(rerun_intent_path),
            }
        if (
            output_repair == repair_result_payload
            and output_rerun == rerun_intent_payload
        ):
            return {
                "status": "replayed",
                "repair_result_path": str(repair_result_path),
                "rerun_intent_path": str(rerun_intent_path),
            }
        return {
            "status": "blocked",
            "reason": "repair_execution_artifacts_collision",
            "repair_result_path": str(repair_result_path),
            "rerun_intent_path": str(rerun_intent_path),
        }

    repair_result_path.write_text(json.dumps(repair_result_payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rerun_intent_path.write_text(json.dumps(rerun_intent_payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "written",
        "repair_result_path": str(repair_result_path),
        "rerun_intent_path": str(rerun_intent_path),
    }


def read_repair_result_artifact(output_dir: Path | str) -> Dict[str, Any] | None:
    return _read_payload(_coerce_path(output_dir, str(Path.cwd())).resolve() / REPAIR_RESULT_FILENAME)


def read_rerun_intent_artifact(output_dir: Path | str) -> Dict[str, Any] | None:
    return _read_payload(_coerce_path(output_dir, str(Path.cwd())).resolve() / RERUN_INTENT_FILENAME)


def write_repair_executor_decision(decision: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(decision, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        if existing == payload:
            return {"status": "replayed", "path": str(output_path)}
        return {"status": "blocked", "reason": "repair_executor_decision_collision", "path": str(output_path)}
    output_path.write_text(payload, encoding="utf-8")
    return {"status": "written", "path": str(output_path)}


__all__ = [
    "select_repair_executor",
    "write_repair_executor_decision",
    "write_repair_execution_artifacts",
    "read_repair_result_artifact",
    "read_rerun_intent_artifact",
]

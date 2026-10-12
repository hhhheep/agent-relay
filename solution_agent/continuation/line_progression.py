from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


LINE_PROGRESSION_SCHEMA_VERSION = 1

TERMINAL_STATUSES = {"completed", "succeeded", "done"}
PARTIAL_STATUSES = {"partial", "completed_with_warning", "warning"}
BLOCKING_REASONS = {
    "needs_user_decision",
    "no_safe_continuation",
    "theory_exhausted",
    "unsupported_claim",
    "policy_blocked",
}
REPAIR_REASONS = {
    "missing_capability",
    "source_code_required",
    "needs_bounded_smoke_runner",
    "codex_debug_request",
    "request_codex_review",
}
ARTIFACT_REASONS = {
    "missing_artifact",
    "missing_cache",
    "missing_dataset",
    "missing_input",
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _coerce_distinct_list(value: Any) -> List[str]:
    return sorted({_normalize(item) for item in _as_list(value) if _normalize(item)})


def _coerce_repair_artifact(value: Any, *, context: str, reasons: List[str]) -> Dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = dict(value)
    elif isinstance(value, (str, Path)):
        artifact_path = Path(value)
        try:
            if not artifact_path.exists():
                reasons.append(f"{context}_artifact_missing")
                return None
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append(f"{context}_artifact_invalid")
            return None
        if not isinstance(payload, dict):
            reasons.append(f"{context}_artifact_invalid")
            return None
    else:
        reasons.append(f"{context}_artifact_invalid")
        return None

    payload = dict(payload)
    if "route" in payload:
        payload["route"] = _normalize(payload.get("route")).lower()
    if "repair_type" in payload:
        payload["repair_type"] = _normalize(payload.get("repair_type")).lower()
    if "execution_status" in payload:
        payload["execution_status"] = _normalize(payload.get("execution_status")).lower()
    return payload


def _coerce_bounded_validation_readiness(
    value: Any,
    *,
    reasons: List[str],
) -> Dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = dict(value)
    elif isinstance(value, (str, Path)):
        artifact_path = Path(value)
        try:
            if not artifact_path.exists():
                reasons.append("bounded_validation_readiness_artifact_missing")
                return None
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append("bounded_validation_readiness_artifact_invalid")
            return None
        if not isinstance(payload, dict):
            reasons.append("bounded_validation_readiness_artifact_invalid")
            return None
    else:
        reasons.append("bounded_validation_readiness_artifact_invalid")
        return None

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    artifact_statuses = summary.get("artifact_statuses") if isinstance(summary.get("artifact_statuses"), dict) else {}
    return {
        "bucket_id": _normalize(payload.get("bucket_id")) or _normalize(summary.get("bucket_id")),
        "status": _normalize(payload.get("status") or summary.get("status")).lower(),
        "next_recommendation": _normalize(
            payload.get("next_recommendation") or summary.get("next_recommendation")
        ).lower(),
        "present_valid_artifact_ids": _coerce_distinct_list(summary.get("present_valid_artifact_ids")),
        "missing_artifact_ids": _coerce_distinct_list(summary.get("missing_artifact_ids")),
        "invalid_artifact_ids": _coerce_distinct_list(summary.get("invalid_artifact_ids")),
        "route_reasons": _coerce_distinct_list(summary.get("route_reasons")),
        "non_valid_artifact_ids": _coerce_distinct_list(summary.get("non_valid_artifact_ids")),
        "artifact_statuses": {
            _normalize(key): _normalize(item).lower()
            for key, item in artifact_statuses.items()
            if _normalize(key) and _normalize(item)
        },
        "reason_count": int(summary.get("reason_count") or len(_coerce_distinct_list(payload.get("reasons")))),
    }


def _action_kinds(action_requests: Any) -> List[str]:
    kinds = []
    for item in _as_list(action_requests):
        if isinstance(item, dict):
            kinds.append(_normalize(item.get("kind")).lower())
        else:
            kinds.append("")
    return [kind for kind in kinds if kind]


def _has_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def assess_line_progression(
    *,
    source_task_id: str,
    decision_summary: Dict[str, Any] | None = None,
    action_requests: Any = None,
    source_repair_plan: Dict[str, Any] | None = None,
    continuation_candidates: List[Dict[str, Any]] | None = None,
    repair_result: Dict[str, Any] | Path | str | None = None,
    rerun_intent: Dict[str, Any] | Path | str | None = None,
    validation_readiness_report: Dict[str, Any] | Path | str | None = None,
) -> Dict[str, Any]:
    """Decide whether a research line can safely continue after a result.

    This helper is intentionally deterministic and advisory-only. It does not
    create tasks, edit source, dispatch work, mutate registry state, or call APIs.
    """

    decision = decision_summary or {}
    repair_plan = source_repair_plan or {}
    candidates = continuation_candidates or []
    status = _normalize(decision.get("status") or decision.get("execution_status")).lower()
    reason = _normalize(decision.get("reason") or decision.get("evidence_status")).lower()
    next_action = _normalize(decision.get("next_action"))
    kinds = _action_kinds(action_requests)
    repair_needed = _has_true(repair_plan.get("needs_source_repair"))
    repair_blockers = _coerce_distinct_list(repair_plan.get("blocked_by"))
    source_repair_stageable = repair_needed and not repair_blockers
    is_repair_signal = reason in REPAIR_REASONS or any(kind in REPAIR_REASONS for kind in kinds)
    source_repair_plan_missing = is_repair_signal and not bool(source_repair_plan)

    repair_artifact_blockers: List[str] = []
    validation_readiness_reasons: List[str] = []
    validation_readiness = _coerce_bounded_validation_readiness(
        validation_readiness_report,
        reasons=validation_readiness_reasons,
    )
    if repair_needed or is_repair_signal:
        repair_result_payload = _coerce_repair_artifact(
            repair_result,
            context="repair_result",
            reasons=repair_artifact_blockers,
        )
        rerun_intent_payload = _coerce_repair_artifact(
            rerun_intent,
            context="rerun_intent",
            reasons=repair_artifact_blockers,
        )
        if repair_result_payload and not rerun_intent_payload:
            repair_artifact_blockers.append("repair_rerun_artifact_missing")
        if rerun_intent_payload and not repair_result_payload:
            repair_artifact_blockers.append("repair_result_artifact_missing")
        if repair_result_payload:
            validation_errors = _coerce_distinct_list(repair_result_payload.get("patch_intent_validation_errors"))
            if validation_errors:
                repair_artifact_blockers.append("repair_result_patch_intent_validation_errors")
                repair_artifact_blockers.extend(
                    f"repair_result_patch_intent_validation:{error}" for error in validation_errors
                )
            execution_status = _normalize(repair_result_payload.get("execution_status")).lower()
            execution_blockers = _coerce_distinct_list(repair_result_payload.get("execution_blockers"))
            if execution_status == "blocked":
                repair_artifact_blockers.append("repair_result_execution_blocked")
                repair_artifact_blockers.extend(
                    f"repair_result_execution:{blocker}" for blocker in execution_blockers
                )
            elif execution_status and execution_status not in {"applied", "externally_applied", "not_attempted"}:
                repair_artifact_blockers.append(f"repair_result_execution_status_invalid:{execution_status}")
                repair_artifact_blockers.append(f"repair_result_execution_status:{execution_status}")
        if repair_result_payload and rerun_intent_payload:
            repair_result_id = _normalize(repair_result_payload.get("repair_id"))
            rerun_repair_id = _normalize(rerun_intent_payload.get("repair_id"))
            if repair_result_id and rerun_repair_id and repair_result_id != rerun_repair_id:
                repair_artifact_blockers.append("repair_result_rerun_id_mismatch")

    source_repair_stageable = repair_needed and not repair_blockers and not repair_artifact_blockers

    reasons: List[str] = []
    next_step = "stop_no_safe_continuation"
    continue_allowed = False
    requires_human = False
    next_step_can_stage = False
    next_step_blockers: List[str] = []

    if reason in BLOCKING_REASONS or any(kind in BLOCKING_REASONS for kind in kinds):
        reasons.append(reason or "blocked_by_action_request")
        requires_human = reason == "needs_user_decision" or "needs_user_decision" in kinds
        next_step = "ask_human" if requires_human else "stop_no_safe_continuation"
    elif repair_needed or is_repair_signal:
        reasons.append("source_repair_required")
        if source_repair_stageable:
            continue_allowed = True
            next_step_can_stage = True
            next_step = "continue_with_bounded_source_repair_then_rerun"
        else:
            if source_repair_plan_missing:
                reasons.append("source_repair_plan_missing")
                continue_allowed = True
                next_step_can_stage = True
                next_step = "continue_with_source_repair_planning"
                next_step_blockers.append("source_repair_plan_missing")
            elif repair_artifact_blockers:
                reasons.append("source_repair_artifact_invalid")
                next_step_blockers.append("source_repair_artifact_invalid")
                requires_human = True
                next_step = "ask_human"
            else:
                reasons.append("source_repair_plan_blocked")
                requires_human = True
                next_step = "ask_human"
            reasons.extend(repair_artifact_blockers)
            next_step_blockers.extend(repair_blockers)
            next_step_blockers.extend(repair_artifact_blockers)
    elif reason in ARTIFACT_REASONS or any(kind in ARTIFACT_REASONS for kind in kinds):
        reasons.append("artifact_or_inventory_completion_required")
        continue_allowed = True
        next_step = "continue_with_inventory_or_metric_completion"
    elif candidates:
        stageable = [
            item
            for item in candidates
            if item.get("can_stage") is True
            and _normalize(item.get("kind")) not in {"", "no_safe_continuation"}
        ]
        if stageable:
            reasons.append("stageable_continuation_candidate_available")
            continue_allowed = True
            next_step_can_stage = True
            next_step = "continue_with_staged_continuation_candidate"
        else:
            reasons.append("only_non-stageable_or_no_safe_candidates")
            next_step = "stop_no_safe_continuation"
    elif status in PARTIAL_STATUSES and next_action:
        reasons.append("partial_result_has_next_action")
        continue_allowed = True
        next_step = "continue_with_bounded_followup"
    elif status in TERMINAL_STATUSES and next_action:
        reasons.append("completed_result_has_next_action")
        continue_allowed = True
        next_step = "continue_with_bounded_followup"
    elif status in TERMINAL_STATUSES:
        reasons.append("terminal_result_without_next_safe_action")
        next_step = "stop_line_complete_or_theory_exhausted"
    else:
        reasons.append("insufficient_progression_evidence")
        requires_human = True
        next_step = "ask_human"

    result = {
        "schema_version": LINE_PROGRESSION_SCHEMA_VERSION,
        "source_task_id": source_task_id,
        "status": "continue" if continue_allowed else "hold",
        "continue_allowed": continue_allowed,
        "next_step": next_step,
        "requires_human": requires_human,
        "reasons": sorted({item for item in reasons if item}),
        "claim_limit": "advisory_progression_only",
        "next_step_readiness": {
            "can_stage": next_step_can_stage,
            "blockers": sorted(set(next_step_blockers)),
        },
        "side_effects": {
            "task_created": False,
            "source_edit": False,
            "inbox_write": False,
            "dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }
    if validation_readiness_reasons:
        result["bounded_validation_readiness"] = {
            "status": "unavailable",
            "reasons": sorted(set(validation_readiness_reasons)),
        }
    elif validation_readiness:
        result["bounded_validation_readiness"] = validation_readiness
        result["next_step_readiness"]["bounded_validation"] = validation_readiness
    return result


__all__ = [
    "assess_line_progression",
    "_coerce_bounded_validation_readiness",
]

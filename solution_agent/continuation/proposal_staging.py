from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gate import review_draft_payload_stub, review_draft_text_stub
from .knowledge_to_proposal import validate_proposal_candidate
from .ledger import append_continuation_run_event
from .night_auto_pending_staging import write_night_auto_pending_staging_artifact
from .night_auto_work_order import write_night_auto_work_order_dry_run
from .paths import (
    continuation_staged_proposal_candidate_dir,
    continuation_staged_proposal_candidate_report_path,
)
from .policy import load_planner_policy

STAGE_SCHEMA_VERSION = 1
PROPOSAL_STAGING_CYCLE_ID = "2026-05-19T_SPARK_P4E_PROPOSAL_STAGING_01"

REQUIRED_SIDE_EFFECT_FALSE = (
    "dispatch",
    "promote",
    "registry_mutation",
    "remote_execution",
    "running_state",
    "subprocess",
    "scheduler_submit",
    "scheduler_dispatch",
    "remote_write",
    "source_edit",
    "write_inbox",
)

FORBIDDEN_TOP_LEVEL_FIELDS = (
    "dispatch",
    "promote",
    "registry_mutation",
    "remote_execution",
    "running_state",
    "subprocess",
    "scheduler_submit",
    "scheduler_dispatch",
    "remote_write",
    "write_inbox",
    "source_edit",
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = _normalize_lower(value)
    if text in {"", "0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _coerce_repair_artifact(value: Any, *, context: str, reasons: List[str]) -> Dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = dict(value)
        if "route" in payload:
            payload["route"] = _normalize_lower(payload.get("route"))
        if "repair_type" in payload:
            payload["repair_type"] = _normalize_lower(payload.get("repair_type"))
        if "execution_status" in payload:
            payload["execution_status"] = _normalize_lower(payload.get("execution_status"))
        return payload
    if isinstance(value, (str, Path)):
        artifact_path = Path(value)
        try:
            if not artifact_path.exists():
                reasons.append(f"candidate_{context}_artifact_missing")
                return None
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append(f"candidate_{context}_artifact_unreadable")
            return None
        if not isinstance(payload, dict):
            reasons.append(f"candidate_{context}_artifact_invalid_format")
            return None
        if "route" in payload:
            payload["route"] = _normalize_lower(payload.get("route"))
        if "repair_type" in payload:
            payload["repair_type"] = _normalize_lower(payload.get("repair_type"))
        if "execution_status" in payload:
            payload["execution_status"] = _normalize_lower(payload.get("execution_status"))
        return payload
    reasons.append(f"candidate_{context}_artifact_invalid")
    return None


def _coerce_apply_readiness(value: Any, reasons: List[str]) -> Dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        reasons.append("candidate_apply_readiness_invalid")
        return None
    status = _normalize_lower(value.get("status"))
    if status not in {"", "ready", "blocked", "unknown", "partial"}:
        reasons.append("candidate_apply_readiness_status_invalid")
    can_apply = _coerce_bool(value.get("can_apply"), default=False)
    blockers = _coerce_str_list(value.get("blockers"))
    if blockers:
        reasons.extend(f"candidate_apply_readiness_blocker:{item}" for item in blockers)
    if status != "ready" or blockers or not can_apply:
        reasons.append("candidate_apply_readiness_blocked")
    return {
        "status": status,
        "can_apply": can_apply,
        "route": _normalize_lower(value.get("route")),
        "blockers": blockers,
    }


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _normalize(value)
        return [normalized] if normalized else []
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value if _normalize(item)]
    return [_normalize(value)]


def _safe_path_value(value: str) -> str:
    value = _normalize(value)
    for marker in ("..", "/", "\\"):
        value = value.replace(marker, "_")
    return value or "unknown"


def _read_payload(candidate: dict | Path | str) -> Dict[str, Any]:
    if isinstance(candidate, (str, Path)):
        loaded = json.loads(Path(candidate).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("candidate payload must be a JSON object")
        return loaded
    if isinstance(candidate, dict):
        return dict(candidate)
    raise TypeError("candidate payload must be a dict, Path, or path string")


def _current_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _allowed_kinds() -> List[str]:
    return [_normalize_lower(item) for item in load_planner_policy().get("allowed_next_step_kinds", []) if _normalize(item)]


def _build_proposal_run_id(candidate: Dict[str, Any]) -> str:
    context = {
        "source_council_id": _normalize(candidate.get("source_council_id")),
        "source_task_id": _normalize(candidate.get("source_task_id")),
        "source_knowledge_pack_id": _normalize(candidate.get("source_knowledge_pack_id")),
        "line": _normalize(candidate.get("line")),
        "workplace_profile": _normalize(candidate.get("workplace_profile")),
        "kind": _normalize_lower(candidate.get("kind")),
        "actor": _normalize(candidate.get("actor")),
    }
    return f"pc_{_sha256_hex(context)[:16]}"


def _build_candidate_id(candidate: Dict[str, Any]) -> str:
    if _normalize(candidate.get("candidate_id")):
        return _safe_path_value(_normalize(candidate.get("candidate_id")))
    source_task = _normalize(candidate.get("source_task_id")) or "unknown_task"
    fallback_context = {
        "source_task_id": source_task,
        "kind": _normalize_lower(candidate.get("kind")),
        "line": _normalize(candidate.get("line")),
        "workplace_profile": _normalize(candidate.get("workplace_profile")),
        "reason": _normalize(candidate.get("reason")),
    }
    return f"{_safe_path_value(source_task)}_{_safe_path_value(_normalize_lower(candidate.get('kind')))}_{_sha256_hex(fallback_context)[:10]}"


def _needs_line_and_workplace(kind: str) -> bool:
    return _normalize_lower(kind) not in {"", "no_safe_continuation"}


def _collect_stage_blockers(candidate: Dict[str, Any], *, raw_candidate: Dict[str, Any] | None, actor: str) -> List[str]:
    validated = candidate
    normalized_kind = _normalize_lower(validated.get("kind"))
    raw = dict(raw_candidate or {})
    allowed_kinds = set(_allowed_kinds())
    reasons: List[str] = []
    continuation_lane = _normalize_lower(validated.get("continuation_lane"))

    if normalized_kind not in allowed_kinds:
        reasons.append(f"invalid_kind:{normalized_kind or 'missing'}")
    if continuation_lane == "night_auto":
        if not _normalize(validated.get("mission_id")):
            reasons.append("candidate_missing_mission_id")
        if not _normalize(validated.get("parent_decision_id")):
            reasons.append("candidate_missing_parent_decision_id")
        if not _coerce_bool(validated.get("mission_gate_pass"), default=False):
            reasons.append("candidate_night_auto_requires_mission_gate_pass")
    if normalized_kind == "no_safe_continuation":
        reasons.append("no_safe_continuation_not_stageable")
    if _normalize(validated.get("candidate_id")) == "":
        reasons.append("candidate_missing_candidate_id")
    if _normalize(validated.get("status")).lower() not in {"proposal_candidate_created", "proposal_candidate_recommended", ""}:
        reasons.append("candidate_status_rejected")
    if not _coerce_bool(validated.get("can_stage"), default=False):
        reasons.append("candidate_can_stage_false")
    if _normalize(raw.get("reason")) == "":
        reasons.append("candidate_missing_reason")
    if not _coerce_str_list(raw.get("expected_outputs")):
        reasons.append("candidate_missing_expected_outputs")
    if _normalize(raw.get("worker")) == "":
        reasons.append("candidate_missing_worker")
    if not _coerce_str_list(raw.get("claim_limitations")):
        reasons.append("candidate_missing_claim_limitations")
    if _needs_line_and_workplace(normalized_kind):
        if _normalize(raw.get("line")) == "":
            reasons.append("candidate_missing_line")
        if _normalize(raw.get("workplace_profile")) == "":
            reasons.append("candidate_missing_workplace_profile")

    side_effects = raw.get("side_effects")
    if not isinstance(side_effects, dict):
        side_effects = validated.get("side_effects")
    if not isinstance(side_effects, dict):
        reasons.append("candidate_missing_side_effects")
    else:
        for key in REQUIRED_SIDE_EFFECT_FALSE:
            if _coerce_bool(side_effects.get(key), default=False):
                reasons.append(f"forbidden_side_effect_true:{key}")

    for field in FORBIDDEN_TOP_LEVEL_FIELDS:
        if _coerce_bool(raw.get(field), default=False):
            reasons.append(f"forbidden_field:{field}")

    raw_repair_result = raw.get("repair_result")
    raw_rerun_intent = raw.get("rerun_intent")
    readiness = validated.get("next_step_readiness")
    if readiness is None and (raw_repair_result or raw_rerun_intent):
        reasons.append("candidate_missing_next_step_readiness_for_repair_artifacts")
    elif isinstance(readiness, dict):
        readiness_blockers = _coerce_str_list(readiness.get("blockers"))
        if not _coerce_bool(readiness.get("can_stage"), default=True) or readiness_blockers:
            reasons.append("next_step_not_stageable_by_progression")
        reasons.extend(f"next_step_readiness_blocker:{item}" for item in readiness_blockers)
    elif readiness is not None:
        reasons.append("candidate_next_step_readiness_invalid")
    repair_result_payload = _coerce_repair_artifact(
        validated.get("repair_result"),
        context="repair_result",
        reasons=reasons,
    )
    rerun_intent_payload = _coerce_repair_artifact(
        validated.get("rerun_intent"),
        context="rerun_intent",
        reasons=reasons,
    )
    if repair_result_payload is not None:
        candidate["repair_result"] = repair_result_payload
    if rerun_intent_payload is not None:
        candidate["rerun_intent"] = rerun_intent_payload
    if repair_result_payload and not rerun_intent_payload:
        reasons.append("candidate_missing_rerun_intent_artifact")
    if rerun_intent_payload and not repair_result_payload:
        reasons.append("candidate_missing_repair_result_artifact")
    if repair_result_payload:
        validation_errors = _coerce_str_list(repair_result_payload.get("patch_intent_validation_errors"))
        if validation_errors:
            reasons.append("candidate_repair_result_has_patch_intent_validation_errors")
            reasons.extend(f"repair_result_patch_intent_validation:{error}" for error in validation_errors)
        execution_status = _normalize_lower(repair_result_payload.get("execution_status"))
        execution_blockers = _coerce_str_list(repair_result_payload.get("execution_blockers"))
        if execution_status == "blocked":
            reasons.append("candidate_repair_result_execution_blocked")
            reasons.extend(f"repair_result_execution:{item}" for item in execution_blockers)
        elif execution_status and execution_status not in {"applied", "externally_applied", "not_attempted"}:
            reasons.append(f"candidate_repair_result_execution_status_invalid:{execution_status}")
            reasons.append(f"repair_result_execution_status:{execution_status}")
    if repair_result_payload and rerun_intent_payload:
        repair_result_id = _normalize(repair_result_payload.get("repair_id"))
        rerun_repair_id = _normalize(rerun_intent_payload.get("repair_id"))
        if repair_result_id and rerun_repair_id and repair_result_id != rerun_repair_id:
            reasons.append("candidate_repair_artifact_repair_id_mismatch")
    apply_readiness_payload = _coerce_apply_readiness(validated.get("apply_readiness"), reasons=reasons)
    if apply_readiness_payload is not None:
        candidate["apply_readiness"] = apply_readiness_payload

    if not _normalize(actor):
        reasons.append("actor_missing")
    return sorted(set(reasons))


def assess_proposal_candidate_for_staging(
    candidate: dict | Path | str,
    *,
    actor: str = "system",
    raw_candidate: dict | Path | str | None = None,
) -> dict:
    payload = _read_payload(candidate)
    candidate_payload = validate_proposal_candidate(payload)
    raw = payload if raw_candidate is None else _read_payload(raw_candidate)
    reasons = _collect_stage_blockers(candidate_payload, raw_candidate=raw, actor=actor)
    kind = _normalize_lower(candidate_payload.get("kind"))
    requires_human_approval = _coerce_bool(candidate_payload.get("requires_human_approval"), default=False)
    candidate_stageable = (
        _normalize_lower(candidate_payload.get("status")) == "proposal_candidate_created"
        and _coerce_bool(candidate_payload.get("can_stage"), default=False)
        and kind not in {"", "no_safe_continuation"}
        and "no_safe_continuation_not_stageable" not in reasons
        and not [
            reason
            for reason in reasons
            if reason != "candidate_missing_candidate_id"
        ]
    )
    codex_draft_only = kind == "codex_debug_request"
    assessment_status = "stageable" if candidate_stageable else ("draft_only" if codex_draft_only else "manual_review")

    return {
        "schema_version": STAGE_SCHEMA_VERSION,
        "status": assessment_status,
        "assessment_status": assessment_status,
        "candidate_id": _build_candidate_id(candidate_payload),
        "candidate": candidate_payload,
        "actor": _normalize(actor) or "system",
        "stageable": bool(candidate_stageable),
        "draft_only": bool(codex_draft_only),
        "requires_human_approval": bool(requires_human_approval),
        "reasons": reasons,
    }


def _source_result_hash(candidate: Dict[str, Any], proposal_run_id: str, candidate_id: str) -> str:
    source_task_id = _normalize(candidate.get("source_task_id")) or "unknown_task"
    return _sha256_hex(
        {
            "proposal_run_id": proposal_run_id,
            "candidate_id": candidate_id,
            "source_task_id": source_task_id,
            "kind": _normalize_lower(candidate.get("kind")),
        }
    )[:24]


def _build_draft_payload(candidate: Dict[str, Any], proposal_run_id: str) -> Dict[str, Any]:
    candidate_id = _build_candidate_id(candidate)
    source_task_id = _normalize(candidate.get("source_task_id")) or "unknown_task"
    source_result_hash_value = _source_result_hash(candidate, proposal_run_id, candidate_id)
    kind = _normalize_lower(candidate.get("kind"))
    reason = _normalize(candidate.get("reason")) or "Proposal candidate requires local staging review."
    claim_limitations = _coerce_str_list(candidate.get("claim_limitations"))
    claim_text = "; ".join(claim_limitations) if claim_limitations else "No claim limitations provided."
    return {
        "schema_version": STAGE_SCHEMA_VERSION,
        "draft_id": candidate_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash_value,
        "reason_for_continuation": reason,
        "goal": f"Review and stage proposal candidate {candidate_id}.",
        "scope": "proposal_candidate_staging",
        "allowed_actions": ["local_review_only", "request_handoff"],
        "forbidden_actions": _coerce_str_list(candidate.get("forbidden_actions")) or [],
        "expected_outputs": _coerce_str_list(candidate.get("expected_outputs")),
        "claim_limitation": (
            f"{claim_text} Local staging only; active proposal writes, promotion, scheduler dispatch, "
            "registry mutation, and source edits are forbidden."
        ),
        "idempotency_key": f"{proposal_run_id}:{candidate_id}:{source_result_hash_value}",
        "proposal_kind": kind,
        "requires_human_approval": bool(candidate.get("requires_human_approval")),
        "worker": _normalize(candidate.get("worker")) or "unknown",
        "line": _normalize(candidate.get("line")),
        "workplace_profile": _normalize(candidate.get("workplace_profile")),
    }


def _build_draft_markdown(candidate: Dict[str, Any], proposal_run_id: str) -> str:
    candidate_id = _build_candidate_id(candidate)
    claim_limitations = _coerce_str_list(candidate.get("claim_limitations"))
    expected_outputs = _coerce_str_list(candidate.get("expected_outputs"))

    front_matter = "\n".join(
        [
            "---",
            f"candidate_id: {candidate_id}",
            f"proposal_run_id: {proposal_run_id}",
            f"kind: {_normalize_lower(candidate.get('kind'))}",
            f"line: {_normalize(candidate.get('line')) or 'unknown'}",
            f"workplace_profile: {_normalize(candidate.get('workplace_profile')) or 'unknown'}",
            f"requires_human_approval: {str(_coerce_bool(candidate.get('requires_human_approval'), default=False)).lower()}",
            "scope: proposal_candidate_staging",
            "status: draft",
            "---",
            "",
        ]
    )
    body = [
        f"# Proposal Candidate Draft: {candidate_id}",
        "",
        f"Source task: {_normalize(candidate.get('source_task_id')) or 'unknown_task'}",
        f"Worker: {_normalize(candidate.get('worker')) or 'unknown'}",
        f"Reason: {_normalize(candidate.get('reason')) or 'not provided'}",
        "",
        "Execution mode: local-only staging review. No active inbox writes, no dispatch, "
        "no promotion, no source edits.",
        "",
        "Expected outputs:",
    ]
    body.extend([f"- {item}" for item in expected_outputs] or ["- not provided"])
    body.append("")
    body.append("Claim limitations:")
    body.extend([f"- {item}" for item in claim_limitations] or ["- not provided"])
    body.append("")
    body.append("Forbidden actions:")
    body.extend([f"- {item}" for item in _coerce_str_list(candidate.get("forbidden_actions"))] or ["- none"])
    body.append("")
    return front_matter + "\n".join(body) + "\n"


def _candidate_write_records(outputs: Dict[str, str]) -> Dict[str, Any]:
    if not outputs:
        return {
            "created": False,
            "replayed": False,
            "status": "skipped",
            "reason": "empty_output_set",
            "existing": {},
        }

    resolved = {Path(path): content for path, content in outputs.items()}
    for path in resolved:
        path.parent.mkdir(parents=True, exist_ok=True)

    existing = {path: path.read_text(encoding="utf-8") if path.exists() else None for path in resolved}

    if all(value is None for value in existing.values()):
        for path, content in resolved.items():
            path.write_text(content, encoding="utf-8")
        return {
            "created": True,
            "replayed": False,
            "status": "written",
            "reason": "new_records_written",
            "existing": {str(path): None for path in resolved},
        }

    any_exists = any(value is not None for value in existing.values())
    if any_exists and not all(value is not None for value in existing.values()):
        return {
            "created": False,
            "replayed": False,
            "status": "blocked",
            "reason": "partial_snapshot_detected",
            "existing": {str(path): value for path, value in existing.items()},
        }

    if all(existing[path] == content for path, content in resolved.items()):
        return {
            "created": False,
            "replayed": True,
            "status": "replayed",
            "reason": "same_output",
            "existing": {str(path): value for path, value in existing.items()},
        }

    return {
        "created": False,
        "replayed": False,
        "status": "blocked",
        "reason": "idempotency_collision",
        "existing": {str(path): value for path, value in existing.items()},
    }


def _ledger_status_from_stage(status: str, replayed: bool) -> str:
    if replayed:
        return "replayed"
    if status in {"staged", "draft_written"}:
        return "succeeded"
    if status == "blocked":
        return "blocked"
    return "manual_review"


def _run_static_gate_checks(draft_payload: Dict[str, Any], draft_markdown: str) -> Dict[str, Any]:
    payload_gate = review_draft_payload_stub(draft_payload=draft_payload)
    text_gate = review_draft_text_stub(
        proposed_task_id=draft_payload["draft_id"],
        source_task_id=draft_payload["source_task_id"],
        source_result_hash=draft_payload["source_result_hash"],
        draft_text=draft_markdown,
    )
    return {
        "checks": [
            {"id": "draft_payload_gate", "status": payload_gate.gate_status},
            {"id": "draft_text_gate", "status": text_gate.gate_status},
        ],
        "violations": list(payload_gate.violations) + list(text_gate.violations),
        "status": "failed" if payload_gate.gate_status == "failed" or text_gate.gate_status == "failed" else "manual_review",
    }


def stage_proposal_candidate_dry_run(
    candidate: dict | Path | str,
    *,
    actor: str = "system",
    proposal_run_id: str | None = None,
) -> dict:
    payload = _read_payload(candidate)
    validated_candidate = validate_proposal_candidate(payload)
    apply_readiness_payload = _coerce_apply_readiness(validated_candidate.get("apply_readiness"), reasons=[])
    if apply_readiness_payload is not None:
        validated_candidate["apply_readiness"] = apply_readiness_payload

    assessment = assess_proposal_candidate_for_staging(
        validated_candidate,
        actor=actor,
        raw_candidate=payload,
    )
    validated_candidate = assessment["candidate"]

    resolved_run_id = _normalize(proposal_run_id) or _build_proposal_run_id(validated_candidate)
    candidate_id = _build_candidate_id(validated_candidate)

    stage_dir = continuation_staged_proposal_candidate_dir(resolved_run_id, candidate_id)
    report_path = continuation_staged_proposal_candidate_report_path(resolved_run_id, candidate_id)
    draft_json_path = stage_dir / "proposal_candidate_draft.json"
    draft_markdown_path = stage_dir / "proposal_candidate_draft.md"

    reasons = list(assessment["reasons"])
    stageable = bool(assessment["stageable"])
    draft_only = bool(assessment["draft_only"])
    candidate_hash = _sha256_hex(_canonical_json(validated_candidate))
    idempotency_key = f"proposal_staging:{resolved_run_id}:{candidate_id}:{candidate_hash[:16]}"
    mission_context_block = any(
        reason in {
            "candidate_missing_mission_id",
            "candidate_missing_parent_decision_id",
            "candidate_night_auto_requires_mission_gate_pass",
        }
        for reason in reasons
    )

    draft_payload: Optional[Dict[str, Any]] = None
    write_targets: Dict[str, str] = {}
    gate_result: Dict[str, Any] = {}

    if stageable or draft_only:
        draft_payload = _build_draft_payload(validated_candidate, resolved_run_id)
        draft_text = _build_draft_markdown(validated_candidate, resolved_run_id)
        gate_result = _run_static_gate_checks(draft_payload, draft_text)

        if gate_result["status"] == "failed":
            reasons.append("static_gate_failed")
        else:
            write_targets[str(draft_json_path)] = _canonical_json(draft_payload) + "\n"
            write_targets[str(draft_markdown_path)] = draft_text
    elif mission_context_block:
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "blocked",
            "proposal_run_id": resolved_run_id,
            "candidate_id": candidate_id,
            "candidate_dir": str(stage_dir),
            "staging_report_path": str(report_path),
            "draft_path": None,
            "markdown_path": None,
            "candidate": validated_candidate,
            "assessment": assessment,
            "reasons": reasons,
            "staged": False,
            "night_auto_work_order_path": None,
            "night_auto_work_order": None,
            "night_auto_work_order_status": None,
            "night_auto_work_order_action": None,
            "night_auto_pending_artifact_path": None,
            "night_auto_pending_artifact": None,
            "night_auto_pending_status": "blocked",
            "night_auto_pending_action": "night_auto_pending_blocked",
            "ledger_event": {
                "written": False,
                "path": None,
                "event_id": None,
                "replayed": False,
                "event": None,
                "status": "skipped",
            },
        }

    write_targets[str(report_path)] = _canonical_json(
        {
            "schema_version": STAGE_SCHEMA_VERSION,
            "status": "manual_review" if reasons else ("staged" if stageable else "draft_written" if draft_only else "manual_review"),
            "proposal_cycle_id": PROPOSAL_STAGING_CYCLE_ID,
            "proposal_run_id": resolved_run_id,
            "candidate_id": candidate_id,
            "candidate": validated_candidate,
            "assessment": assessment,
            "requires_human_approval": _coerce_bool(validated_candidate.get("requires_human_approval")),
            "candidate_stageable": bool(stageable),
            "candidate_draft_only": bool(draft_only),
            "actor": _normalize(actor) or "system",
            "reason": reasons,
            "generated_at": "1970-01-01T00:00:00Z",
            "gate_checks": gate_result,
            "draft_path": str(draft_json_path),
            "draft_markdown_path": str(draft_markdown_path),
        }
    ) + "\n"

    write_result = _candidate_write_records(write_targets)
    draft_written = bool(write_targets) and bool(draft_payload) and write_result["status"] in {"written", "replayed"}
    status = write_result["status"]
    hard_block = any(
        reason.startswith("forbidden_side_effect_true:")
        or reason.startswith("forbidden_field:")
        or reason.startswith("next_step_readiness_blocker:")
        or reason.startswith("candidate_missing_mission_id")
        or reason.startswith("candidate_missing_parent_decision_id")
        or reason == "candidate_night_auto_requires_mission_gate_pass"
        or reason == "next_step_not_stageable_by_progression"
        or reason == "candidate_missing_next_step_readiness_for_repair_artifacts"
        or reason == "candidate_next_step_readiness_invalid"
        or reason.startswith("repair_result_patch_intent_validation:")
        or reason == "candidate_repair_result_has_patch_intent_validation_errors"
        or reason == "candidate_repair_result_execution_blocked"
        or reason.startswith("candidate_repair_result_execution_status_invalid:")
        or reason.startswith("repair_result_execution_status:")
        or reason.startswith("repair_result_execution:")
        or reason == "candidate_apply_readiness_blocked"
        or reason in {
            "candidate_missing_rerun_intent_artifact",
            "candidate_missing_repair_result_artifact",
            "candidate_repair_artifact_repair_id_mismatch",
        }
        or reason.startswith("candidate_apply_readiness_blocker:")
        or reason.startswith("candidate_") and reason.endswith("_artifact_missing")
        or reason.startswith("candidate_") and reason.endswith("_artifact_unreadable")
        or reason.startswith("candidate_") and reason.endswith("_artifact_invalid")
        or reason.startswith("candidate_") and reason.endswith("_artifact_invalid_format")
        or reason == "static_gate_failed"
        for reason in reasons
    )

    if status == "written":
        if hard_block:
            status = "blocked"
        else:
            status = "staged" if stageable else ("draft_written" if draft_only else "manual_review")
    elif status == "replayed":
        status = "replayed"
    elif status in {"partial_snapshot_detected", "idempotency_collision"}:
        status = "blocked"
    elif stageable is False and draft_only is False and not reasons:
        status = "manual_review"

    night_auto_work_order_result = None
    if status == "blocked":
        reasons.extend([write_result["reason"]])

    if (
        _normalize_lower(validated_candidate.get("continuation_lane")) == "night_auto"
        and status != "blocked"
        and _normalize(validated_candidate.get("mission_id"))
        and _normalize(validated_candidate.get("parent_decision_id"))
        and _coerce_bool(validated_candidate.get("mission_gate_pass"), default=False)
        and write_result["status"] in {"written", "replayed"}
    ):
        night_auto_work_order_result = write_night_auto_work_order_dry_run(
            validated_candidate,
            proposal_run_id=resolved_run_id,
            actor=actor,
        )
        if night_auto_work_order_result["status"] in {"blocked", "manual_review", "idempotency_collision"}:
            status = "blocked"
            reasons.extend(night_auto_work_order_result.get("reasons", []))
    else:
        night_auto_work_order_result = None

    report_payload = {
        "status": status,
        "created": bool(write_result.get("created")),
        "replayed": bool(write_result.get("replayed")),
        "proposal_run_id": resolved_run_id,
        "candidate_id": candidate_id,
        "candidate_dir": str(stage_dir),
        "staging_report_path": str(report_path),
        "draft_path": str(draft_json_path) if draft_json_path.exists() else None,
        "markdown_path": str(draft_markdown_path) if draft_markdown_path.exists() else None,
        "candidate": validated_candidate,
        "assessment": assessment,
        "reasons": reasons,
        "ledger_event": None,
        "night_auto_work_order_path": night_auto_work_order_result.get("work_order_path") if night_auto_work_order_result else None,
        "night_auto_work_order": night_auto_work_order_result.get("work_order") if night_auto_work_order_result else None,
        "night_auto_work_order_status": night_auto_work_order_result.get("status") if night_auto_work_order_result else None,
        "night_auto_work_order_action": night_auto_work_order_result.get("action") if night_auto_work_order_result else None,
        "night_auto_pending_artifact_path": None,
        "night_auto_pending_artifact": None,
        "night_auto_pending_status": None,
        "night_auto_pending_action": None,
    }
    report_payload["draft"] = draft_payload
    night_auto_pending_result = None
    if (
        status != "blocked"
        and night_auto_work_order_result is not None
        and night_auto_work_order_result.get("status") in {"created", "replayed"}
        and _normalize_lower(validated_candidate.get("continuation_lane")) == "night_auto"
        and _normalize(validated_candidate.get("mission_id"))
        and _normalize(validated_candidate.get("parent_decision_id"))
        and _coerce_bool(validated_candidate.get("mission_gate_pass"), default=False)
        and write_result["status"] in {"written", "replayed"}
    ):
        night_auto_pending_result = write_night_auto_pending_staging_artifact(
            night_auto_work_order_result.get("work_order"),
            proposal_run_id=resolved_run_id,
            actor=actor,
            scheduler_visible=False,
        )
        if night_auto_pending_result["status"] in {"blocked", "idempotency_collision"}:
            status = "blocked"
            reasons.extend(night_auto_pending_result.get("reasons", []))
        report_payload["night_auto_pending_artifact_path"] = night_auto_pending_result.get("pending_artifact_path")
        report_payload["night_auto_pending_artifact"] = night_auto_pending_result.get("pending_artifact")
        report_payload["night_auto_pending_status"] = night_auto_pending_result.get("status")
        report_payload["night_auto_pending_action"] = night_auto_pending_result.get("action")
    elif status != "blocked":
        report_payload["night_auto_pending_status"] = "skipped"
        report_payload["night_auto_pending_action"] = "night_auto_pending_not_applicable"
    elif report_payload["night_auto_pending_status"] is None:
        report_payload["night_auto_pending_status"] = "blocked"
        report_payload["night_auto_pending_action"] = "night_auto_pending_blocked"

    staged_count = "written" if write_result["status"] == "written" else "replayed" if write_result["status"] == "replayed" else status
    report_payload["staged"] = (
        status in {"staged", "draft_written", "replayed"}
        and status != "blocked"
        and (stageable or draft_only)
    )

    try:
        ledger_result = append_continuation_run_event(
            stage="proposal_staging",
            status=_ledger_status_from_stage(
                "replayed" if write_result["status"] == "replayed" else status if status in {"staged", "draft_written", "manual_review"} else "blocked",
                replayed=bool(write_result.get("replayed")),
            ),
            actor=_normalize(actor) or "system",
            idempotency_key=idempotency_key,
            run_id=resolved_run_id,
            source_task_id=_normalize(validated_candidate.get("source_task_id")),
            task_id=candidate_id,
            artifact_path=str(report_path),
            reasons=reasons,
            side_effects={key: False for key in REQUIRED_SIDE_EFFECT_FALSE},
        )
    except ValueError:
        # Keep the dry-run local gate deterministic even if schema does not allow this stage
        # yet. Tests may run this file before the ledger schema/allowlist updates.
        ledger_result = {
            "written": False,
            "path": str(report_path.parent.parent / "run_ledger"),
            "event_id": None,
            "replayed": False,
            "event": None,
            "status": "skipped",
        }
    report_payload["ledger_event"] = ledger_result

    return {
        **report_payload,
        "action": status,
    }


def summarize_staged_proposal_candidates(staged_candidates: Dict[str, Any] | Path | str) -> dict:
    source: List[Dict[str, Any]] = []

    if isinstance(staged_candidates, (str, Path)):
        candidate_root = Path(staged_candidates)
        if candidate_root.is_dir():
            source.extend(
                [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(candidate_root.rglob("staging_report.json"))
                    if path.is_file()
                ]
            )
        else:
            try:
                payload = _read_payload(candidate_root)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                if "entries" in payload and isinstance(payload["entries"], list):
                    source.extend([entry for entry in payload["entries"] if isinstance(entry, dict)])
                elif payload.get("staging_report_path") or payload.get("status"):
                    source.append(payload)
    elif isinstance(staged_candidates, dict):
        if "entries" in staged_candidates and isinstance(staged_candidates["entries"], list):
            source.extend([entry for entry in staged_candidates["entries"] if isinstance(entry, dict)])
        else:
            source.append(staged_candidates)

    candidate_count = len(source)
    stageable_count = 0
    staged_count = 0
    manual_review_count = 0
    blocked_count = 0
    replayed_count = 0
    requires_human_approval_count = 0

    for entry in source:
        status = _normalize_lower(entry.get("status"))
        if _coerce_bool(entry.get("candidate_stageable")):
            stageable_count += 1
        if _coerce_bool(entry.get("staged")):
            staged_count += 1
        if status == "blocked":
            blocked_count += 1
        elif status == "replayed":
            replayed_count += 1
        elif status in {"manual_review", "draft_written", "stageable"}:
            manual_review_count += 1
        if _coerce_bool(entry.get("requires_human_approval")):
            requires_human_approval_count += 1

    return {
        "schema_version": STAGE_SCHEMA_VERSION,
        "candidate_count": candidate_count,
        "candidate_ids": sorted({
            _normalize(item.get("candidate_id")) for item in source if _normalize(item.get("candidate_id"))
        }),
        "stageable_candidate_count": stageable_count,
        "staged_candidate_count": staged_count,
        "replayed_candidate_count": replayed_count,
        "manual_review_candidate_count": manual_review_count,
        "blocked_candidate_count": blocked_count,
        "requires_human_approval_count": requires_human_approval_count,
        "reasons": sorted({item for entry in source for item in _coerce_str_list(entry.get("reasons"))}),
    }


__all__ = [
    "assess_proposal_candidate_for_staging",
    "stage_proposal_candidate_dry_run",
    "summarize_staged_proposal_candidates",
]

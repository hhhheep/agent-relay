from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .api_council import validate_api_council_response
from .ledger import append_continuation_run_event
from .memory_observer import append_continuation_observation
from .paths import (
    continuation_proposal_candidates_path,
    continuation_proposal_candidates_summary_path,
)
from .policy import load_planner_policy

PROPOSAL_CANDIDATE_SCHEMA_VERSION = 1
DEFAULT_CYCLE_ID = "2026-05-19T_SPARK_P4D_KNOWLEDGE_TO_PROPOSAL_01"

FORBIDDEN_TOP_LEVEL_FIELDS = {
    "dispatch",
    "promote",
    "write_inbox",
    "registry_mutation",
    "source_edit",
    "remote_execution",
    "scheduler_submit",
    "remote_write",
    "running_state",
    "subprocess",
    "action_request",
    "unbounded_task",
    "credential_request",
    "path_guessing",
}

FORBIDDEN_MARKER_TEXT = {
    "direct_dispatch",
    "write_inbox",
    "promote",
    "dispatch",
    "registry_mutation",
    "source_edit",
    "remote_execution",
    "scheduler_submit",
    "remote_write",
    "credential_request",
    "credential",
    "api_key",
    "token",
    "password",
    "secret",
    "unbounded_task",
    "generate_task",
    "create_task",
    "path_guess",
}

PATH_GUESS_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"\\\\"),
    re.compile(r"^/"),
    re.compile(r"\.\."),
    re.compile(r"(?i)/ssd"),
    re.compile(r"(?i)/mnt"),
    re.compile(r"(?i)/raid"),
    re.compile(r"(?i)/home/"),
)

CANONICAL_KIND_CONFIG: Dict[str, Dict[str, Any]] = {
    "bounded_probe": {
        "worker": "small_task_worker",
        "can_stage": True,
        "requires_human_approval": False,
        "expected_outputs": [
            "bounded_probe_plan.md",
            "bounded_probe_artifacts.md",
            "action_requests.json",
        ],
        "claim_limitations": [
            "Bounded follow-up is requested for weak evidence; no automatic execution is implied.",
        ],
    },
    "inventory_audit": {
        "worker": "materialized_execution",
        "can_stage": True,
        "requires_human_approval": False,
        "expected_outputs": ["artifact_inventory_report.md", "action_requests.json"],
        "claim_limitations": [
            "Inventory audit is advisory and evidence-limited.",
        ],
    },
    "metric_completion": {
        "worker": "small_task_worker",
        "can_stage": True,
        "requires_human_approval": False,
        "expected_outputs": ["metric_completion_report.md", "action_requests.json"],
        "claim_limitations": [
            "Metric completion is bounded and evidence-only; it does not imply safe execution.",
        ],
    },
    "decision_synthesis": {
        "worker": "materialized_execution",
        "can_stage": True,
        "requires_human_approval": False,
        "expected_outputs": ["decision_synthesis.md", "action_requests.json"],
        "claim_limitations": [
            "Decision synthesis is advisory; no execution-side effects should be assumed.",
        ],
    },
    "missing_capability_request": {
        "worker": "none",
        "can_stage": False,
        "requires_human_approval": True,
        "expected_outputs": ["capability_request.json", "action_requests.json"],
        "claim_limitations": [
            "Missing capability prevents continuation until support is provided.",
        ],
    },
    "codex_debug_request": {
        "worker": "codex",
        "can_stage": False,
        "requires_human_approval": True,
        "expected_outputs": ["codex_debug_request.md", "action_requests.json"],
        "claim_limitations": [
            "Codex request is advisory only; do not perform source edits directly.",
        ],
    },
    "no_safe_continuation": {
        "worker": "none",
        "can_stage": False,
        "requires_human_approval": False,
        "expected_outputs": [],
        "claim_limitations": [
            "No safe continuation is currently identified.",
        ],
    },
}

REQUIRED_SIDE_EFFECTS = {
    "dispatch": False,
    "promote": False,
    "registry_mutation": False,
    "remote_execution": False,
    "running_state": False,
    "subprocess": False,
    "scheduler_submit": False,
    "remote_write": False,
    "write_inbox": False,
    "source_edit": False,
}


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


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_normalize(value)] if _normalize(value) else []
    if isinstance(value, (list, tuple, set)):
        return [item for item in (_normalize(item) for item in value) if item]
    return [_normalize(value)]


def _coerce_nested_readiness_payload(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = dict(value)
    if "route" in payload:
        payload["route"] = _normalize_lower(payload.get("route"))
    if "status" in payload:
        payload["status"] = _normalize_lower(payload.get("status"))
    if "blockers" in payload:
        blockers = _coerce_str_list(payload.get("blockers"))
        payload["blockers"] = blockers
    if "can_apply" in payload:
        payload["can_apply"] = _coerce_bool(payload.get("can_apply"), default=False)
    if "can_stage" in payload:
        payload["can_stage"] = _coerce_bool(payload.get("can_stage"), default=False)
    if isinstance(payload.get("bounded_validation"), dict):
        bounded_validation = dict(payload["bounded_validation"])
        for field in (
            "present_valid_artifact_ids",
            "missing_artifact_ids",
            "invalid_artifact_ids",
            "route_reasons",
            "non_valid_artifact_ids",
        ):
            if field in bounded_validation:
                bounded_validation[field] = _coerce_str_list(bounded_validation.get(field))
        if "bucket_id" in bounded_validation:
            bounded_validation["bucket_id"] = _normalize(bounded_validation.get("bucket_id"))
        if "status" in bounded_validation:
            bounded_validation["status"] = _normalize_lower(bounded_validation.get("status"))
        if "next_recommendation" in bounded_validation:
            bounded_validation["next_recommendation"] = _normalize_lower(
                bounded_validation.get("next_recommendation")
            )
        if isinstance(bounded_validation.get("artifact_statuses"), dict):
            bounded_validation["artifact_statuses"] = {
                _normalize(key): _normalize_lower(item)
                for key, item in bounded_validation["artifact_statuses"].items()
                if _normalize(key) and _normalize(item)
            }
        payload["bounded_validation"] = bounded_validation
    return payload


def _coerce_repair_artifact_payload(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = dict(value)
    if "route" in payload:
        payload["route"] = _normalize_lower(payload.get("route"))
    if "repair_type" in payload:
        payload["repair_type"] = _normalize_lower(payload.get("repair_type"))
    if "execution_status" in payload:
        payload["execution_status"] = _normalize_lower(payload.get("execution_status"))
    return payload


def _read_payload(payload: dict | Path | str) -> Dict[str, Any]:
    if isinstance(payload, (Path, str)):
        data = json.loads(Path(payload).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("payload must be a JSON object")
        return data
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("payload must be a dict, Path, or string path")


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _policy() -> Dict[str, Any]:
    return load_planner_policy()


def _allowed_kinds() -> List[str]:
    allowed = [_normalize_lower(item) for item in _policy().get("allowed_next_step_kinds", [])]
    return [kind for kind in allowed if kind]


def _max_candidates() -> int:
    value = _policy().get("max_tasks_per_source_result", 3)
    try:
        max_value = int(value)
    except (TypeError, ValueError):
        max_value = 3
    if max_value <= 0:
        return 3
    return max_value


def _policy_forbidden_actions() -> List[str]:
    policy_actions = _policy().get("forbidden_actions", [])
    actions = [_normalize_lower(item) for item in policy_actions if _normalize(item)]
    if not actions:
        actions = [
            "direct_dispatch",
            "scheduler_submit",
            "registry_mutation",
            "source_edit",
            "remote_path_guess",
            "write_inbox",
            "promote",
            "remote_execution",
        ]
    return sorted(set(actions))


def _looks_like_path_guessing(text: str) -> bool:
    normalized = _normalize_lower(text)
    return any(pattern.search(normalized) for pattern in PATH_GUESS_PATTERNS)


def _contains_forbidden_text(value: Any) -> List[str]:
    reasons: List[str] = []
    if value is None:
        return reasons
    if isinstance(value, (bool, int, float)):
        return reasons
    if isinstance(value, str):
        normalized = _normalize_lower(value)
        if _looks_like_path_guessing(normalized):
            reasons.append("path_guessing_detected")
        for marker in FORBIDDEN_MARKER_TEXT:
            if marker in normalized:
                reasons.append(f"forbidden_text:{marker}")
        return reasons
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_lower(key) in FORBIDDEN_TOP_LEVEL_FIELDS:
                reasons.append(f"forbidden_field:{_normalize_lower(key)}")
            reasons.extend(_contains_forbidden_text(item))
        return reasons
    if isinstance(value, (list, tuple, set)):
        for item in value:
            reasons.extend(_contains_forbidden_text(item))
        return reasons
    return reasons


def _normalize_source_context(council_response: Dict[str, Any], knowledge_subset: Dict[str, Any] | None, actor: str) -> Dict[str, str]:
    return {
        "source_council_id": _normalize(council_response.get("council_id")),
        "source_task_id": _normalize(
            (knowledge_subset or {}).get("source_task_id")
            or council_response.get("source_task_id")
            or "unknown_task"
        ),
        "source_knowledge_pack_id": _normalize((knowledge_subset or {}).get("pack_id") or (knowledge_subset or {}).get("source_knowledge_pack_id")),
        "line": _normalize((knowledge_subset or {}).get("line_filter") or (knowledge_subset or {}).get("line") or council_response.get("line")),
        "workplace_profile": _normalize(
            (knowledge_subset or {}).get("workplace_profile_filter")
            or (knowledge_subset or {}).get("workplace_profile")
            or council_response.get("workplace_profile")
        ),
        "actor": _normalize(actor) or "system",
    }


def _build_proposal_run_id(source: Dict[str, str]) -> str:
    digest = _sha256_hex(
        {
            "source_council_id": source.get("source_council_id"),
            "source_task_id": source.get("source_task_id"),
            "source_knowledge_pack_id": source.get("source_knowledge_pack_id"),
            "line": source.get("line"),
            "workplace_profile": source.get("workplace_profile"),
            "actor": source.get("actor"),
        }
    )
    return f"pc_{digest[:16]}"


def _safe_id(value: str) -> str:
    safe = _normalize(value).replace("..", "_").replace("/", "_").replace("\\", "_")
    return safe or "item"


def _base_candidate(
    source: Dict[str, str],
    *,
    kind: str,
    index: int,
    reason: str,
    risk_flags: List[str],
    claim_limitations: List[str] | None = None,
    expected_outputs: List[str] | None = None,
    can_stage: bool | None = None,
    requires_human_approval: bool | None = None,
    worker: str | None = None,
) -> Dict[str, Any]:
    config = CANONICAL_KIND_CONFIG.get(kind, CANONICAL_KIND_CONFIG["no_safe_continuation"])
    source_task_id = source.get("source_task_id") or "unknown_task"
    source_council_id = source.get("source_council_id") or "unknown_council"
    seed = {
        "source_task_id": source_task_id,
        "source_council_id": source_council_id,
        "kind": kind,
        "index": index,
        "risk_flags": sorted(set(_coerce_str_list(risk_flags))),
    }
    candidate_id = (
        f"{_safe_id(source_task_id)}_{_safe_id(source_council_id)}_"
        f"{kind}_{index}_{_sha256_hex(seed)[:10]}"
    )
    return {
        "schema_version": PROPOSAL_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "source_council_id": source_council_id,
        "source_task_id": source_task_id,
        "source_knowledge_pack_id": _normalize(source.get("source_knowledge_pack_id")) or None,
        "kind": kind,
        "worker": _normalize_lower(worker or config["worker"]),
        "line": _normalize(source.get("line")) or None,
        "workplace_profile": _normalize(source.get("workplace_profile")) or None,
        "reason": _normalize(reason) or f"Proposal candidate for {kind}",
        "risk_flags": _coerce_str_list(risk_flags),
        "claim_limitations": _coerce_str_list(claim_limitations) if claim_limitations is not None else _coerce_str_list(config["claim_limitations"]),
        "expected_outputs": _coerce_str_list(expected_outputs) if expected_outputs is not None else _coerce_str_list(config["expected_outputs"]),
        "forbidden_actions": _policy_forbidden_actions(),
        "status": "proposal_candidate_created",
        "can_stage": bool(config["can_stage"] if can_stage is None else can_stage),
        "requires_human_approval": bool(config["requires_human_approval"] if requires_human_approval is None else requires_human_approval),
        "side_effects": dict(REQUIRED_SIDE_EFFECTS),
        "forbidden_action_policy": _policy().get("forbidden_actions", []),
    }


def _normalize_disagreement_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(
            _normalize_lower(item)
            for item in (value.get("text"), value.get("summary"), value.get("reason"), "")
            if _normalize(item)
        ).strip()
    return _normalize_lower(value)


def _contains_token(text: str, *tokens: str) -> bool:
    normalized = f" { _normalize_lower(text).replace('_', ' ')} "
    for token in tokens:
        if f" {token} " in normalized:
            return True
    return False


def _derive_candidate_kind(
    raw_kind: str,
    reason: str,
    risk_flags: List[str],
    disagreement: str,
    category: str,
    *,
    fallback: str,
) -> str:
    allowed = set(_allowed_kinds())
    normalized_kind = _normalize_lower(raw_kind)
    if normalized_kind and normalized_kind in allowed:
        return normalized_kind

    candidate_context = f"{normalized_kind} {_normalize_lower(category)} {_normalize_lower(reason)} {_normalize_lower(disagreement)} {' '.join(_normalize_lower(item) for item in risk_flags)}"
    if _normalize_lower(category) == "source_code_required":
        return "codex_debug_request" if "codex_debug_request" in allowed else fallback
    if _normalize_lower(category) == "missing_capability":
        return "missing_capability_request" if "missing_capability_request" in allowed else fallback
    if _normalize_lower(category) == "missing_artifact":
        if "metric_completion" in allowed:
            return "metric_completion"
        if "inventory_audit" in allowed:
            return "inventory_audit"
        if "no_safe_continuation" in allowed:
            return "no_safe_continuation"
        return fallback

    if _contains_token(candidate_context, "source code", "source-edit", "source_edit", "needs source code"):
        return "codex_debug_request" if "codex_debug_request" in allowed else fallback
    if _contains_token(candidate_context, "missing", "capability", "needs_capability"):
        return "missing_capability_request" if "missing_capability_request" in allowed else fallback
    if _contains_token(candidate_context, "missing", "artifact"):
        if "metric_completion" in allowed:
            return "metric_completion"
        if "inventory_audit" in allowed:
            return "inventory_audit"
        return fallback
    if _contains_token(candidate_context, "weak", "weak_metric", "weak_evidence", "success_but_weak_evidence", "metric"):
        if "bounded_probe" in allowed:
            return "bounded_probe"
        if "decision_synthesis" in allowed:
            return "decision_synthesis"
        return fallback
    if _contains_token(candidate_context, "needs", "user", "decision", "no_safe_continuation", "scheduler_resource_wait", "unknown", "uncertain"):
        return "no_safe_continuation" if "no_safe_continuation" in allowed else fallback

    return fallback


def _candidate_from_raw(
    source: Dict[str, str],
    raw: Any,
    index: int,
    risk_flags: List[str],
    disagreement: str,
    category: str,
) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw_candidate = {"kind": raw}
    elif isinstance(raw, dict):
        raw_candidate = dict(raw)
    else:
        raw_candidate = {"kind": "no_safe_continuation", "reason": "Unsupported next_step_candidate type"}

    raw_kind = _normalize_lower(raw_candidate.get("kind") or raw_candidate.get("next_step_kind") or raw_candidate.get("type"))
    reason = _normalize(raw_candidate.get("reason") or raw_candidate.get("disagreement") or raw_candidate.get("description"))
    candidate_kind = _derive_candidate_kind(
        raw_kind,
        reason=reason,
        risk_flags=risk_flags,
        disagreement=disagreement,
        category=_normalize_lower(category),
        fallback="no_safe_continuation",
    )
    candidate = _base_candidate(
        source,
        kind=candidate_kind,
        index=index,
        reason=reason or disagreement or "Derived continuation proposal candidate.",
        risk_flags=risk_flags,
    )

    for field in FORBIDDEN_TOP_LEVEL_FIELDS:
        if field in raw_candidate:
            candidate[field] = raw_candidate[field]
    for field in ("path", "command", "action", "source_edit", "direct_dispatch", "remote_path_guess", "credential_request"):
        if field in raw_candidate:
            candidate[field] = raw_candidate[field]
    for field in ("repair_result", "rerun_intent", "apply_readiness", "next_step_readiness"):
        if field in raw_candidate:
            if field in {"repair_result", "rerun_intent"}:
                candidate[field] = _coerce_repair_artifact_payload(raw_candidate.get(field))
            else:
                candidate[field] = _coerce_nested_readiness_payload(raw_candidate.get(field))

    has_repair_result = bool(raw_candidate.get("repair_result"))
    has_rerun_intent = bool(raw_candidate.get("rerun_intent"))
    readiness = raw_candidate.get("next_step_readiness")
    if (has_repair_result or has_rerun_intent) and not isinstance(readiness, dict):
        candidate["next_step_readiness"] = {
            "schema_version": 1,
            "can_stage": False,
            "route": _normalize_lower(
                raw_candidate.get("route") or raw_candidate.get("executor_route")
            ),
            "blockers": ["candidate_missing_next_step_readiness_for_repair_artifacts"],
        }
        candidate["can_stage"] = False

    # Policy-driven allowlist enrichment from raw candidate
    if "expected_outputs" in raw_candidate:
        candidate["expected_outputs"] = _coerce_str_list(raw_candidate.get("expected_outputs"))
    if "claim_limitations" in raw_candidate:
        candidate["claim_limitations"] = _coerce_str_list(raw_candidate.get("claim_limitations"))
    if "line" in raw_candidate and _normalize(raw_candidate.get("line")):
        candidate["line"] = _normalize(raw_candidate.get("line"))
    if "workplace_profile" in raw_candidate and _normalize(raw_candidate.get("workplace_profile")):
        candidate["workplace_profile"] = _normalize(raw_candidate.get("workplace_profile"))
    if "worker" in raw_candidate:
        candidate["worker"] = _normalize_lower(raw_candidate.get("worker")) or candidate["worker"]
    if "can_stage" in raw_candidate:
        candidate["can_stage"] = _coerce_bool(raw_candidate.get("can_stage"), default=candidate["can_stage"])
    if "requires_human_approval" in raw_candidate:
        candidate["requires_human_approval"] = _coerce_bool(
            raw_candidate.get("requires_human_approval"),
            default=candidate["requires_human_approval"],
        )

    return validate_proposal_candidate(candidate)


def validate_proposal_candidate(candidate: dict) -> Dict[str, Any]:
    output = dict(candidate or {})
    reasons: List[str] = []
    schema_version = _normalize(candidate.get("schema_version"))
    if schema_version and str(schema_version) != str(PROPOSAL_CANDIDATE_SCHEMA_VERSION):
        reasons.append("invalid_schema_version")

    output["schema_version"] = PROPOSAL_CANDIDATE_SCHEMA_VERSION

    kind = _normalize_lower(output.get("kind"))
    allowed_kinds = set(_allowed_kinds())
    if not kind:
        reasons.append("missing_kind")
        kind = "no_safe_continuation"
    if kind not in allowed_kinds:
        reasons.append(f"invalid_kind:{kind}")
        if kind != "no_safe_continuation":
            if not allowed_kinds:
                allowed_kinds = {"no_safe_continuation"}
            kind = "no_safe_continuation" if "no_safe_continuation" in allowed_kinds else next(iter(sorted(allowed_kinds)))
    output["kind"] = kind

    output["candidate_id"] = _normalize(output.get("candidate_id")) or _sha256_hex(output)[:24]
    output["source_council_id"] = _normalize(output.get("source_council_id")) or "unknown_council"
    output["source_task_id"] = _normalize(output.get("source_task_id")) or None
    output["source_knowledge_pack_id"] = _normalize(output.get("source_knowledge_pack_id")) or None

    output["line"] = _normalize(output.get("line")) or None
    output["workplace_profile"] = _normalize(output.get("workplace_profile")) or None

    output["reason"] = _normalize(output.get("reason")) or f"Proposal candidate for {kind}"
    output["risk_flags"] = _coerce_str_list(output.get("risk_flags"))
    output["claim_limitations"] = _coerce_str_list(output.get("claim_limitations"))
    output["expected_outputs"] = _coerce_str_list(output.get("expected_outputs"))
    output["continuation_lane"] = _normalize_lower(output.get("continuation_lane")) or None
    output["mission_id"] = _normalize(output.get("mission_id")) or None
    output["parent_decision_id"] = _normalize(output.get("parent_decision_id")) or None
    output["mission_gate_pass"] = _coerce_bool(output.get("mission_gate_pass"), default=False)

    if not output["claim_limitations"]:
        output["claim_limitations"] = _coerce_str_list(CANONICAL_KIND_CONFIG.get(kind, {}).get("claim_limitations"))
    if not output["expected_outputs"]:
        output["expected_outputs"] = _coerce_str_list(CANONICAL_KIND_CONFIG.get(kind, {}).get("expected_outputs"))

    candidate_config = CANONICAL_KIND_CONFIG.get(kind, CANONICAL_KIND_CONFIG["no_safe_continuation"])
    output["worker"] = _normalize_lower(output.get("worker") or candidate_config["worker"])
    output["can_stage"] = _coerce_bool(output.get("can_stage"), default=bool(candidate_config["can_stage"]))
    output["requires_human_approval"] = _coerce_bool(
        output.get("requires_human_approval"),
        default=bool(candidate_config["requires_human_approval"]),
    )

    output["forbidden_actions"] = sorted({_normalize_lower(item) for item in _coerce_str_list(output.get("forbidden_actions"))})
    if not output["forbidden_actions"]:
        output["forbidden_actions"] = _policy_forbidden_actions()

    if isinstance(output.get("next_step_readiness"), dict):
        output["next_step_readiness"] = _coerce_nested_readiness_payload(output.get("next_step_readiness"))
    if isinstance(output.get("apply_readiness"), dict):
        output["apply_readiness"] = _coerce_nested_readiness_payload(output.get("apply_readiness"))
    if isinstance(output.get("repair_result"), dict):
        output["repair_result"] = _coerce_repair_artifact_payload(output.get("repair_result"))
    if isinstance(output.get("rerun_intent"), dict):
        output["rerun_intent"] = _coerce_repair_artifact_payload(output.get("rerun_intent"))

    output["side_effects"] = dict(REQUIRED_SIDE_EFFECTS)
    side_effects = output.get("side_effects")
    if isinstance(side_effects, dict):
        for key, value in side_effects.items():
            output["side_effects"][str(key)] = _coerce_bool(value, default=bool(output["side_effects"].get(str(key), False)))

    # kind-level policy constraints
    if kind == "no_safe_continuation":
        output["can_stage"] = False
    if kind == "codex_debug_request":
        output["worker"] = "codex"
        output["can_stage"] = False
        output["requires_human_approval"] = True

    if kind in {"bounded_probe", "decision_synthesis"} and not output["claim_limitations"]:
        output["claim_limitations"] = ["Weak evidence requires bounded review before any continuation attempt."]

    if kind in {"bounded_probe", "decision_synthesis", "inventory_audit", "metric_completion"}:
        output["worker"] = output["worker"] or "small_task_worker"

    scan_candidate = {
        key: value
        for key, value in output.items()
        if key not in {"side_effects", "forbidden_actions", "forbidden_action_policy"}
    }
    forbidden_text_reasons = _contains_forbidden_text(scan_candidate)
    reasons.extend(forbidden_text_reasons)

    for field in FORBIDDEN_TOP_LEVEL_FIELDS:
        if field in output and _coerce_bool(output.get(field), default=False):
            reasons.append(f"forbidden_field:{field}")

    if _coerce_bool(output.get("source_edit")):
        reasons.append("forbidden_field:source_edit")
    if _coerce_bool(output.get("write_inbox")):
        reasons.append("forbidden_field:write_inbox")

    if any(_looks_like_path_guessing(_normalize(item)) for item in output.get("forbidden_actions", [])):
        reasons.append("path_guessing_detected")
    if output["side_effects"]:
        if any(output["side_effects"].get(key) for key in REQUIRED_SIDE_EFFECTS):
            reasons.append("side_effects_not_all_false")

    output["status"] = "proposal_candidate_created" if not reasons else "rejected"
    output["status"] = _normalize_lower(output.get("status")) or "proposal_candidate_created"
    if reasons:
        output["status"] = "rejected"
    output["reasons"] = sorted(set(reasons))
    output["forbidden_action_policy"] = _policy().get("forbidden_actions", [])
    return output


def _build_payload_for_replay(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = dict(payload)
    for key in ("created", "replayed", "action", "candidates_path", "summary_path", "summary"):
        candidate.pop(key, None)
    return candidate


def build_proposal_candidates_from_council_dry_run(
    council_response: dict | Path,
    knowledge_subset: dict | Path | None = None,
    *,
    actor: str = "system",
) -> dict:
    response = _read_payload(council_response)
    subset = _read_payload(knowledge_subset) if knowledge_subset is not None else {}
    validation = validate_api_council_response(response, request=None)
    source = _normalize_source_context(response, subset, actor=actor)
    proposal_run_id = _build_proposal_run_id(source)
    max_candidates = _max_candidates()

    risk_flags = _coerce_str_list(response.get("risk_flags"))
    disagreement_obj = response.get("disagreement_summary")
    disagreement = _normalize_disagreement_text(disagreement_obj)
    category = _normalize_lower(
        response.get("category")
        or response.get("interpretation_category")
        or response.get("interpretation_votes", [{}])[0].get("category", "")
    )

    raw_candidates = response.get("next_step_candidates")
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    candidates: List[Dict[str, Any]] = []
    if raw_candidates:
        for index, raw in enumerate(raw_candidates[:max_candidates]):
            candidates.append(
                _candidate_from_raw(
                    source=source,
                    raw=raw,
                    index=index,
                    risk_flags=risk_flags,
                    disagreement=disagreement,
                    category=category,
                )
            )

    if not candidates:
        allowed = set(_allowed_kinds())
        fallback_kinds = []
        if category == "missing_artifact" and ("metric_completion" in allowed or "inventory_audit" in allowed):
            for kind in ("metric_completion", "inventory_audit", "no_safe_continuation"):
                if kind in allowed:
                    fallback_kinds = [kind]
                    break
        elif category == "missing_capability" and "missing_capability_request" in allowed:
            fallback_kinds = ["missing_capability_request"]
        elif _contains_token(" ".join(_normalize_lower(item) for item in risk_flags), "missing", "capability", "needs_capability"):
            if "missing_capability_request" in allowed:
                fallback_kinds = ["missing_capability_request"]
        elif _contains_token(" ".join(_normalize_lower(item) for item in risk_flags), "missing", "artifact"):
            for kind in ("metric_completion", "inventory_audit", "no_safe_continuation"):
                if kind in allowed:
                    fallback_kinds = [kind]
                    break
        elif _contains_token(
            f"{category} {disagreement}",
            "weak",
            "weak_metric",
            "weak evidence",
            "success_but_weak_evidence",
        ):
            if "bounded_probe" in allowed:
                fallback_kinds.append("bounded_probe")
            if "decision_synthesis" in allowed:
                fallback_kinds.append("decision_synthesis")
            if not fallback_kinds:
                fallback_kinds = ["no_safe_continuation"]
        elif category == "source_code_required" and "codex_debug_request" in allowed:
            fallback_kinds = ["codex_debug_request"]
        else:
            fallback_kinds = ["no_safe_continuation"] if "no_safe_continuation" in allowed else list(sorted(allowed))[:1]

        for index, kind in enumerate(fallback_kinds[:max_candidates]):
            candidates.append(
                validate_proposal_candidate(
                    _base_candidate(
                        source,
                        kind=kind,
                        index=index,
                        reason=_normalize(disagreement) or "Fallback candidate from advisory uncertainty.",
                        risk_flags=risk_flags,
                    )
                )
            )
    else:
        # Keep only allowed policy kinds and cap count.
        candidates = candidates[:max_candidates]

    status = "proposal_candidates_created" if all(
        _normalize_lower(item.get("status")) == "proposal_candidate_created" for item in candidates
    ) else "proposal_candidates_rejected"

    for candidate in candidates:
        if _normalize_lower(candidate.get("kind")) in {"bounded_probe", "decision_synthesis"}:
            if not candidate.get("claim_limitations"):
                candidate["claim_limitations"] = [
                    "Weak/evidence-limited continuation requires bounded follow-up before execution-like actions.",
                ]
            candidate["claim_limitations"] = list(dict.fromkeys(_coerce_str_list(candidate["claim_limitations"])))

    return {
        "schema_version": PROPOSAL_CANDIDATE_SCHEMA_VERSION,
        "proposal_cycle_id": DEFAULT_CYCLE_ID,
        "proposal_run_id": proposal_run_id,
        "status": status,
        "actor": _normalize(source.get("actor")) or "system",
        "source_council_id": source["source_council_id"],
        "source_task_id": source["source_task_id"],
        "source_knowledge_pack_id": source["source_knowledge_pack_id"] or None,
        "line": source["line"] or None,
        "workplace_profile": source["workplace_profile"] or None,
        "max_candidates": max_candidates,
        "candidate_count": len(candidates),
        "stageable_candidate_count": len([item for item in candidates if _coerce_bool(item.get("can_stage"))]),
        "candidates": candidates,
        "validation": validation,
    }


def write_proposal_candidates_dry_run(candidates: dict, *, proposal_run_id: str | None = None) -> dict:
    payload = _read_payload(candidates)
    target_run_id = _normalize(proposal_run_id or payload.get("proposal_run_id")) or _build_proposal_run_id({
        "source_council_id": _normalize(payload.get("source_council_id")),
        "source_task_id": _normalize(payload.get("source_task_id")),
        "source_knowledge_pack_id": _normalize(payload.get("source_knowledge_pack_id")),
        "line": _normalize(payload.get("line")),
        "workplace_profile": _normalize(payload.get("workplace_profile")),
        "actor": _normalize(payload.get("actor")) or "system",
    })

    candidates_path = continuation_proposal_candidates_path(target_run_id)
    summary_path = continuation_proposal_candidates_summary_path(target_run_id)

    output: Dict[str, Any] = {
        "schema_version": PROPOSAL_CANDIDATE_SCHEMA_VERSION,
        "proposal_run_id": target_run_id,
        "actor": _normalize(payload.get("actor")) or "system",
        "candidates": [],
        "candidates_path": str(candidates_path),
        "summary_path": str(summary_path),
    }

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return {
            **output,
            **payload,
            "status": "proposal_candidates_rejected",
            "created": False,
            "replayed": False,
            "action": "invalid_candidates_payload",
            "reasons": ["candidates_must_be_list"],
        }

    validated_candidates = [validate_proposal_candidate(item) for item in raw_candidates if isinstance(item, dict)]
    output["candidates"] = validated_candidates
    output["candidate_count"] = len(validated_candidates)
    output["stageable_candidate_count"] = len([item for item in validated_candidates if _coerce_bool(item.get("can_stage"))])
    output["source_council_id"] = _normalize(payload.get("source_council_id")) or "unknown_council"
    output["source_task_id"] = _normalize(payload.get("source_task_id")) or None
    output["source_knowledge_pack_id"] = _normalize(payload.get("source_knowledge_pack_id")) or None
    output["line"] = _normalize(payload.get("line")) or None
    output["workplace_profile"] = _normalize(payload.get("workplace_profile")) or None
    output["status"] = "proposal_candidates_rejected" if any(
        _normalize_lower(item.get("status")) != "proposal_candidate_created"
        for item in validated_candidates
    ) else "proposal_candidates_created"
    output["reasons"] = []
    output["reasons"].extend(sum((item.get("reasons", []) for item in validated_candidates), []))

    summary = summarize_proposal_candidates(output)
    output["summary"] = summary

    if output["candidate_count"]:
        output["candidate_count"] = len(validated_candidates)
    candidates_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_existing_json(candidates_path)
    replay_check = _build_payload_for_replay(output)
    existing_payload = None if not existing else _build_payload_for_replay(existing)
    if existing_payload is not None:
        if _canonical_json(replay_check) == _canonical_json(existing_payload):
            return {
                **output,
                "status": output["status"],
                "created": False,
                "replayed": True,
                "action": "proposal_candidates_replayed",
                "reason_collision_check": [],
            }
        return {
            **output,
            "status": "proposal_candidates_rejected",
            "created": False,
            "replayed": False,
            "action": "idempotency_collision",
            "reasons": sorted(set(output["reasons"] + ["idempotency_collision"])),
        }

    candidates_path.write_text(_canonical_json(replay_check) + "\n", encoding="utf-8")
    summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8")

    if existing_payload is None:
        ledger_input = {
            "stage": "proposal_candidate",
            "status": "succeeded" if output["status"] == "proposal_candidates_created" else "manual_review",
            "actor": _normalize(payload.get("actor")) or "system",
            "idempotency_key": f"proposal_candidate:{target_run_id}:{_sha256_hex(replay_check)[:16]}",
            "run_id": target_run_id,
            "source_task_id": output.get("source_task_id"),
            "artifact_path": str(candidates_path),
            "reasons": output["reasons"] or ["proposal candidates written"],
            "side_effects": dict(REQUIRED_SIDE_EFFECTS),
        }
        append_continuation_run_event(**ledger_input)
        append_continuation_observation(
            run_id=target_run_id,
            stage="proposal_candidate",
            actor=_normalize(payload.get("actor")) or "system",
            status="created" if output["status"] == "proposal_candidates_created" else output["status"],
            idempotency_key=f"proposal_candidate_observation:{target_run_id}:{_sha256_hex(replay_check)[:16]}",
            source_task_id=output.get("source_task_id"),
            artifact_path=str(candidates_path),
            reasons=output["reasons"] or ["proposal candidates written"],
            payload={
                "summary": output.get("summary"),
                "status": output["status"],
                "candidate_count": output["candidate_count"],
                "stageable_candidate_count": output["stageable_candidate_count"],
            },
        )

    return {
        **output,
        "created": True,
        "replayed": False,
        "action": "proposal_candidates_written",
        "summary_path": str(summary_path),
        "candidates_path": str(candidates_path),
    }


def summarize_proposal_candidates(candidates: dict | Path) -> dict:
    payload = _read_payload(candidates)
    candidate_list = payload.get("candidates", [])
    normalized_candidates = [validate_proposal_candidate(item) for item in candidate_list if isinstance(item, dict)]

    rejected = [item for item in normalized_candidates if _normalize_lower(item.get("status")) != "proposal_candidate_created"]
    all_side_effects_false = all(
        not any(_coerce_bool(value) for value in candidate["side_effects"].values())
        for candidate in normalized_candidates
    ) if normalized_candidates else True

    return {
        "schema_version": PROPOSAL_CANDIDATE_SCHEMA_VERSION,
        "proposal_run_id": _normalize(payload.get("proposal_run_id")),
        "source_council_id": _normalize(payload.get("source_council_id")),
        "status": payload.get("status") or ("proposal_candidates_created" if not rejected else "proposal_candidates_rejected"),
        "candidate_count": len(normalized_candidates),
        "stageable_candidate_count": len([item for item in normalized_candidates if item.get("can_stage")]),
        "created_candidate_count": len([item for item in normalized_candidates if item.get("status") == "proposal_candidate_created"]),
        "rejected_candidate_count": len(rejected),
        "candidate_kinds": sorted({_normalize_lower(item.get("kind")) for item in normalized_candidates if item.get("kind")}),
        "rejected_kinds": sorted({_normalize_lower(item.get("kind")) for item in rejected if item.get("kind")}),
        "all_side_effects_false": bool(all_side_effects_false),
        "proposal_cycle_id": DEFAULT_CYCLE_ID,
    }


__all__ = [
    "build_proposal_candidates_from_council_dry_run",
    "validate_proposal_candidate",
    "write_proposal_candidates_dry_run",
    "summarize_proposal_candidates",
]

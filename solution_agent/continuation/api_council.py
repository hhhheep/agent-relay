from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict

from .paths import (
    continuation_api_council_request_path,
    continuation_api_council_response_path,
    continuation_api_council_summary_path,
)

API_COUNCIL_SCHEMA_VERSION = 1

REQUEST_ALLOWED_OUTPUTS = (
    "interpretation_votes",
    "risk_flags",
    "next_step_candidates",
    "disagreement_summary",
)

REQUEST_FORBIDDEN_ACTIONS = (
    "write_inbox",
    "promote",
    "dispatch",
    "scheduler_submit",
    "registry_mutation",
    "remote_execution",
    "source_edit",
    "path_guessing",
    "credential_request",
)

REQUEST_SIDE_EFFECTS = {
    "write_inbox": False,
    "promote": False,
    "dispatch": False,
    "scheduler_submit": False,
    "registry_mutation": False,
    "remote_execution": False,
    "source_edit": False,
    "path_guessing": False,
    "credential_request": False,
}

RESPONSE_STATUS_VALUES = {"valid", "rejected"}
RESPONSE_REQUIRED_FIELDS = {
    "schema_version",
    "council_id",
    "interpretation_votes",
    "risk_flags",
    "next_step_candidates",
    "disagreement_summary",
    "status",
}

RESPONSE_ALLOWED_METADATA_FIELDS = {
    "metadata",
    "idempotency_key",
    "source_task_id",
    "line",
    "workplace_profile",
}

FORBIDDEN_TOP_LEVEL_RESPONSE_FIELDS = {
    "direct_dispatch",
    "promote",
    "write_inbox",
    "dispatch",
    "registry_mutation",
    "remote_execution",
    "source_edit",
    "scheduler_submit",
    "command",
    "action_request",
    "api_council_request_id",
    "running_state",
    "subprocess",
}

FORBIDDEN_RESPONSE_TEXT = (
    "direct_dispatch",
    "promote",
    "write_inbox",
    "dispatch",
    "registry_mutation",
    "remote_execution",
    "source_edit",
    "scheduler_submit",
    "path_guessing",
    "credential_request",
    "credential",
    "api_key",
    "token",
    "password",
    "secret",
    "unbounded_task",
    "generate_task",
    "create_task",
    "command",
)

PATH_GUESSING_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\\\\"),
    re.compile(r"^/"),
    re.compile(r"\.\."),
)


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


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


def _normalize_remote_section(section: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "section_id": _normalize_lower(section.get("section_id") or section.get("section")),
        "classification": _normalize_lower(section.get("classification")),
        "path": _normalize(section.get("path")),
        "content_ref": _normalize(section.get("content_ref")),
        "workplace_profile": _normalize_lower(section.get("workplace_profile") or section.get("workplace")),
        "line": _normalize_lower(section.get("line")),
        "stale_or_expired": bool(section.get("stale_or_expired")),
        "included": bool(section.get("included")),
        "inclusion_reason": _normalize(section.get("inclusion_reason")),
    }


def _normalize_subset_for_hash(subset: Dict[str, Any]) -> Dict[str, Any]:
    sections = [
        _normalize_remote_section(dict(section) if isinstance(section, dict) else {})
        for section in subset.get("remote_sections", [])
        if isinstance(section, dict)
    ]
    return {
        "pack_id": _normalize(subset.get("pack_id")),
        "source_task_id": _normalize(subset.get("source_task_id")),
        "line_filter": _normalize_lower(subset.get("line_filter")),
        "workplace_profile_filter": _normalize_lower(subset.get("workplace_profile_filter")),
        "remote_sections": sections,
    }


def _payload_for_replay(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = dict(payload)
    for key in ("created", "replayed", "request_path", "response_path", "summary_path", "validation", "action"):
        candidate.pop(key, None)
    return candidate


def _is_same_payload(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return _canonical_json(_payload_for_replay(left)) == _canonical_json(_payload_for_replay(right))


def _looks_like_path_guessing(text: str) -> bool:
    normalized = _normalize_lower(text)
    for pattern in PATH_GUESSING_PATTERNS:
        if pattern.search(normalized):
            return True
    return False


def _forbidden_findings_in_object(value: Any) -> list[str]:
    if value is None or isinstance(value, (bool, int, float)):
        return []

    findings: list[str] = []
    if isinstance(value, str):
        normalized = _normalize_lower(value)
        for marker in FORBIDDEN_RESPONSE_TEXT:
            if marker in normalized:
                findings.append(f"forbidden_text:{marker}")
        if _looks_like_path_guessing(normalized):
            findings.append("path_guessing_detected")
        return findings

    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_lower(key)
            if normalized_key in FORBIDDEN_TOP_LEVEL_RESPONSE_FIELDS:
                findings.append(f"forbidden_field:{normalized_key}")
            findings.extend(_forbidden_findings_in_object(item))
        return findings

    if isinstance(value, (list, tuple, set)):
        for item in value:
            findings.extend(_forbidden_findings_in_object(item))
        return findings

    return []


def build_api_council_request_dry_run(
    knowledge_subset: dict | Path,
    *,
    source_task_id: str | None = None,
    line: str | None = None,
    workplace_profile: str | None = None,
    actor: str = "system",
    workplace: str | None = None,  # compatibility alias for P4-b callers
) -> dict:
    """Build a deterministic local-only API-council request."""

    subset = _read_payload(knowledge_subset)
    actor_norm = _normalize(actor) or "system"
    source_task_norm = _normalize(source_task_id or subset.get("source_task_id"))
    line_norm = _normalize_lower(line)
    workplace_profile_norm = _normalize_lower(workplace_profile or workplace)
    if not workplace_profile_norm:
        workplace_profile_norm = _normalize_lower(
            subset.get("workplace_profile_filter") or subset.get("workplace_profile") or subset.get("workplace")
        )

    subset_status = _normalize_lower(subset.get("status") or "ready")
    knowledge_subset_hash = _sha256_hex(_normalize_subset_for_hash(subset))
    request_seed = {
        "actor": actor_norm,
        "source_task_id": source_task_norm,
        "line": line_norm,
        "workplace_profile": workplace_profile_norm,
        "knowledge_subset_hash": knowledge_subset_hash,
        "subset_status": subset_status,
    }

    council_id = f"ac_{_sha256_hex(request_seed)[:16]}"
    idempotency_key = f"ac_{_sha256_hex(request_seed)[:24]}"

    return {
        "schema_version": API_COUNCIL_SCHEMA_VERSION,
        "council_id": council_id,
        "idempotency_key": idempotency_key,
        "actor": actor_norm,
        "status": "request_created" if subset_status == "ready" else "request_blocked",
        "source_task_id": source_task_norm or None,
        "line": line_norm or None,
        "workplace_profile": workplace_profile_norm or None,
        "knowledge_subset_hash": knowledge_subset_hash,
        "allowed_outputs": list(REQUEST_ALLOWED_OUTPUTS),
        "forbidden_actions": list(REQUEST_FORBIDDEN_ACTIONS),
        "side_effects": dict(REQUEST_SIDE_EFFECTS),
    }


def _validate_api_council_request_dry_run(request: dict) -> dict:
    reasons: list[str] = []
    council_id = _normalize(request.get("council_id"))
    output = {
        "schema_version": API_COUNCIL_SCHEMA_VERSION,
        "council_id": council_id,
        "status": "rejected",
        "reasons": reasons,
    }

    if not isinstance(request, dict):
        output["reasons"] = ["request_not_mapping"]
        return output

    required_fields = {
        "schema_version",
        "council_id",
        "idempotency_key",
        "actor",
        "knowledge_subset_hash",
        "allowed_outputs",
        "forbidden_actions",
        "side_effects",
        "status",
    }
    missing = sorted(required_fields - set(request.keys()))
    if missing:
        reasons.append("missing_required_request_fields:" + ",".join(missing))

    if _normalize(request.get("schema_version")) and str(request.get("schema_version")) != str(API_COUNCIL_SCHEMA_VERSION):
        reasons.append("invalid_schema_version")

    if not council_id:
        reasons.append("missing_council_id")

    if _normalize(request.get("actor")) == "":
        reasons.append("missing_actor")

    if _normalize_lower(request.get("status")) not in {"request_created", "request_blocked"}:
        reasons.append("invalid_status")

    allowed_outputs = request.get("allowed_outputs")
    if not isinstance(allowed_outputs, list) or sorted([_normalize_lower(item) for item in allowed_outputs]) != sorted(
        REQUEST_ALLOWED_OUTPUTS
    ):
        reasons.append("invalid_allowed_outputs")

    forbidden_actions = request.get("forbidden_actions")
    if not isinstance(forbidden_actions, list) or sorted(_normalize_lower(item) for item in forbidden_actions) != sorted(
        REQUEST_FORBIDDEN_ACTIONS
    ):
        reasons.append("invalid_forbidden_actions")

    side_effects = request.get("side_effects")
    if not isinstance(side_effects, dict):
        reasons.append("invalid_side_effects")
    else:
        for key, default in REQUEST_SIDE_EFFECTS.items():
            if bool(side_effects.get(key, default)):
                reasons.append(f"side_effect_not_false:{key}")

    if reasons:
        output["reasons"] = sorted(set(reasons))
        return output

    output["status"] = "accepted" if _normalize_lower(request.get("status")) == "request_created" else "blocked"
    output["actor"] = _normalize(request.get("actor"))
    output["idempotency_key"] = _normalize(request.get("idempotency_key"))
    return output


def write_api_council_request_dry_run(request: dict, *, council_id: str | None = None) -> dict:
    """Write a deterministic API-council request artifact under continuation/api_council/<id>/."""

    validated = _validate_api_council_request_dry_run(request)
    target_id = _normalize(council_id or request.get("council_id"))
    target_path = continuation_api_council_request_path(target_id) if target_id else None

    output = {**dict(request), **validated}
    output["request_path"] = str(target_path or "")

    if validated["status"] not in {"accepted", "blocked"}:
        output["status"] = "blocked"
        output["created"] = False
        output["replayed"] = False
        output["action"] = "request_validation_failed"
        if "request_validation_failed" not in output["reasons"]:
            output.setdefault("reasons", ["request_validation_failed"])
        return output

    if not target_id or target_path is None:
        output["status"] = "blocked"
        output["created"] = False
        output["replayed"] = False
        output["reasons"] = list(sorted(set(list(output.get("reasons", [])) + ["missing_council_id"])))
        output["action"] = "missing_council_id"
        return output

    if validated["status"] == "blocked":
        output["status"] = "blocked"
        output["created"] = False
        output["replayed"] = False
        output["reasons"] = list(sorted(set(list(output.get("reasons", [])) + ["request_not_ready"])))
        output["action"] = "request_not_ready"
        return output

    payload_to_write = dict(request)
    payload_to_write["council_id"] = target_id
    target_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_existing_json(target_path)
    if existing is not None:
        if _is_same_payload(existing, payload_to_write):
            return {
                **output,
                "council_id": target_id,
                "status": "accepted",
                "created": False,
                "replayed": True,
                "request_path": str(target_path),
                "action": "request_replayed",
            }
        if _normalize(existing.get("council_id")) == target_id:
            return {
                **output,
                "council_id": target_id,
                "status": "blocked",
                "created": False,
                "replayed": False,
                "request_path": str(target_path),
                "action": "idempotency_collision",
                "reasons": sorted(set(list(output.get("reasons", [])) + ["idempotency_collision"])),
            }

    target_path.write_text(_canonical_json(payload_to_write) + "\n", encoding="utf-8")
    return {
        **output,
        "council_id": target_id,
        "status": "accepted",
        "created": True,
        "replayed": False,
        "request_path": str(target_path),
        "action": "request_created",
    }


def validate_api_council_response(response: dict | Path, *, request: dict | Path | None = None) -> dict:
    """Validate that a council response only contains advisory-only fields."""

    payload = _read_payload(response)
    request_payload = _read_payload(request) if request is not None else None
    result: dict[str, Any] = {
        "schema_version": API_COUNCIL_SCHEMA_VERSION,
        "council_id": _normalize(payload.get("council_id") or (request_payload or {}).get("council_id")),
        "status": "rejected",
        "reasons": [],
        "advisory_only": True,
    }

    if not isinstance(payload, dict):
        result["reasons"] = ["response_not_mapping"]
        return result

    allowed = set(RESPONSE_REQUIRED_FIELDS | RESPONSE_ALLOWED_METADATA_FIELDS)
    unknown_fields = set(payload.keys()) - allowed
    if unknown_fields:
        result["reasons"].extend(f"unknown_response_field:{name}" for name in sorted(unknown_fields))

    for field in RESPONSE_REQUIRED_FIELDS:
        if field not in payload:
            result["reasons"].append(f"missing_required_response_field:{field}")

    if _normalize(payload.get("schema_version")) and int(payload.get("schema_version", 0)) != API_COUNCIL_SCHEMA_VERSION:
        result["reasons"].append("invalid_schema_version")
    interpretation_votes = payload.get("interpretation_votes")
    risk_flags = payload.get("risk_flags")
    next_step_candidates = payload.get("next_step_candidates")
    disagreement_summary = payload.get("disagreement_summary")
    status = _normalize_lower(payload.get("status"))

    if status not in RESPONSE_STATUS_VALUES:
        result["reasons"].append("invalid_status")

    if not isinstance(interpretation_votes, list):
        result["reasons"].append("interpretation_votes_must_be_list")
    else:
        if any(not isinstance(item, dict) for item in interpretation_votes):
            result["reasons"].append("interpretation_votes_must_be_mappings")

    if not isinstance(risk_flags, list):
        result["reasons"].append("risk_flags_must_be_list")

    if not isinstance(next_step_candidates, list):
        result["reasons"].append("next_step_candidates_must_be_list")
    else:
        if any(not isinstance(item, (dict, str)) for item in next_step_candidates):
            result["reasons"].append("next_step_candidates_must_be_mapping_or_string")

    if not isinstance(disagreement_summary, (dict, str)):
        result["reasons"].append("disagreement_summary_must_be_mapping_or_string")

    request_council_id = _normalize(request_payload.get("council_id")) if isinstance(request_payload, dict) else None
    if request_council_id and _normalize(payload.get("council_id")) and _normalize(payload.get("council_id")) != request_council_id:
        result["reasons"].append("council_id_mismatch")

    result["reasons"].extend(_forbidden_findings_in_object(payload))

    if "unbounded_task" in _normalize(payload.get("status")):
        result["reasons"].append("unbounded_task_generation_forbidden")

    if result["reasons"]:
        result["status"] = "rejected"
        result["advisory_only"] = False
        return result

    result["status"] = "valid"
    result["action"] = "response_valid"
    return result


def write_api_council_response_dry_run(response: dict, *, council_id: str) -> dict:
    """Write an advisory council response artifact under continuation/api_council/<id>/."""

    payload = dict(response)
    payload["council_id"] = _normalize(council_id)
    validated = validate_api_council_response(payload)
    payload["validation"] = validated

    if not _normalize(payload.get("council_id")):
        return {
            **payload,
            "created": False,
            "replayed": False,
            "response_path": "",
            "summary_path": "",
            "action": "missing_council_id",
            "status": validated["status"],
            "reasons": list(validated.get("reasons", [])),
        }

    response_path = continuation_api_council_response_path(payload["council_id"])
    summary_path = continuation_api_council_summary_path(payload["council_id"])
    response_path.parent.mkdir(parents=True, exist_ok=True)

    if validated["status"] == "rejected" and "unknown_response_field" in "".join(validated.get("reasons", [])):
        # persist rejected responses too for deterministic audit trace.
        pass

    existing = _read_existing_json(response_path)
    if existing is not None:
        if _is_same_payload(existing, payload):
            return {
                **payload,
                "status": validated["status"],
                "created": False,
                "replayed": True,
                "response_path": str(response_path),
                "summary_path": str(summary_path),
                "reasons": list(validated.get("reasons", [])),
                "action": "response_replayed",
            }
        if _normalize(existing.get("council_id")) == _normalize(payload.get("council_id")):
            return {
                **payload,
                "status": "rejected",
                "created": False,
                "replayed": False,
                "response_path": str(response_path),
                "summary_path": str(summary_path),
                "reasons": list(sorted(set(list(validated.get("reasons", [])) + ["idempotency_collision"]))),
                "action": "idempotency_collision",
            }

    response_path.write_text(_canonical_json(_payload_for_replay(payload)) + "\n", encoding="utf-8")

    summary = summarize_api_council_response(payload)
    summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8")

    return {
        **payload,
        "status": validated["status"],
        "created": True,
        "replayed": False,
        "response_path": str(response_path),
        "summary_path": str(summary_path),
        "reasons": list(validated.get("reasons", [])),
        "action": "response_written",
    }


def summarize_api_council_response(response: dict | Path) -> dict:
    payload = _read_payload(response)
    validation = validate_api_council_response(payload)
    interpretation_votes = payload.get("interpretation_votes")
    risk_flags = payload.get("risk_flags")
    next_step_candidates = payload.get("next_step_candidates")

    return {
        "schema_version": API_COUNCIL_SCHEMA_VERSION,
        "council_id": _normalize(payload.get("council_id")),
        "status": _normalize_lower(payload.get("status")),
        "validation_status": validation["status"],
        "interpretation_vote_count": len(interpretation_votes) if isinstance(interpretation_votes, list) else 0,
        "next_step_candidate_count": len(next_step_candidates) if isinstance(next_step_candidates, list) else 0,
        "risk_flags_count": len(risk_flags) if isinstance(risk_flags, list) else 0,
        "advisory_only": validation["status"] == "valid",
    }


__all__ = [
    "build_api_council_request_dry_run",
    "write_api_council_request_dry_run",
    "validate_api_council_response",
    "write_api_council_response_dry_run",
    "summarize_api_council_response",
]

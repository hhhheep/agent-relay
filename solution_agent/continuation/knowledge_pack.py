from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence
import yaml

from .paths import continuation_knowledge_pack_dir

KNOWLEDGE_PACK_SCHEMA_VERSION = 1
PROJECT_UNDERSTANDING_PROTOCOL_ID = "project_understanding_protocol_v0"
PROJECT_UNDERSTANDING_OPERATING_ORDER = [
    "load_scope",
    "resolve_authority_and_chronology",
    "reconstruct_current_state",
    "classify_blockers",
    "choose_next_safe_action",
    "route_repair_or_continuation",
    "stop_or_continue",
]
PROJECT_AUTHORITY_ORDER = [
    "explicit_current_user_instruction",
    "canonical_current_state_or_decision_ledger",
    "latest_validated_report_or_collected_result",
    "chronology_and_superseded_map",
    "action_requests",
    "taskbook_or_handoff_seed",
    "raw_logs_or_artifact_text",
]

_PROJECT_UNDERSTANDING_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[2]
    / ".solution"
    / "continuation"
    / "project_understanding_protocol.yaml"
)

CANONICAL_SECTIONS: List[str] = [
    "project_brief",
    "chronology",
    "experiment_lines",
    "workplace_profiles",
    "capability_inventory",
    "result_index",
    "decision_log",
    "failure_taxonomy",
    "action_requests",
]

CANONICAL_SECTION_FILES = {section: f"{section}.md" for section in CANONICAL_SECTIONS}

KNOWN_CLASSIFICATIONS = {
    "local_only",
    "remote_safe",
    "workplace_specific",
    "line_specific",
}

DEFAULT_SIDE_EFFECTS = {
    "dispatch": False,
    "registry_mutation": False,
    "remote_execution": False,
    "running_state": False,
    "subprocess": False,
    "scheduler_dispatch": False,
    "remote_write": False,
}

FORBIDDEN_MARKERS = (
    "api_key",
    "secret",
    "credential",
    "password",
    "token",
    "ssh_",
    ".env",
    "raw_log",
    "unbounded_log",
)

FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:\\\\"),
    re.compile(r"^\\\\"),
    re.compile(r"^/[A-Za-z]"),
    re.compile(r"^/ssd", re.IGNORECASE),
    re.compile(r"^/mnt", re.IGNORECASE),
    re.compile(r"^/raid", re.IGNORECASE),
)


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
    text = _normalize(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _strip_markdown_suffix(section_id: str) -> str:
    normalized = _normalize(section_id)
    if not normalized:
        return ""
    if normalized.lower().endswith(".md"):
        return normalized[:-3]
    return normalized


def _section_id(value: Any) -> str:
    normalized = _normalize(value).replace("\\", "/")
    if not normalized:
        return ""
    candidate = normalized.split("/")[-1]
    return _strip_markdown_suffix(candidate).replace(" ", "_").lower()


def _sort_sections(sections: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [dict(section) for section in sections],
        key=lambda item: (
            _normalize_lower(item.get("section_id")),
            _normalize_lower(item.get("classification")),
            _normalize_lower(item.get("path") or item.get("content_ref")),
            _normalize_lower(item.get("line")),
            _normalize_lower(item.get("workplace_profile")),
            bool(item.get("stale_or_expired")),
        ),
    )


def _secretish(text: str) -> bool:
    normalized = _normalize(text).lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in FORBIDDEN_MARKERS):
        return True
    if any(pattern.search(normalized) for pattern in FORBIDDEN_PATH_PATTERNS):
        return True
    return False


def _read_payload(payload: dict | Path | str) -> Dict[str, Any]:
    if isinstance(payload, (Path, str)):
        data = json.loads(Path(payload).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("knowledge pack payload must be a JSON object")
        return data
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("payload must be a dict, Path, or string path")


def _read_manifest_sections(payload: Dict[str, Any]) -> List[Any]:
    raw_sections = payload.get("sections")
    if raw_sections is None:
        raw_sections = payload.get("local_sections")
    if raw_sections is None:
        return []
    if not isinstance(raw_sections, list):
        raise TypeError("sections must be a list")
    return raw_sections


def _normalize_knowledge_item(entry: Any, *, enforce_strict: bool = True) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        if enforce_strict:
            return {}
        return {}

    section_id = _section_id(
        entry.get("section_id")
        or entry.get("section")
        or entry.get("knowledge_section")
        or entry.get("artifact_section")
    )
    classification = _normalize_lower(entry.get("classification"))
    line = _normalize(entry.get("line"))
    workplace_profile = _normalize(entry.get("workplace_profile") or entry.get("workplace") or entry.get("workplace_profile_id"))
    path = _normalize(entry.get("path"))
    content_ref = _normalize(entry.get("content_ref"))
    stale_or_expired = _coerce_bool(entry.get("stale_or_expired"), default=False)

    normalized = {
        "section_id": section_id,
        "classification": classification,
        "line": line,
        "workplace_profile": workplace_profile,
        "path": path,
        "content_ref": content_ref,
        "stale_or_expired": bool(stale_or_expired),
    }
    # Include legacy alias used by a few older callers while keeping canonical
    # payload names stable for schema checks.
    normalized["section"] = section_id
    normalized["workplace"] = workplace_profile

    normalized["hash"] = _sha256_hex(_canonical_json(normalized))
    return normalized


def _sections_for_hash(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "section_id": section.get("section_id"),
            "classification": section.get("classification"),
            "workplace_profile": section.get("workplace_profile"),
            "line": section.get("line"),
            "path": section.get("path"),
            "content_ref": section.get("content_ref"),
            "stale_or_expired": bool(section.get("stale_or_expired")),
        }
        for section in sections
    ]


def _build_pack_id(
    source_task_id: str,
    actor: str,
    line: str,
    workplace_profile: str,
    sections: list[Dict[str, Any]],
) -> str:
    seed = {
        "source_task_id": source_task_id,
        "actor": actor,
        "line": line,
        "workplace_profile": workplace_profile,
        "sections": _sections_for_hash(_sort_sections(sections)),
    }
    return f"kp_{_sha256_hex(seed)[:16]}"


def _payload_for_replay(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = dict(payload)
    for key in (
        "created",
        "replayed",
        "manifest_path",
        "remote_safe_subset_path",
        "latest_pointer_path",
        "file_written",
    ):
        candidate.pop(key, None)
    # Preserve existing call sites that still use one of these aliases.
    if "sections" in candidate and not candidate.get("local_sections"):
        candidate["local_sections"] = candidate["sections"]
    return candidate


def _is_same_payload(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return _canonical_json(_payload_for_replay(left)) == _canonical_json(_payload_for_replay(right))


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def project_understanding_protocol_metadata() -> Dict[str, Any]:
    metadata = {
        "protocol_id": PROJECT_UNDERSTANDING_PROTOCOL_ID,
        "core_rule": "taskbook_is_seed_not_line_state",
        "core_rule_description": "",
        "authority_order": list(PROJECT_AUTHORITY_ORDER),
        "operating_order": list(PROJECT_UNDERSTANDING_OPERATING_ORDER),
        "required_outputs": [
            "current_mainline",
            "completed_or_superseded",
            "bottlenecks",
            "claim_limits",
            "next_safe_action",
            "why_not_other_options",
            "stop_or_continue",
        ],
    }
    try:
        protocol = yaml.safe_load(_PROJECT_UNDERSTANDING_PROTOCOL_PATH.read_text(encoding="utf-8"))
        if isinstance(protocol, dict):
            authority_order = [item.get("id") for item in protocol.get("authority_order", []) if isinstance(item, dict)]
            operating_order = [item.get("step") for item in protocol.get("operating_order", []) if isinstance(item, dict)]
            required_fields = protocol.get("required_understanding_fields", [])
            if authority_order and all(isinstance(item, str) for item in authority_order):
                metadata["authority_order"] = [item for item in authority_order if item]
            if operating_order and all(isinstance(item, str) for item in operating_order):
                metadata["operating_order"] = [item for item in operating_order if item]
            metadata["core_rule_description"] = _normalize(protocol.get("core_rule"))
            if isinstance(required_fields, list) and required_fields:
                metadata["required_understanding_fields"] = [str(item) for item in required_fields if str(item)]
    except Exception:
        metadata["core_rule_description"] = "Project-understanding protocol metadata was unavailable."
    return metadata


def validate_knowledge_pack_manifest(pack_payload: dict | Path | str) -> Dict[str, Any]:
    """Validate and normalize a manifest-style payload."""

    payload = _read_payload(pack_payload)
    actor = _normalize(payload.get("actor") or "system")
    pack_id = _normalize(payload.get("pack_id"))
    source_task_id = _normalize(payload.get("source_task_id") or payload.get("source_task"))

    checks: List[Dict[str, Any]] = []
    action_requests: List[Dict[str, Any]] = []
    reasons: List[str] = []
    blocked_reasons: List[str] = []

    checks.append({"id": "knowledge_pack_actor", "status": "passed" if actor else "failed", "actor": actor})
    schema_version_raw = payload.get("schema_version", KNOWLEDGE_PACK_SCHEMA_VERSION)
    schema_is_expected = False
    try:
        schema_is_expected = int(schema_version_raw or 0) == KNOWLEDGE_PACK_SCHEMA_VERSION
    except (TypeError, ValueError):
        schema_is_expected = False
    checks.append({"id": "knowledge_pack_schema_version", "status": "passed" if schema_is_expected else "failed"})

    missing_sections = list(CANONICAL_SECTIONS)
    normalized_sections: List[Dict[str, Any]] = []

    try:
        raw_sections = _read_manifest_sections(payload)
    except (TypeError, ValueError) as exc:
        blocked_reasons.append("sections_not_list")
        checks.append({"id": "knowledge_pack_sections_type", "status": "failed", "reason": str(exc)})
        return {
            "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
            "pack_id": pack_id,
            "source_task_id": source_task_id,
            "status": "blocked",
            "actor": actor,
            "sections": [],
            "local_sections": [],
            "checks": checks,
            "action_requests": [{"type": "needs_update", "reason": "sections_not_list"}],
            "missing_sections": sorted(missing_sections),
            "reasons": ["sections_not_list"],
            "blocked_reasons": ["sections_not_list"],
        }

    for entry in raw_sections:
        if not isinstance(entry, dict):
            blocked_reasons.append("section_entry_not_mapping")
            checks.append({"id": "knowledge_pack_section_entry", "status": "failed", "reason": "entry_not_mapping"})
            continue

        normalized = _normalize_knowledge_item(entry)
        section_id = normalized.get("section_id")
        classification = _normalize_lower(normalized.get("classification"))
        section_reference = normalized.get("path") or normalized.get("content_ref")
        checks.append({"id": f"section_{section_id or 'unknown'}", "status": "passed" if section_id else "failed"})

        reason_codes: List[str] = []

        if not section_id:
            reason_codes.append("missing_section")
            checks.append({"id": "knowledge_pack_missing_section_id", "status": "failed"})
        elif section_id in missing_sections:
            missing_sections.remove(section_id)
            checks.append({"id": f"knowledge_pack_section_{section_id}", "status": "passed"})
        else:
            reason_codes.append("unknown_section")
            checks.append({"id": f"knowledge_pack_section_{section_id}", "status": "failed"})

        if classification not in KNOWN_CLASSIFICATIONS:
            reason_codes.append("invalid_classification")
            checks.append({"id": f"classification_{section_id}", "status": "failed", "classification": classification})
        else:
            checks.append({"id": f"classification_{section_id}", "status": "passed", "classification": classification})

        if not section_reference:
            reason_codes.append("missing_path_or_content_ref")
            checks.append({"id": f"knowledge_pack_reference_{section_id}", "status": "failed"})
        elif _secretish(section_reference):
            reason_codes.append("unsafe_or_sensitive_reference")
            checks.append({"id": f"knowledge_pack_reference_{section_id}", "status": "failed", "reason": "forbidden_reference"})

        if classification == "workplace_specific" and not normalized.get("workplace_profile"):
            reason_codes.append("workplace_specific_missing_workplace_profile")
            checks.append({"id": f"knowledge_pack_workplace_{section_id}", "status": "failed"})

        if classification == "line_specific" and not normalized.get("line"):
            reason_codes.append("line_specific_missing_line")
            checks.append({"id": f"knowledge_pack_line_{section_id}", "status": "failed"})

        normalized_sections.append(normalized)
        if reason_codes:
            reasons.extend(reason_codes)

    if missing_sections:
        reason = "missing_canonical_sections"
        reasons.append(reason)
        action_requests.append({"type": "needs_update", "reason": reason, "sections": sorted(missing_sections)})
        checks.append({
            "id": "knowledge_pack_canonical_sections_complete",
            "status": "failed",
            "missing_sections": sorted(missing_sections),
        })

    if missing_sections:
        for required_section in missing_sections:
            normalized_sections.append(
                {
                    "section_id": required_section,
                    "section": required_section,
                    "classification": "",
                    "line": "",
                    "workplace_profile": "",
                    "workplace": "",
                    "path": "",
                    "content_ref": "",
                    "stale_or_expired": False,
                    "hash": _sha256_hex(_canonical_json({"section_id": required_section})),
                }
            )

    if not actor:
        blocked_reasons.append("actor_missing")

    if any(reason in {"unknown_section", "missing_path_or_content_ref", "unsafe_or_sensitive_reference", "section_entry_not_mapping", "sections_not_list"} for reason in reasons):
        blocked_reasons.append("unsafe_or_invalid_section_payload")

    if blocked_reasons:
        status = "blocked"
    elif action_requests:
        status = "needs_update"
    else:
        status = "ready"

    if status == "blocked":
        checks.append({"id": "knowledge_pack_ready", "status": "failed"})
    elif status == "needs_update":
        checks.append({"id": "knowledge_pack_ready", "status": "needs_update"})
    else:
        checks.append({"id": "knowledge_pack_ready", "status": "passed"})

    if status == "needs_update" and not action_requests:
        action_requests.append({"type": "needs_update", "reason": "needs_manual_update"})

    return {
        "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
        "pack_id": pack_id,
        "source_task_id": source_task_id,
        "status": status,
        "actor": actor,
        "sections": normalized_sections,
        "local_sections": normalized_sections,
        "checks": checks,
        "action_requests": action_requests,
        "missing_sections": sorted(missing_sections),
        "reasons": sorted(set(reasons)),
        "blocked_reasons": sorted(set(blocked_reasons)),
        "understanding_protocol": project_understanding_protocol_metadata(),
    }


def build_knowledge_pack_manifest_dry_run(
    source_items: list[dict],
    *,
    line: str | None = None,
    workplace_profile: str | None = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Build a deterministic manifest without writing any file."""

    actor_norm = _normalize(actor) or "system"
    line_norm = _normalize(line)
    workplace_profile_norm = _normalize(workplace_profile)

    if not isinstance(source_items, list):
        return {
            "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
            "status": "blocked",
            "pack_id": "",
            "source_task_id": "",
            "actor": actor_norm,
            "sections": [],
            "local_sections": [],
            "checks": [{"id": "knowledge_pack_source_items_type", "status": "failed"}],
            "action_requests": [{"type": "needs_update", "reason": "source_items_not_list"}],
            "reasons": ["source_items_not_list"],
            "blocked_reasons": ["source_items_not_list"],
            "created": False,
            "replayed": False,
            "manifest_path": "",
        }

    source_task_ids = {
        _normalize(item.get("source_task_id"))
        for item in source_items
        if isinstance(item, dict)
        and _normalize(item.get("source_task_id"))
    }
    source_task_id = next(iter(sorted(source_task_ids)), "unknown_task")

    source_payload = {
        "actor": actor_norm,
        "source_task_id": source_task_id,
        "line": line_norm,
        "workplace_profile": workplace_profile_norm,
        "sections": [dict(item) if isinstance(item, dict) else item for item in source_items],
    }

    assessed = validate_knowledge_pack_manifest(source_payload)

    normalized_sections = _sort_sections(assessed.get("sections", []))
    pack_id = _build_pack_id(source_task_id, actor_norm, line_norm, workplace_profile_norm, normalized_sections)

    seed = {
        "actor": actor_norm,
        "source_task_id": source_task_id,
        "line": line_norm,
        "workplace_profile": workplace_profile_norm,
        "pack_id": pack_id,
        "sections_count": len(normalized_sections),
        "section_ids": [entry.get("section_id") for entry in normalized_sections],
    }

    return {
        "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
        "pack_id": pack_id,
        "source_task_id": source_task_id,
        "line": line_norm,
        "workplace_profile": workplace_profile_norm,
        "actor": actor_norm,
        "status": assessed["status"],
        "sections": normalized_sections,
        "local_sections": normalized_sections,
        "checks": list(assessed.get("checks", []))
        + [
            {"id": "knowledge_pack_pack_id", "status": "passed", "pack_id": pack_id},
            {"id": "knowledge_pack_idempotent_hash_seed", "status": "passed", "manifest_id": f"kp_manifest_{_sha256_hex(seed)[:16]}"},
        ],
        "action_requests": list(assessed.get("action_requests", [])),
        "missing_sections": assessed.get("missing_sections", []),
        "reasons": list(assessed.get("reasons", [])),
        "blocked_reasons": list(assessed.get("blocked_reasons", [])),
        "manifest_id": f"kp_manifest_{_sha256_hex(seed)[:16]}",
        "idempotency_key": f"kp_{_sha256_hex(seed)[:24]}",
        "understanding_protocol": project_understanding_protocol_metadata(),
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        "created": False,
        "replayed": False,
        "manifest_path": str(continuation_knowledge_pack_dir() / pack_id / "manifest.json"),
    }


def write_knowledge_pack_manifest_dry_run(
    manifest: dict,
    *,
    pack_id: str | None = None,
) -> dict:
    """Write a dry-run manifest to continuation/knowledge_packs/<pack_id>/manifest.json."""

    assessed = validate_knowledge_pack_manifest(manifest)
    target_pack_id = _normalize(pack_id or assessed.get("pack_id"))
    target_path = continuation_knowledge_pack_dir() / target_pack_id / "manifest.json" if target_pack_id else None

    if not target_pack_id:
        return {
            **assessed,
            "created": False,
            "replayed": False,
            "manifest_path": "",
            "action": "missing_pack_id",
        }

    if assessed["status"] != "ready":
        return {
            **assessed,
            "created": False,
            "replayed": False,
            "manifest_path": str(target_path),
            "action": "manifest_not_ready",
        }

    if assessed.get("pack_id") and assessed["pack_id"] != target_pack_id:
        return {
            **assessed,
            "status": "blocked",
            "created": False,
            "replayed": False,
            "manifest_path": str(target_path),
            "reasons": list(sorted(set(list(assessed.get("reasons", [])) + ["pack_id_mismatch"]))),
            "action_requests": [{"type": "needs_update", "reason": "pack_id_mismatch", "pack_id": target_pack_id}],
            "action": "pack_id_mismatch",
        }

    output = dict(assessed)
    output["pack_id"] = target_pack_id
    output["manifest_path"] = str(target_path)
    output["sections"] = _sort_sections(output.get("sections", []))
    output["local_sections"] = output["sections"]

    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_json(target_path)
    if existing is not None:
        if _is_same_payload(existing, output):
            return {**output, "created": False, "replayed": True, "manifest_path": str(target_path)}
        if _normalize(existing.get("pack_id")) == target_pack_id:
            return {
                **output,
                "status": "blocked",
                "created": False,
                "replayed": False,
                "reasons": list(sorted(set(list(output.get("reasons", [])) + ["idempotency_collision"]))),
                "action_requests": [{"type": "needs_update", "reason": "idempotency_collision", "pack_id": target_pack_id}],
                "action": "idempotency_collision",
                "manifest_path": str(target_path),
            }

    target_path.write_text(_canonical_json(_payload_for_replay(output)) + "\n", encoding="utf-8")
    return {**output, "created": True, "replayed": False, "manifest_path": str(target_path)}


def _selection_entry_for_output(entry: Dict[str, Any], *, included: bool, reason: str | None = None) -> Dict[str, Any]:
    payload = dict(entry)
    payload["included"] = bool(included)
    if reason:
        payload["inclusion_reason"] = reason
    return payload


def select_remote_safe_knowledge_subset(
    manifest: dict | Path | str,
    *,
    line: str | None = None,
    workplace_profile: str | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Select the remote-safe subset from a validated local manifest."""

    payload = _read_payload(manifest) if not isinstance(manifest, dict) else dict(manifest)
    pack_id = _normalize(payload.get("pack_id"))
    source_task_id = _normalize(payload.get("source_task_id"))
    line_norm = _normalize(line or kwargs.get("line"))
    workplace_profile_norm = _normalize(workplace_profile or kwargs.get("workplace") or kwargs.get("workplace_profile"))

    if not workplace_profile_norm and kwargs.get("workplace") is not None:
        workplace_profile_norm = _normalize(kwargs["workplace"])

    sections = [dict(section) for section in payload.get("sections", payload.get("local_sections", [])) if isinstance(section, dict)]

    assessed = validate_knowledge_pack_manifest(
        {
            "actor": payload.get("actor", "system"),
            "source_task_id": source_task_id,
            "line": line_norm,
            "workplace_profile": workplace_profile_norm,
            "sections": sections,
        }
    )

    checks = list(assessed.get("checks", []))
    checks.append({"id": "knowledge_pack_remote_selector_line", "status": "passed" if line_norm else "skipped", "line": line_norm})
    checks.append({"id": "knowledge_pack_remote_selector_workplace_profile", "status": "passed" if workplace_profile_norm else "skipped", "workplace_profile": workplace_profile_norm})

    if assessed["status"] == "blocked":
        return {
            "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
            "pack_id": pack_id,
            "source_task_id": source_task_id,
            "status": "blocked",
            "line_filter": line_norm,
            "workplace_profile_filter": workplace_profile_norm,
            "local_sections": sections,
            "remote_sections": [],
            "checks": checks,
            "action_requests": assessed.get("action_requests", []),
            "reasons": list(assessed.get("reasons", [])),
            "understanding_protocol": project_understanding_protocol_metadata(),
        }

    local_sections = [dict(section) for section in assessed.get("sections", sections)]
    remote_sections: List[Dict[str, Any]] = []
    reasons: List[str] = []
    local_results: List[Dict[str, Any]] = []
    action_requests: List[Dict[str, Any]] = []

    for section in local_sections:
        normalized = _normalize_knowledge_item(section)
        classification = _normalize_lower(normalized.get("classification"))
        include_reason: str | None = None
        included = True

        reference = normalized.get("path") or normalized.get("content_ref")
        if not reference:
            included = False
            include_reason = "missing_reference"
        elif _secretish(reference):
            included = False
            include_reason = "unsafe_or_sensitive_reference"

        if included and classification == "local_only":
            included = False
            include_reason = "excluded_local_only"
        elif included and _coerce_bool(normalized.get("stale_or_expired"), default=False):
            included = False
            include_reason = "excluded_stale_or_expired"
        elif included and classification == "workplace_specific":
            if workplace_profile_norm != _normalize(normalized.get("workplace_profile")).lower():
                included = False
                include_reason = "workplace_profile_mismatch"
        elif included and classification == "line_specific":
            if line_norm != _normalize(normalized.get("line")).lower():
                included = False
                include_reason = "line_mismatch"

        if included and classification == "":
            included = False
            include_reason = "invalid_classification"

        if include_reason:
            reasons.append(include_reason)
        selection_entry = _selection_entry_for_output(normalized, included=included, reason=include_reason)
        local_results.append(selection_entry)
        if included:
            remote_sections.append(selection_entry)

    if any(reason in {"unsafe_or_sensitive_reference"} for reason in reasons):
        action_requests.append({"type": "needs_update", "reason": "unsafe_or_sensitive_reference"})

    return {
        "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
        "pack_id": pack_id,
        "source_task_id": source_task_id,
        "status": "blocked" if any(reason == "unsafe_or_sensitive_reference" for reason in reasons) else "ready",
        "line_filter": line_norm,
        "workplace_profile_filter": workplace_profile_norm,
        "local_sections": local_results,
        "remote_sections": remote_sections,
        "checks": checks,
        "action_requests": action_requests,
        "reasons": sorted(set(reasons)),
        "understanding_protocol": project_understanding_protocol_metadata(),
    }


def write_remote_safe_subset_dry_run(selection: dict, *, pack_id: str | None = None) -> dict:
    """Write a deterministic remote-safe subset under continuation/knowledge_packs/<pack_id>/remote_safe_subset.json."""

    payload = dict(selection)
    target_pack_id = _normalize(pack_id or payload.get("pack_id"))
    target_path = continuation_knowledge_pack_dir() / target_pack_id / "remote_safe_subset.json" if target_pack_id else None

    if not target_pack_id:
        return {
            **payload,
            "created": False,
            "replayed": False,
            "remote_safe_subset_path": "",
            "status": "blocked",
            "action": "missing_pack_id",
            "action_requests": [{"type": "needs_update", "reason": "missing_pack_id"}],
        }

    if payload.get("status") not in {"ready", "needs_update"}:
        payload["status"] = "blocked"
        return {
            **payload,
            "created": False,
            "replayed": False,
            "remote_safe_subset_path": str(target_path),
            "action": "subset_not_ready",
        }

    payload["remote_sections"] = payload.get("remote_sections", [])
    payload["pack_id"] = target_pack_id
    payload["remote_safe_subset_path"] = str(target_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_json(target_path)
    if existing is not None:
        if _is_same_payload(existing, payload):
            return {**payload, "created": False, "replayed": True, "remote_safe_subset_path": str(target_path)}
        if _normalize(existing.get("pack_id")) == target_pack_id:
            return {
                **payload,
                "status": "blocked",
                "created": False,
                "replayed": False,
                "reasons": list(sorted(set(list(payload.get("reasons", [])) + ["idempotency_collision"]))),
                "action_requests": [{"type": "needs_update", "reason": "idempotency_collision", "pack_id": target_pack_id}],
                "action": "idempotency_collision",
                "remote_safe_subset_path": str(target_path),
            }

    target_path.write_text(_canonical_json(_payload_for_replay(payload)) + "\n", encoding="utf-8")
    return {**payload, "created": True, "replayed": False, "remote_safe_subset_path": str(target_path)}


def update_knowledge_pack_latest_pointer_dry_run(pack_id: str, *, actor: str) -> dict:
    """Write an explicit local pointer for the latest knowledge pack."""

    actor_norm = _normalize(actor)
    pack_id_norm = _normalize(pack_id)

    latest_path = continuation_knowledge_pack_dir() / "latest.json"
    pointer = {
        "schema_version": KNOWLEDGE_PACK_SCHEMA_VERSION,
        "pointer_type": "knowledge_pack_latest",
        "pack_id": pack_id_norm,
        "actor": actor_norm,
        "source": "local_knowledge_pack_builder",
        "manifest_path": str(continuation_knowledge_pack_dir() / pack_id_norm / "manifest.json"),
        "updated_utc": "1970-01-01T00:00:00Z",
        "idempotency_key": f"kp_latest_{_sha256_hex({'pack_id': pack_id_norm, 'actor': actor_norm})[:20]}",
    }

    if not actor_norm:
        return {
            **pointer,
            "status": "blocked",
            "created": False,
            "replayed": False,
            "latest_pointer_path": "",
            "reasons": ["actor_missing"],
            "action_requests": [{"type": "needs_update", "reason": "actor_missing"}],
        }
    if not pack_id_norm:
        return {
            **pointer,
            "status": "blocked",
            "created": False,
            "replayed": False,
            "latest_pointer_path": "",
            "reasons": ["pack_id_missing"],
            "action_requests": [{"type": "needs_update", "reason": "pack_id_missing"}],
        }

    latest_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_json(latest_path)
    if existing is not None:
        if _is_same_payload(existing, pointer):
            return {
                **pointer,
                "status": "ready",
                "created": False,
                "replayed": True,
                "latest_pointer_path": str(latest_path),
            }
        if _normalize(existing.get("pack_id")) == pack_id_norm and _normalize(existing.get("actor")) == actor_norm:
            return {
                **pointer,
                "status": "blocked",
                "created": False,
                "replayed": False,
                "latest_pointer_path": str(latest_path),
                "reasons": ["idempotency_collision"],
                "action_requests": [{"type": "needs_update", "reason": "latest_pointer_collision", "pack_id": pack_id_norm}],
            }

    latest_path.write_text(_canonical_json(_payload_for_replay(pointer)) + "\n", encoding="utf-8")
    return {
        **pointer,
        "status": "ready",
        "created": True,
        "replayed": False,
        "latest_pointer_path": str(latest_path),
    }


__all__ = [
    "CANONICAL_SECTIONS",
    "CANONICAL_SECTION_FILES",
    "KNOWN_CLASSIFICATIONS",
    "project_understanding_protocol_metadata",
    "build_knowledge_pack_manifest_dry_run",
    "write_knowledge_pack_manifest_dry_run",
    "validate_knowledge_pack_manifest",
    "select_remote_safe_knowledge_subset",
    "write_remote_safe_subset_dry_run",
    "update_knowledge_pack_latest_pointer_dry_run",
]

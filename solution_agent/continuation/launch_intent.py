from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .paths import continuation_launch_intents_dir


LAUNCH_INTENT_SCHEMA_VERSION = 1
LAUNCH_INTENT_SCHEMA_ID = "bounded_remote_launch_intent"

FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"^/ssd[^/]*", re.IGNORECASE),
    re.compile(r"^/mnt[^/]*", re.IGNORECASE),
    re.compile(r"^/raid[^/]*", re.IGNORECASE),
    re.compile(r"^[A-Za-z]:\\\\", re.IGNORECASE),
    re.compile(r"^\\\\\\\\", re.IGNORECASE),
]

DEFAULT_SIDE_EFFECTS: Dict[str, bool] = {
    "ssh": False,
    "remote_write": False,
    "scheduler_dispatch": False,
    "registry_mutation": False,
    "running_state": False,
    "subprocess": False,
    "remote_execution": False,
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"", "0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(value)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _safe_intent_id(work_order_id: str, work_order_hash: str) -> str:
    base = _normalize(work_order_id) or "work_order"
    token = work_order_hash[:12] if work_order_hash else "000000000000"
    return f"li_{base}_{token}"


def _compute_idempotency_key(
    *,
    source_work_order_id: str,
    source_work_order_hash: str,
    actor: str,
    explicit_operator_approval: bool,
) -> str:
    payload = {
        "source_work_order_id": source_work_order_id,
        "source_work_order_hash": source_work_order_hash,
        "actor": actor,
        "explicit_operator_approval": bool(explicit_operator_approval),
    }
    return f"li_{_sha256_hex(payload)[:20]}"


def _is_path_guessed(path: str) -> bool:
    normalized = _normalize(path)
    return any(pattern.match(normalized) for pattern in FORBIDDEN_PATH_PATTERNS)


def _load_work_order(work_order: dict | Path | str) -> Dict[str, Any]:
    if isinstance(work_order, (Path, str)):
        payload = json.loads(Path(work_order).read_text(encoding="utf-8"))
    elif isinstance(work_order, dict):
        payload = dict(work_order)
    else:
        raise TypeError("work_order must be dict or path")

    if not isinstance(payload, dict):
        raise ValueError("work_order payload must be a JSON object")
    return payload


def _read_side_effects(raw_side_effects: Any) -> Dict[str, bool]:
    values = {key: False for key in DEFAULT_SIDE_EFFECTS}
    if not isinstance(raw_side_effects, dict):
        return values

    for key in values:
        values[key] = _normalize_bool(raw_side_effects.get(key), default=False)
    return values


def _extract_work_order_fields(work_order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "work_order_id": _normalize(work_order.get("work_order_id")),
        "source_hash": _normalize(work_order.get("source_work_order_hash")),
        "dispatch_request_id": _normalize(work_order.get("dispatch_request_id")),
        "task_id": _normalize(work_order.get("task_id")),
        "workplace_profile": _normalize(work_order.get("workplace_profile")),
        "host": _normalize(work_order.get("host")),
        "remote_workdir": _normalize(work_order.get("remote_workdir")),
        "remote_root": _normalize(work_order.get("remote_root")),
        "worker": _normalize(work_order.get("worker")),
        "line": _normalize(work_order.get("line")),
        "batch": _normalize(work_order.get("batch")),
        "dag_root": _normalize(work_order.get("dag_root")),
        "prefix": _normalize(work_order.get("prefix")),
        "resource_hint": _normalize(work_order.get("resource_hint")),
        "resource_lane": _normalize(work_order.get("resource_lane")),
        "execution_mode": _normalize(work_order.get("execution_mode")),
        "status": _normalize(work_order.get("status")),
        "path_authority": _normalize(work_order.get("path_authority")),
        "side_effects": _read_side_effects(work_order.get("side_effects")),
        "resource_fields": work_order.get("resource_fields", {}),
        "required_artifacts": work_order.get("required_artifacts", []),
        "capability_checks": work_order.get("capability_checks", []),
    }


def _required_fields_present(work_order: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not work_order["work_order_id"]:
        reasons.append("source_work_order_id_missing")
    if not work_order["dispatch_request_id"]:
        reasons.append("dispatch_request_id_missing")
    if not work_order["task_id"]:
        reasons.append("task_id_missing")
    if not work_order["line"] or not work_order["batch"] or not work_order["dag_root"] or not work_order["prefix"]:
        reasons.append("scope_field_missing")
    if not work_order["workplace_profile"]:
        reasons.append("workplace_profile_missing")
    if not work_order["host"]:
        reasons.append("host_missing")
    if not work_order["remote_root"]:
        reasons.append("remote_root_missing")
    if not work_order["remote_workdir"]:
        reasons.append("remote_workdir_missing")
    return reasons


def _check_work_order_status_and_mode(work_order: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if work_order["status"] != "ready_for_materialized_execution_handoff":
        reasons.append("work_order_status_not_ready_for_handoff")
    if work_order["execution_mode"] != "dry_run":
        reasons.append("work_order_execution_mode_not_dry_run")
    return reasons


def _check_worker_and_side_effects(work_order: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if work_order["worker"] != "materialized_execution":
        reasons.append("non_materialized_execution_worker")
    for key, expected_false in DEFAULT_SIDE_EFFECTS.items():
        if work_order["side_effects"].get(key) != expected_false:
            reasons.append("work_order_side_effects_enabled")
            break
    path_authority = work_order.get("path_authority")
    path_is_authorized = path_authority in {
        "task_contract_target_workplace",
        "workplace_profile",
        "mission_contract",
    }
    if not path_is_authorized and (_is_path_guessed(work_order["remote_root"]) or _is_path_guessed(work_order["remote_workdir"])):
        reasons.append("remote_path_guess")
    if work_order["remote_root"] and work_order["remote_workdir"] and not work_order["remote_workdir"].startswith(f"{work_order['remote_root']}/"):
        if work_order["remote_workdir"] != work_order["remote_root"]:
            reasons.append("remote_workdir_not_under_remote_root")
    return reasons


def assess_launch_intent_contract(
    work_order: dict | Path | str,
    *,
    actor: str,
    approve_remote_launch: bool = False,
) -> dict:
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []
    normalized_actor = _normalize(actor)
    if not normalized_actor:
        reasons.append("actor_missing")
        checks.append({"id": "actor_present", "status": "failed"})
    else:
        checks.append({"id": "actor_present", "status": "passed", "actor": normalized_actor})

    try:
        raw_work_order = _load_work_order(work_order)
        checks.append({"id": "work_order_load", "status": "passed"})
    except (TypeError, OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append({"id": "work_order_load", "status": "failed", "reason": str(exc)})
        return {
            "schema_version": LAUNCH_INTENT_SCHEMA_VERSION,
            "launch_intent_id": "",
            "idempotency_key": "",
            "source_work_order_id": "",
            "source_work_order_hash": "",
            "dispatch_request_id": "",
            "task_id": "",
            "actor": normalized_actor,
            "approval": {
                "remote_launch_requested": True,
                "explicit_operator_approval": bool(approve_remote_launch),
            },
            "workplace_profile": "",
            "host": "",
            "remote_workdir": "",
            "remote_root": "",
            "worker": "",
            "line": "",
            "batch": "",
            "dag_root": "",
            "prefix": "",
            "resource_hint": "",
            "resource_lane": "",
            "resource_fields": {},
            "execution_mode": LAUNCH_INTENT_SCHEMA_ID,
            "launch_enabled": False,
            "status": "blocked",
            "reasons": ["work_order_load_failed"],
            "side_effects": dict(DEFAULT_SIDE_EFFECTS),
            "checks": checks,
        }

    parsed = _extract_work_order_fields(raw_work_order)
    work_order_source_hash = _sha256_hex(raw_work_order)

    reasons.extend(_required_fields_present(parsed))
    checks.append({"id": "required_fields_present", "status": "passed" if not reasons else "failed", "missing": reasons})

    status_reasons = _check_work_order_status_and_mode(parsed)
    checks.append({"id": "work_order_ready_status_and_dry_run", "status": "passed" if not status_reasons else "failed", "reasons": status_reasons})
    reasons.extend(status_reasons)

    worker_effects_reasons = _check_worker_and_side_effects(parsed)
    checks.append(
        {
            "id": "worker_scope_and_side_effects",
            "status": "passed" if not worker_effects_reasons else "failed",
            "reasons": worker_effects_reasons,
        },
    )
    reasons.extend(worker_effects_reasons)

    if not approve_remote_launch:
        reasons.append("explicit_operator_approval_required")

    launch_intent_id = _safe_intent_id(parsed["work_order_id"], work_order_source_hash)
    checks.append({"id": "launch_intent_id", "status": "passed", "launch_intent_id": launch_intent_id})
    idempotency_key = _compute_idempotency_key(
        source_work_order_id=parsed["work_order_id"],
        source_work_order_hash=work_order_source_hash,
        actor=normalized_actor,
        explicit_operator_approval=bool(approve_remote_launch),
    )
    checks.append({"id": "idempotency_key", "status": "passed", "idempotency_key": idempotency_key})

    status = "blocked" if reasons else "ready_for_launch_preflight"
    return {
        "schema_version": LAUNCH_INTENT_SCHEMA_VERSION,
        "launch_intent_id": launch_intent_id,
        "idempotency_key": idempotency_key,
        "source_work_order_id": parsed["work_order_id"],
        "source_work_order_hash": work_order_source_hash,
        "dispatch_request_id": parsed["dispatch_request_id"],
        "task_id": parsed["task_id"],
        "actor": normalized_actor,
        "approval": {
            "remote_launch_requested": True,
            "explicit_operator_approval": bool(approve_remote_launch),
        },
        "workplace_profile": parsed["workplace_profile"],
        "host": parsed["host"],
        "remote_workdir": parsed["remote_workdir"],
        "remote_root": parsed["remote_root"],
        "worker": parsed["worker"],
        "line": parsed["line"],
        "batch": parsed["batch"],
        "dag_root": parsed["dag_root"],
        "prefix": parsed["prefix"],
        "resource_hint": parsed["resource_hint"],
        "resource_lane": parsed["resource_lane"],
        "resource_fields": parsed["resource_fields"] if isinstance(parsed["resource_fields"], dict) else {},
        "path_authority": parsed["path_authority"],
        "execution_mode": LAUNCH_INTENT_SCHEMA_ID,
        "launch_enabled": False,
        "status": status,
        "reasons": sorted(set(_normalize(r) for r in reasons)),
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        "checks": checks,
    }


def create_launch_intent_dry_run(
    work_order: dict | Path | str,
    *,
    actor: str,
    approve_remote_launch: bool = False,
) -> dict:
    assessment = assess_launch_intent_contract(
        work_order,
        actor=actor,
        approve_remote_launch=approve_remote_launch,
    )

    launch_intent_id = _normalize(assessment.get("launch_intent_id"))
    if not launch_intent_id:
        return {
            **assessment,
            "created": False,
            "replayed": False,
            "launch_intent_path": "",
            "action_required": ["fix_launch_intent_payload"],
        }

    launch_intent_root = continuation_launch_intents_dir()
    launch_intent_path = launch_intent_root / f"{launch_intent_id}.json"
    assessment["checks"].append(
        {
            "id": "launch_intent_path_ready",
            "status": "passed",
            "path": str(launch_intent_path),
        },
    )

    if assessment.get("status") != "ready_for_launch_preflight":
        return {
            **assessment,
            "created": False,
            "replayed": False,
            "launch_intent_path": str(launch_intent_path),
        }

    launch_intent_root.mkdir(parents=True, exist_ok=True)

    if launch_intent_path.exists():
        try:
            existing = json.loads(launch_intent_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                return {
                    **assessment,
                    "status": "blocked",
                    "reasons": ["launch_intent_payload_corrupt"],
                    "created": False,
                    "replayed": False,
                    "launch_intent_path": str(launch_intent_path),
                }
            if _canonical_json(existing) == _canonical_json(assessment):
                return {
                    **assessment,
                    "created": False,
                    "replayed": True,
                    "launch_intent_path": str(launch_intent_path),
                }
            if _normalize(existing.get("idempotency_key")) == _normalize(assessment.get("idempotency_key")):
                return {
                    **assessment,
                    "status": "blocked",
                    "reasons": ["idempotency_collision"],
                    "created": False,
                    "replayed": False,
                    "launch_intent_path": str(launch_intent_path),
                }
            return {
                **assessment,
                "status": "blocked",
                "reasons": ["launch_intent_id_collision"],
                "created": False,
                "replayed": False,
                "launch_intent_path": str(launch_intent_path),
            }
        except json.JSONDecodeError:
            return {
                **assessment,
                "status": "blocked",
                "reasons": ["launch_intent_payload_corrupt"],
                "created": False,
                "replayed": False,
                "launch_intent_path": str(launch_intent_path),
            }

    launch_intent_payload = dict(assessment)
    launch_intent_path.write_text(_canonical_json(launch_intent_payload) + "\n", encoding="utf-8")
    return {
        **assessment,
        "created": True,
        "replayed": False,
        "launch_intent_path": str(launch_intent_path),
        "checks": list(assessment["checks"]),
    }


__all__ = [
    "assess_launch_intent_contract",
    "create_launch_intent_dry_run",
]

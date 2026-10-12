from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .knowledge_to_proposal import _coerce_nested_readiness_payload
from .paths import (
    continuation_night_auto_dry_run_dir,
    continuation_night_auto_work_order_path,
)


WORK_ORDER_SCHEMA_VERSION = 1
NIGHT_AUTO_WORK_ORDER_CYCLE_ID = "2026-05-23T_SPARK_P4E_NIGHT_AUTO_WORK_ORDER_01"
NIGHT_AUTO_SCHEDULER_VISIBLE = False
NIGHT_AUTO_SIDE_EFFECTS = {
    "scheduler_dispatch": False,
    "active_inbox_write": False,
    "registry_mutation": False,
    "remote_api_call": False,
    "dataset_or_checkpoint_mutation": False,
}
NIGHT_AUTO_DEFAULT_CLAIM_LIMITS = [
    "no_scheduler_dispatch",
    "no_active_inbox_write",
    "no_active_kb_mutation",
    "no_registry_mutation",
    "no_remote_api_call",
    "no_dataset_or_checkpoint_mutation",
]


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
    normalized = _normalize_lower(value)
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return default


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _coerce_claim_limits(raw: Any) -> List[str]:
    if isinstance(raw, str):
        normalized = _normalize(raw)
        return [normalized] if normalized else list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)
    if isinstance(raw, (list, tuple, set)):
        values = [_normalize(item) for item in raw]
        values = [item for item in values if item]
        return values or list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)
    return list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)


def _safe_candidate_id(candidate: Dict[str, Any]) -> str:
    candidate_id = _normalize(candidate.get("candidate_id"))
    return candidate_id or "unknown_candidate"


def _build_source_decision(candidate: Dict[str, Any], mission_id: str) -> Dict[str, Any]:
    parent_decision_id = _normalize(candidate.get("parent_decision_id"))
    source_decision = {
        "mission_id": mission_id,
        "parent_decision_id": parent_decision_id,
        "candidate_id": _safe_candidate_id(candidate),
        "decision_id": _normalize(candidate.get("source_decision_id") or parent_decision_id),
    }
    next_step_readiness = _coerce_nested_readiness_payload(candidate.get("next_step_readiness"))
    if next_step_readiness:
        source_decision["next_step_readiness"] = next_step_readiness
    return source_decision


def _build_work_order(candidate: Dict[str, Any], proposal_run_id: str | None, actor: str | None) -> Dict[str, Any]:
    mission_id = _normalize(candidate.get("mission_id"))
    parent_decision_id = _normalize(candidate.get("parent_decision_id"))
    mission_gate_pass = _coerce_bool(candidate.get("mission_gate_pass"), default=False)
    candidate_id = _safe_candidate_id(candidate)
    intended_execution_type = _normalize(candidate.get("kind")) or "bounded_task"
    claim_limits = _coerce_claim_limits(candidate.get("claim_limits"))

    idempotency_key_payload = {
        "proposal_run_id": _normalize(proposal_run_id),
        "candidate_id": candidate_id,
        "mission_id": mission_id,
        "parent_decision_id": parent_decision_id,
        "intended_execution_type": intended_execution_type,
        "continuation_lane": "night_auto",
    }
    idempotency_key = _sha256_hex(idempotency_key_payload)
    work_order_id = f"nawo_{_sha256_hex(idempotency_key)[:16]}"

    return {
        "schema_version": WORK_ORDER_SCHEMA_VERSION,
        "work_order_id": work_order_id,
        "idempotency_key": idempotency_key,
        "proposal_run_id": _normalize(proposal_run_id),
        "mission_id": mission_id,
        "parent_decision_id": parent_decision_id,
        "mission_gate_pass": mission_gate_pass,
        "continuation_lane": "night_auto",
        "source_decision": _build_source_decision(candidate, mission_id),
        "intended_execution_type": intended_execution_type,
        "candidate_id": candidate_id,
        "claim_limits": sorted(set(claim_limits)),
        "status": "created",
        "actor": _normalize(actor or "system"),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scheduler_visible": NIGHT_AUTO_SCHEDULER_VISIBLE,
        "side_effects": dict(NIGHT_AUTO_SIDE_EFFECTS),
        "line": _normalize(candidate.get("line")),
        "workplace_profile": _normalize(candidate.get("workplace_profile")),
    }


def write_night_auto_work_order_dry_run(
    candidate: Dict[str, Any] | None,
    *,
    proposal_run_id: str | None = None,
    actor: str | None = None,
) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        raise TypeError("candidate must be a dict")

    validated = dict(candidate)
    mission_id = _normalize(validated.get("mission_id"))
    candidate_id = _safe_candidate_id(validated)
    work_order_payload = _build_work_order(validated, proposal_run_id=proposal_run_id, actor=actor)
    order_dir = continuation_night_auto_dry_run_dir(mission_id, proposal_run_id, candidate_id)
    work_order_path = continuation_night_auto_work_order_path(
        mission_id,
        proposal_run_id,
        candidate_id,
        work_order_id=work_order_payload["work_order_id"],
    )

    if work_order_path.exists():
        existing = json.loads(work_order_path.read_text(encoding="utf-8"))
        if existing == work_order_payload:
            return {
                "status": "replayed",
                "created": False,
                "replayed": True,
                "action": "night_auto_work_order_replayed",
                "work_order_path": str(work_order_path),
                "work_order": work_order_payload,
                "proposal_run_id": _normalize(proposal_run_id),
                "night_auto_work_order_dir": str(order_dir),
                "reasons": [],
            }

        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_work_order_idempotency_collision",
            "work_order_path": str(work_order_path),
            "work_order": work_order_payload,
            "proposal_run_id": _normalize(proposal_run_id),
            "night_auto_work_order_dir": str(order_dir),
            "reasons": ["idempotency_collision"],
        }

    order_dir.mkdir(parents=True, exist_ok=True)
    work_order_path.write_text(_canonical_json(work_order_payload) + "\n", encoding="utf-8")
    return {
        "status": "created",
        "created": True,
        "replayed": False,
        "action": "night_auto_work_order_written",
        "work_order_path": str(work_order_path),
        "work_order": work_order_payload,
        "proposal_run_id": _normalize(proposal_run_id),
        "night_auto_work_order_dir": str(order_dir),
        "reasons": [],
        "cycle_id": NIGHT_AUTO_WORK_ORDER_CYCLE_ID,
    }


def build_night_auto_work_order_dry_run(
    candidate: Dict[str, Any] | None,
    *,
    proposal_run_id: str | None = None,
    actor: str | None = None,
) -> Dict[str, Any]:
    return write_night_auto_work_order_dry_run(candidate, proposal_run_id=proposal_run_id, actor=actor)


__all__ = ["build_night_auto_work_order_dry_run", "write_night_auto_work_order_dry_run"]

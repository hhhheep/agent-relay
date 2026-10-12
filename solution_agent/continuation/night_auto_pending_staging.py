from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .paths import (
    continuation_night_auto_pending_artifact_path,
)

PENDING_ARTIFACT_SCHEMA_VERSION = 1
NIGHT_AUTO_PENDING_CYCLE_ID = "2026-05-23T_SPARK_P4E_NIGHT_AUTO_PENDING_STAGING_01"
NIGHT_AUTO_PENDING_SIDE_EFFECTS = {
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


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = _normalize(value).lower()
    if text in {"", "0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _coerce_claim_limits(raw: Any) -> List[str]:
    if isinstance(raw, str):
        normalized = _normalize(raw)
        return [normalized] if normalized else list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)
    if isinstance(raw, (list, tuple, set)):
        values = [_normalize(item) for item in raw]
        values = [item for item in values if item]
        return values or list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)
    return list(NIGHT_AUTO_DEFAULT_CLAIM_LIMITS)


def _coerce_source_decision(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_decision = payload.get("source_decision")
    if isinstance(source_decision, dict):
        return dict(source_decision)
    return {
        "mission_id": _normalize(payload.get("mission_id")),
        "parent_decision_id": _normalize(payload.get("parent_decision_id")),
        "candidate_id": _normalize(payload.get("candidate_id")),
        "decision_id": _normalize(payload.get("decision_id")),
    }


def _build_payload(work_order: Dict[str, Any], *, proposal_run_id: str | None, actor: str | None, scheduler_visible: bool) -> Dict[str, Any]:
    mission_id = _normalize(work_order.get("mission_id"))
    parent_decision_id = _normalize(work_order.get("parent_decision_id"))
    candidate_id = _normalize(work_order.get("candidate_id"))
    claim_limits = _coerce_claim_limits(work_order.get("claim_limits"))
    work_order_status = _normalize(work_order.get("status"))
    idempotency_key_payload = {
        "proposal_run_id": _normalize(proposal_run_id),
        "candidate_id": candidate_id,
        "mission_id": mission_id,
        "parent_decision_id": parent_decision_id,
        "continuation_lane": "night_auto",
        "work_order_id": _normalize(work_order.get("work_order_id")),
        "scheduler_visible": bool(scheduler_visible),
    }
    idempotency_key = _sha256_hex(idempotency_key_payload)
    pending_artifact_id = f"naap_{_sha256_hex(idempotency_key)[:16]}"
    return {
        "schema_version": PENDING_ARTIFACT_SCHEMA_VERSION,
        "pending_artifact_id": pending_artifact_id,
        "idempotency_key": idempotency_key,
        "proposal_run_id": _normalize(proposal_run_id),
        "mission_id": mission_id,
        "parent_decision_id": parent_decision_id,
        "mission_gate_pass": _coerce_bool(work_order.get("mission_gate_pass"), default=False),
        "continuation_lane": "night_auto",
        "source_decision": _coerce_source_decision(work_order),
        "work_order_id": _normalize(work_order.get("work_order_id")),
        "work_order_path": _normalize(work_order.get("work_order_path")),
        "candidate_id": candidate_id,
        "claim_limits": sorted(set(claim_limits)),
        "status": work_order_status or "created",
        "actor": _normalize(actor or work_order.get("actor")),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scheduler_visible": bool(scheduler_visible),
        "side_effects": dict(NIGHT_AUTO_PENDING_SIDE_EFFECTS),
    }


def write_night_auto_pending_staging_artifact(
    work_order: Dict[str, Any] | None,
    *,
    proposal_run_id: str | None = None,
    actor: str | None = None,
    scheduler_visible: bool = False,
) -> Dict[str, Any]:
    if not isinstance(work_order, dict):
        raise TypeError("work_order must be a dict")

    if scheduler_visible:
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_blocked_by_scheduler_visibility",
            "pending_artifact_path": None,
            "pending_artifact": None,
            "proposal_run_id": _normalize(proposal_run_id),
            "reasons": ["scheduler_visible_true_not_allowed_for_pending_bridge"],
        }

    mission_id = _normalize(work_order.get("mission_id"))
    parent_decision_id = _normalize(work_order.get("parent_decision_id"))
    candidate_id = _normalize(work_order.get("candidate_id"))
    continuation_lane = _normalize(work_order.get("continuation_lane")).lower()

    if continuation_lane != "night_auto":
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_lane_mismatch",
            "pending_artifact_path": None,
            "pending_artifact": None,
            "proposal_run_id": _normalize(proposal_run_id),
            "reasons": ["continuation_lane_must_be_night_auto"],
        }

    if not mission_id or not parent_decision_id:
        reasons = []
        if not mission_id:
            reasons.append("pending_missing_mission_id")
        if not parent_decision_id:
            reasons.append("pending_missing_parent_decision_id")
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_blocked",
            "pending_artifact_path": None,
            "pending_artifact": None,
            "proposal_run_id": _normalize(proposal_run_id),
            "reasons": reasons,
        }

    if not _coerce_bool(work_order.get("mission_gate_pass"), default=False):
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_blocked",
            "pending_artifact_path": None,
            "pending_artifact": None,
            "proposal_run_id": _normalize(proposal_run_id),
            "reasons": ["pending_night_auto_requires_mission_gate_pass"],
        }

    side_effects = work_order.get("side_effects")
    if not isinstance(side_effects, dict):
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_blocked",
            "pending_artifact_path": None,
            "pending_artifact": None,
            "proposal_run_id": _normalize(proposal_run_id),
            "reasons": ["pending_work_order_missing_side_effects"],
        }

    for key, value in NIGHT_AUTO_PENDING_SIDE_EFFECTS.items():
        if _coerce_bool(side_effects.get(key), default=True) != _coerce_bool(value, default=False):
            return {
                "status": "blocked",
                "created": False,
                "replayed": False,
                "action": "night_auto_pending_blocked",
                "pending_artifact_path": None,
                "pending_artifact": None,
                "proposal_run_id": _normalize(proposal_run_id),
                "reasons": [f"pending_side_effect_violation:{key}"],
            }

    payload = _build_payload(
        work_order,
        proposal_run_id=_normalize(proposal_run_id or work_order.get("proposal_run_id")),
        actor=actor,
        scheduler_visible=scheduler_visible,
    )
    pending_artifact_id = payload["pending_artifact_id"]
    pending_path = continuation_night_auto_pending_artifact_path(
        mission_id,
        _normalize(proposal_run_id or work_order.get("proposal_run_id")),
        candidate_id,
        pending_artifact_id=pending_artifact_id,
    )

    if pending_path.exists():
        existing = json.loads(pending_path.read_text(encoding="utf-8"))
        if existing == payload:
            return {
                "status": "replayed",
                "created": False,
                "replayed": True,
                "action": "night_auto_pending_replayed",
                "pending_artifact_path": str(pending_path),
                "pending_artifact": payload,
                "proposal_run_id": _normalize(proposal_run_id or work_order.get("proposal_run_id")),
                "reasons": [],
                "night_auto_pending_cycle_id": NIGHT_AUTO_PENDING_CYCLE_ID,
            }
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "action": "night_auto_pending_idempotency_collision",
            "pending_artifact_path": str(pending_path),
            "pending_artifact": payload,
            "proposal_run_id": _normalize(proposal_run_id or work_order.get("proposal_run_id")),
            "reasons": ["pending_idempotency_collision"],
        }

    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
    return {
        "status": "created",
        "created": True,
        "replayed": False,
        "action": "night_auto_pending_artifact_written",
        "pending_artifact_path": str(pending_path),
        "pending_artifact": payload,
        "proposal_run_id": _normalize(proposal_run_id or work_order.get("proposal_run_id")),
        "reasons": [],
        "night_auto_pending_cycle_id": NIGHT_AUTO_PENDING_CYCLE_ID,
    }


__all__ = ["write_night_auto_pending_staging_artifact"]

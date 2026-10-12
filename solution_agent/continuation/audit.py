from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict

from .paths import continuation_audit_events_path
from .schema import AuditAction, AuditActor, AuditEvent, AuditOutcome, ContinuationPlan


def append_audit_event(event: AuditEvent) -> Dict[str, Any]:
    """Append one audit event under continuation/audit only."""

    path = continuation_audit_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")
    return {"written": True, "path": str(path), "event_id": event.event_id}


def record_draft_write_audit_stub(
    plan: ContinuationPlan,
    draft_results: list[dict[str, Any]],
    target: str,
    policy_version: str = "0.1.0",
) -> Dict[str, Any]:
    """Write one draft-write audit entry while staying P0-only and file-local."""

    has_collision = any(item.get("outcome") == "collision" for item in draft_results)
    normalized = json.dumps(draft_results, sort_keys=True, indent=2)
    event = AuditEvent(
        event_id=f"{plan.source_task_id}:draft_write:{int(datetime.now(tz=timezone.utc).timestamp() * 1000)}",
        timestamp=datetime.now(timezone.utc),
        actor=AuditActor.CONTINUATION_PLANNER,
        action=AuditAction.WRITE,
        target=target,
        idempotency_key=plan.idempotency_key,
        outcome=AuditOutcome.FAIL if has_collision else AuditOutcome.SUCCESS,
        source_task_id=plan.source_task_id,
        prompt_versions={"continuation_planner": "0.1.0", "writer": "0.1.0"},
        policy_version=policy_version,
        inputs_hash=plan.interpretation_hash,
        outputs_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
    return append_audit_event(event)

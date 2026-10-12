from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from .audit import append_audit_event
from .schema import AuditEvent, AuditOutcome, ContinuationState, HistoryEntry, ProposedTaskRecord


TERMINAL_STATES = {
    ContinuationState.STAGING_PASSED,
    ContinuationState.PROMOTED_TO_INBOX,
    ContinuationState.REJECTED,
    ContinuationState.REVOKED,
    ContinuationState.EXPIRED,
}


def is_terminal_state(state: ContinuationState | str) -> bool:
    return ContinuationState(state) in TERMINAL_STATES


def assert_no_promotion_in_p0() -> dict:
    return {
        "promotion_allowed": False,
        "reason": "P0 skeleton forbids inbox promotion and scheduler dispatch.",
    }


def _terminal_event(
    *,
    record: ProposedTaskRecord,
    action: str,
    actor: str,
    reason: str,
    outcome: AuditOutcome,
) -> AuditEvent:
    now = datetime.now(timezone.utc)
    return AuditEvent(
        event_id=f"{record.proposed_task_id}:{action}:{int(now.timestamp())}",
        timestamp=now,
        actor=actor,
        action=action,
        target=record.proposed_task_id,
        idempotency_key=f"{record.proposed_task_id}:{action}",
        outcome=outcome,
        source_task_id=record.source_task_id,
        prompt_versions=record.prompt_versions,
        policy_version=record.policy_version,
        error_detail=reason or None,
        inputs_hash=record.source_result_hash,
        outputs_hash=f"{record.proposed_task_id}:{action}:{outcome.value}",
    )


def _terminal_transition_stub(
    *,
    record: ProposedTaskRecord,
    target_state: ContinuationState,
    action: str,
    actor: str = "human",
    reason: str = "",
    persist_audit: bool = True,
) -> Dict[str, Any]:
    """P0 lifecycle transition: update record state and append audit only."""

    history_map: Dict[ContinuationState, str] = {
        ContinuationState.STAGING_PASSED: "approval_history",
        ContinuationState.REJECTED: "rejection_history",
        ContinuationState.REVOKED: "rejection_history",
        ContinuationState.EXPIRED: "expiry_history",
    }

    if target_state == ContinuationState.PROMOTED_TO_INBOX:
        raise ValueError("P0 lifecycle transitions are stubbed and do not promote tasks.")

    if target_state not in history_map:
        raise ValueError("P0 lifecycle skeleton only supports explicit audit-only transitions")

    history_field = history_map[target_state]

    if is_terminal_state(record.state):
        event = _terminal_event(
            record=record,
            action=action,
            actor=actor,
            reason=f"already terminal: {record.state.value}",
            outcome=AuditOutcome.SKIP,
        )
        audit = append_audit_event(event) if persist_audit else {"written": False, "event_id": event.event_id}
        return {"record": record, "event": event, "audit": audit, "changed": False}

    now = datetime.now(timezone.utc)
    event = _terminal_event(
        record=record,
        action=action,
        actor=actor,
        reason=reason,
        outcome=AuditOutcome.SUCCESS,
    )
    history = HistoryEntry(at=now, actor=actor, reason=reason, audit_event_id=event.event_id)
    updated = record.model_copy(
        update={
            "state": target_state,
            history_field: [*getattr(record, history_field), history],
        }
    )
    audit = append_audit_event(event) if persist_audit else {"written": False, "event_id": event.event_id}
    return {"record": updated, "event": event, "audit": audit, "changed": True}


def reject_record_stub(record: ProposedTaskRecord, *, reason: str, actor: str = "human") -> Dict[str, Any]:
    return _terminal_transition_stub(
        record=record,
        target_state=ContinuationState.REJECTED,
        action="reject",
        actor=actor,
        reason=reason,
    )


def revoke_record_stub(record: ProposedTaskRecord, *, reason: str, actor: str = "human") -> Dict[str, Any]:
    return _terminal_transition_stub(
        record=record,
        target_state=ContinuationState.REVOKED,
        action="revoke",
        actor=actor,
        reason=reason,
    )


def approve_record_stub(record: ProposedTaskRecord, *, reason: str, actor: str = "human") -> Dict[str, Any]:
    """Record approval with audit-only side effects."""

    return _terminal_transition_stub(
        record=record,
        target_state=ContinuationState.STAGING_PASSED,
        action="approve",
        actor=actor,
        reason=reason,
    )


def expire_record_stub(record: ProposedTaskRecord, *, reason: str, actor: str = "human") -> Dict[str, Any]:
    """Record expiry with audit-only side effects."""

    return _terminal_transition_stub(
        record=record,
        target_state=ContinuationState.EXPIRED,
        action="expire",
        actor=actor,
        reason=reason,
    )

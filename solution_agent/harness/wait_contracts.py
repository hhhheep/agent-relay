from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import StrictModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


WaitLifecycle = Literal["WAITING_EXTERNAL", "COLLECTING", "REVIEW_REQUIRED", "CLOSED"]
ObservationStatus = Literal["available", "unavailable", "stale"]
EvidenceStatus = Literal["not_collected", "collecting", "complete", "incomplete", "invalid"]
ScientificStatus = Literal[
    "not_assessed",
    "hypothesis_supported",
    "hypothesis_not_supported",
    "inconclusive",
    "invalid_evidence",
    "not_applicable",
]
WaitEventKind = Literal[
    "COMPLETION_CANDIDATE",
    "EXECUTION_FAILURE_VERIFIED",
    "OBSERVATION_UNAVAILABLE",
    "OBSERVATION_RESTORED",
    "PROGRESS_ADVANCED",
    "STALLED",
    "DECISION_REQUIRED",
    "BUDGET_EXCEEDED",
    "COLLECTION_VERIFIED",
    "COLLECTION_REJECTED",
]


class WaitRunState(StrictModel):
    schema_version: Literal["harness-wait-state/v1"] = "harness-wait-state/v1"
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    spec_id: str = Field(min_length=1, max_length=256)
    spec_version: int = Field(default=1, ge=1)
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_version: int = Field(default=1, ge=1)
    lifecycle_state: WaitLifecycle = "WAITING_EXTERNAL"
    execution_status: str = Field(min_length=1, max_length=128)
    observation_status: ObservationStatus = "available"
    evidence_status: EvidenceStatus = "not_collected"
    scientific_status: ScientificStatus = "not_assessed"
    execution_submission_count: int = Field(default=1, ge=0)
    execution_retry_count: int = Field(default=0, ge=0)
    observation_retry_count: int = Field(default=0, ge=0)
    model_dispatch_count: int = Field(default=0, ge=0)
    collect_transition_count: int = Field(default=0, ge=0)
    monitor_heartbeat_at: datetime | None = None
    transport_observed_at: datetime | None = None
    progress_observed_at: datetime | None = None
    process_observed_at: datetime | None = None
    last_progress_marker: str | None = Field(default=None, max_length=512)
    stall_after_seconds: float | None = Field(default=None, gt=0)
    stalled_event_emitted: bool = False
    applied_event_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_applied_events(self) -> "WaitRunState":
        if len(self.applied_event_ids) != len(set(self.applied_event_ids)):
            raise ValueError("applied_event_ids contains duplicates")
        return self


class WaitEvent(StrictModel):
    schema_version: Literal["harness-wait-event/v1"] = "harness-wait-event/v1"
    event_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    state_version: int = Field(ge=1)
    kind: WaitEventKind
    producer_id: str = Field(default="local-monitor", min_length=1, max_length=256)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_payload_hash(self) -> "WaitEvent":
        canonical = json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != self.payload_sha256:
            raise ValueError("payload_sha256 does not match the canonical payload")
        return self


class WaitAcknowledgement(StrictModel):
    schema_version: Literal["harness-wait-ack/v1"] = "harness-wait-ack/v1"
    consumer_id: str = "outer-controller"
    event_id: str
    run_id: str
    result: Literal["applied", "duplicate", "stale", "quarantined", "rejected"]
    duplicate: bool = False
    committed_state_version: int = Field(ge=1)
    wake_required: bool = False
    model_dispatch_allowed: bool = False
    detail: str | None = None
    received_at: datetime = Field(default_factory=utc_now)


class WaitDisposition(StrictModel):
    schema_version: Literal["harness-wait-disposition/v1"] = "harness-wait-disposition/v1"
    lifecycle_state: WaitLifecycle
    state_version: int = Field(ge=1)
    suppress_model_continuation: bool
    wake_required: bool
    resume_mode: Literal["external_event_or_manual"] = "external_event_or_manual"
    next_model_action: Literal["END_TURN", "COLLECT", "REVIEW", "NONE"]
    state_path: str
    wait_handle: str
    run_id: str
    attempt_id: str
    resume_trigger: list[str] = Field(default_factory=list)
    manual_resume_required: bool
    host_resume_mode: Literal["supported", "manual", "unavailable"]


__all__ = [
    "WaitAcknowledgement",
    "WaitDisposition",
    "WaitEvent",
    "WaitRunState",
    "utc_now",
]

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterpretationCategory(str, Enum):
    SUCCESS_BUT_WEAK_EVIDENCE = "success_but_weak_evidence"
    MISSING_ARTIFACT = "missing_artifact"
    MISSING_CAPABILITY = "missing_capability"
    MISSING_EVIDENCE = "missing_evidence"
    SCHEDULER_RESOURCE_WAIT = "scheduler_resource_wait"
    SOURCE_CODE_REQUIRED = "source_code_required"
    NEEDS_USER_DECISION = "needs_user_decision"
    TRUE_BUG = "true_bug"
    UNCOLLECTABLE = "uncollectable"
    UNKNOWN = "unknown"


class NextStepKind(str, Enum):
    BOUNDED_PROBE = "bounded_probe"
    INVENTORY_AUDIT = "inventory_audit"
    METRIC_COMPLETION = "metric_completion"
    DECISION_SYNTHESIS = "decision_synthesis"
    MISSING_CAPABILITY_REQUEST = "missing_capability_request"
    CODEX_DEBUG_REQUEST = "codex_debug_request"
    NO_SAFE_CONTINUATION = "no_safe_continuation"


class ContinuationState(str, Enum):
    DRAFTED = "drafted"
    STAGING_FAILED = "staging_failed"
    STAGING_PASSED = "staging_passed"
    PROMOTED_TO_INBOX = "promoted_to_inbox"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ForbiddenAction(str, Enum):
    DIRECT_DISPATCH = "direct_dispatch"
    SCHEDULER_SUBMIT = "scheduler_submit"
    REGISTRY_MUTATION = "registry_mutation"
    SOURCE_EDIT = "source_edit"
    REMOTE_PATH_GUESS = "remote_path_guess"
    CROSS_WORKPLACE_DATA_MIX = "cross_workplace_data_mix"
    CROSS_LINE_CONTINUATION = "cross_line_continuation"
    MANUAL_FILE_PROMOTION = "manual_file_promotion"


class AuditActor(str, Enum):
    CONTINUATION_PLANNER = "continuation_planner"
    RESULT_INTERPRETER = "result_interpreter"
    STAGING_GATE = "staging_gate"
    HUMAN = "human"
    CLI = "cli"
    AUTOPROMOTE = "autopromote"


class AuditAction(str, Enum):
    INTERPRET = "interpret"
    PLAN = "plan"
    WRITE = "write"
    STAGE = "stage"
    REJECT = "reject"
    REVOKE = "revoke"
    EXPIRE = "expire"
    APPROVE = "approve"
    PROMOTE = "promote"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    SKIP = "skip"
    FAIL = "fail"
    COLLISION = "collision"


class CliErrorCode(str, Enum):
    TASK_NOT_FOUND = "E_TASK_NOT_FOUND"
    RESULT_MISSING = "E_RESULT_MISSING"
    ALREADY_INTERPRETED = "E_ALREADY_INTERPRETED"
    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    NO_INTERPRETATION = "E_NO_INTERPRETATION"
    POLICY_VIOLATION = "E_POLICY_VIOLATION"
    MAX_TASKS_EXCEEDED = "E_MAX_TASKS_EXCEEDED"
    LINT_FAILED = "E_LINT_FAILED"
    DRYRUN_FAILED = "E_DRYRUN_FAILED"
    FORBIDDEN_ACTION_DETECTED = "E_FORBIDDEN_ACTION_DETECTED"
    GATE_NOT_PASSED = "E_GATE_NOT_PASSED"
    ALREADY_PROMOTED = "E_ALREADY_PROMOTED"
    HUMAN_APPROVAL_REQUIRED = "E_HUMAN_APPROVAL_REQUIRED"


class ResultInterpretation(StrictModel):
    schema_version: int = 1
    source_task_id: str
    source_result_hash: str
    idempotency_key: str
    category: InterpretationCategory
    execution_success: bool = False
    blocks_execution: bool = False
    blocks_claim_not_execution: bool = False
    needs_codex: bool = False
    needs_user_decision: bool = False
    recommended_continuation_kinds: List[NextStepKind] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    claim_limitations: List[str] = Field(default_factory=list)


class ProposedNextStep(StrictModel):
    step_id: str
    kind: NextStepKind
    reason: str
    owner: str
    can_stage: bool
    expected_outputs: List[str] = Field(default_factory=list)
    forbidden_actions: List[ForbiddenAction] = Field(default_factory=list)
    claim_limitation: str = ""


class ProposedTaskDraft(StrictModel):
    schema_version: int = 1
    draft_id: str
    source_task_id: str
    source_result_hash: str
    reason_for_continuation: str
    goal: str
    scope: str
    allowed_actions: List[str] = Field(default_factory=list)
    forbidden_actions: List[ForbiddenAction] = Field(default_factory=list)
    expected_outputs: List[str] = Field(default_factory=list)
    claim_limitation: str
    idempotency_key: str


class DraftRenderInput(StrictModel):
    draft_id: str
    source_task_id: str
    source_result_hash: str
    reason_for_continuation: str
    goal: str
    scope: str
    allowed_actions: List[str] = Field(default_factory=list)
    forbidden_actions: List[ForbiddenAction] = Field(default_factory=list)
    expected_outputs: List[str] = Field(default_factory=list)
    claim_limitation: str
    idempotency_key: str


class ContinuationPlan(StrictModel):
    schema_version: int = 1
    plan_id: str
    source_task_id: str
    interpretation_hash: str
    idempotency_key: str
    proposed_next_steps: List[ProposedNextStep] = Field(default_factory=list)
    no_safe_continuation_reason: Optional[str] = None

    @field_validator("proposed_next_steps")
    @classmethod
    def _bounded_steps(cls, value: List[ProposedNextStep]) -> List[ProposedNextStep]:
        if len(value) > 3:
            raise ValueError("continuation plans may include at most 3 proposed next steps")
        return value


class HistoryEntry(StrictModel):
    at: datetime
    actor: AuditActor
    reason: str = ""
    audit_event_id: Optional[str] = None


class ProposedTaskRecord(StrictModel):
    schema_version: int = 1
    proposed_task_id: str
    state: ContinuationState
    source_task_id: str
    source_result_hash: str
    idempotency_key: str
    prompt_versions: Dict[str, str]
    policy_version: str
    created_at: datetime
    created_by: AuditActor
    promotion_history: List[HistoryEntry] = Field(default_factory=list)
    rejection_history: List[HistoryEntry] = Field(default_factory=list)
    approval_history: List[HistoryEntry] = Field(default_factory=list)
    expiry_history: List[HistoryEntry] = Field(default_factory=list)


class PromotionReport(StrictModel):
    schema_version: int = 1
    report_id: str
    proposed_task_id: str
    source_task_id: str
    source_result_hash: str
    idempotency_key: str
    gate_status: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    violations: List[Dict[str, Any]] = Field(default_factory=list)
    prompt_versions: Dict[str, str] = Field(default_factory=dict)
    policy_version: str

    @field_validator("gate_status")
    @classmethod
    def _gate_status(cls, value: str) -> str:
        allowed = {"passed", "failed", "manual_review"}
        if value not in allowed:
            raise ValueError(f"gate_status must be one of {sorted(allowed)}")
        return value


class StagingDryRunManifest(StrictModel):
    schema_version: int = 1
    manifest_id: str
    plan_id: str
    source_task_id: str
    idempotency_key: str
    dry_run: bool = True
    proposed_task_count: int
    writes_allowed: List[str] = Field(default_factory=list)
    writes_forbidden: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    generated_drafts: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _p0_manifest_must_remain_dry_run(self) -> "StagingDryRunManifest":
        if not self.dry_run:
            raise ValueError("P0 staging manifests must be dry-run only")
        forbidden_text = "\n".join(self.writes_forbidden)
        for required in ("tasks/inbox/", "registry"):
            if required not in forbidden_text:
                raise ValueError(f"P0 staging manifests must forbid {required}")
        return self


class ProposalManifest(StrictModel):
    schema_version: int = 1
    manifest_id: str
    bundle_id: str
    source_task_id: str
    source_result_hash: str
    plan_id: str
    proposal_path: str
    draft_count: int
    proposed_paths: List[str] = Field(default_factory=list)
    writes_forbidden: List[str] = Field(default_factory=list)
    writes_allowed: List[str] = Field(default_factory=list)
    checks: List[Dict[str, str]] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    dry_run: bool = True
    idempotency_key: str

    @model_validator(mode="after")
    def _proposal_manifest_is_dry_run(self) -> "ProposalManifest":
        if not self.dry_run:
            raise ValueError("proposal manifests must represent dry-run proposal writes")
        forbidden_text = "\n".join(self.writes_forbidden)
        for required in ("tasks/inbox/", "registry"):
            if required not in forbidden_text:
                raise ValueError(f"proposal manifests must forbid {required}")
        return self


class ProposedBundle(StrictModel):
    schema_version: int = 1
    bundle_id: str
    proposal_id: str
    source_task_id: str
    source_result_hash: str
    plan_id: str
    idempotency_key: str
    output_root: str
    proposal_json_path: str
    proposal_markdown_path: str
    manifest_path: str
    gate_report_path: str
    draft_count: int
    proposal_count: int
    created_at: datetime
    prompt_versions: Dict[str, str] = Field(default_factory=dict)


class GateReport(StrictModel):
    schema_version: int = 1
    report_id: str
    bundle_id: str
    source_task_id: str
    source_result_hash: str
    idempotency_key: str
    gate_status: str
    checks: List[Dict[str, str]] = Field(default_factory=list)
    violations: List[Dict[str, str]] = Field(default_factory=list)
    prompt_versions: Dict[str, str] = Field(default_factory=dict)
    policy_version: str

    @model_validator(mode="after")
    def _gate_status(self) -> "GateReport":
        allowed = {"passed", "failed", "manual_review"}
        if self.gate_status not in allowed:
            raise ValueError(f"gate_status must be one of {sorted(allowed)}")
        return self


class AuditEvent(StrictModel):
    schema_version: int = 1
    event_id: str
    timestamp: datetime
    actor: AuditActor
    action: AuditAction
    target: str
    idempotency_key: str
    outcome: AuditOutcome
    source_task_id: Optional[str] = None
    prompt_versions: Dict[str, str] = Field(default_factory=dict)
    policy_version: str
    error_detail: Optional[str] = None
    inputs_hash: str
    outputs_hash: str


class ContinuationWriteEnvelope(StrictModel):
    """Common write envelope for future P0b filesystem writes."""

    schema_version: int = 1
    stage: str
    output_path: str
    idempotency_key: str
    collision_rule: str
    payload_schema_ref: str

    @model_validator(mode="after")
    def _requires_collision_rule(self) -> "ContinuationWriteEnvelope":
        if not self.collision_rule:
            raise ValueError("write envelopes must define a collision rule")
        return self

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .research_prerequisites import assess_research_prerequisites
from .schema import StrictModel


class MissionScenario(str, Enum):
    SUPERSEDED_TASKBOOK = "superseded_taskbook"
    WEAK_METRIC_CONTINUE_OR_STOP = "weak_metric_continue_or_stop"
    P2_ONLY_NOT_FINAL_BBOX = "p2_only_not_final_bbox"
    BOUNDED_DECODER_SMOKE = "bounded_decoder_smoke"
    MISSING_EVIDENCE_INVENTORY = "missing_evidence_inventory"
    SOURCE_BEHAVIOR_BOUNDARY = "source_behavior_boundary"
    UNKNOWN = "unknown"


class DecisionAction(str, Enum):
    CONTINUE = "continue"
    CONTINUE_AND_STAGE = "continue_and_stage"
    HOLD = "hold"
    STOP = "stop"
    REQUEST_INVENTORY = "request_inventory"
    REQUEST_CODEX_REVIEW = "request_codex_review"
    REQUEST_HUMAN_BOUNDARY = "request_human_boundary"


class DecisionStatus(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class AutonomyBudget(StrictModel):
    max_iterations: int = Field(gt=0)
    max_new_tasks: int = Field(ge=0)
    max_self_repairs: int = Field(ge=0)
    max_repair_attempts_per_issue: int = Field(ge=0)
    max_cpu_tasks: int = Field(ge=0)
    max_gpu_tasks: int = Field(ge=0)
    max_gpu_hours_total: float = Field(gt=0)
    max_wallclock_hours: float = Field(gt=0)


class AllowedExecution(StrictModel):
    cpu: List[str] = Field(default_factory=list)
    gpu: List[str] = Field(default_factory=list)


class AllowedCodeChange(StrictModel):
    can_create_new_files_under: List[str] = Field(default_factory=list)
    can_modify: List[str] = Field(default_factory=list)
    cannot_modify: List[str] = Field(default_factory=list)


class ClaimPolicy(StrictModel):
    bounded_probe_not_formal_validation: bool = True
    proxy_result_not_formal_evidence: bool = True
    weak_metric_not_execution_failure: bool = True
    claim_upgrade_requires_human: bool = True


class MissionObjective(StrictModel):
    short: str
    mainline: str
    rationale: str


class ResearchMission(StrictModel):
    schema_version: int = 1
    mission_id: str
    project_id: str
    workplace_id: str
    remote_root: str
    knowledge_root: str
    status: str = "active"
    objective: MissionObjective
    current_mainline: str
    allowed_research_questions: List[str] = Field(default_factory=list)
    forbidden_research_questions: List[str] = Field(default_factory=list)
    autonomy_budget: AutonomyBudget
    allowed_execution: AllowedExecution
    allowed_code_change: AllowedCodeChange
    forbidden_code_roots: List[str] = Field(default_factory=list)
    claim_policy: ClaimPolicy
    stop_conditions: List[str] = Field(default_factory=list)
    human_decision_boundaries: List[str] = Field(default_factory=list)
    codex_escalation_boundaries: List[str] = Field(default_factory=list)
    implementation_sequence: List[str] = Field(default_factory=list)


class MissionState(StrictModel):
    schema_version: int = 1
    mission_id: str
    status: str = "active"
    current_iteration: int = 0
    total_evidence_runs: int = 0
    active_decision_count: int = 0
    last_decision_id: Optional[str] = None
    parent_decision_id: Optional[str] = None
    claim_limits: List[str] = Field(default_factory=list)


class NightReplayEvidence(StrictModel):
    scenario_type: MissionScenario = MissionScenario.UNKNOWN
    superseded_taskbook: bool = False
    weak_metric_score: Optional[float] = None
    p2_only_bbox_final: bool = False
    bounded_decoder_smoke_available: bool = False
    missing_evidence: bool = False
    source_behavior_change_required: bool = False
    bounded_probe_safe: bool = True


class NightReplayFixture(StrictModel):
    schema_version: int = 1
    fixture_id: str
    mission_id: str
    scenario_id: str
    scenario_type: MissionScenario
    parent_decision_id: Optional[str] = None
    evidence: NightReplayEvidence
    expected: Dict[str, Any] = Field(default_factory=dict)


class DecisionResult(StrictModel):
    schema_version: int = 1
    decision_id: str
    mission_id: str
    scenario_id: str
    scenario_type: MissionScenario
    parent_decision_id: Optional[str]
    status: DecisionStatus
    chosen_action: DecisionAction
    can_continue: bool
    can_stage_to_night_auto: bool
    requires_human_review: bool
    requires_codex_review: bool
    reason_codes: List[str] = Field(default_factory=list)
    reason_text: str = ""
    claim_limits: List[str] = Field(default_factory=list)
    checkpoint_needed: bool = True


class ActivityNarrationEvent(StrictModel):
    schema_version: int = 1
    mission_id: str
    event_id: str
    phase: str
    headline: str
    current_action: str
    rationale: str
    branch_options: List[str] = Field(default_factory=list)
    blocked_by: List[str] = Field(default_factory=list)
    next_step: str
    preflight_status: str = ""
    preflight_execution_task: str = ""
    preflight_branch: str = ""
    preflight_action: str = ""
    judge_execution_success: bool = False
    judge_worth_continuing: bool = False
    judge_next_action: str = ""
    judge_stop_reason: str = ""
    remote_boundary: str = "not_reached"
    source_refs: List[str] = Field(default_factory=list)
    claim_limits: List[str] = Field(default_factory=list)
    side_effects: Dict[str, bool] = Field(
        default_factory=lambda: {
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_api_call": False,
            "dataset_or_checkpoint_mutation": False,
        }
    )


def _normalize(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


GOVERNANCE_CLAIM_LIMITS = [
    "no_scheduler_dispatch",
    "no_active_inbox_write",
    "no_registry_mutation",
    "no_active_kb_mutation",
    "no_remote_api_call",
    "no_dataset_or_checkpoint_mutation",
    "claim_upgrade_via_human_only",
]


MISSION_PATH = Path(".solution/continuation/research_mission.yaml")
MUSIC_SCHEMA_PATH = Path(".solution/continuation/research_mission_schema.yaml")
DECISION_SCHEMA_PATH = Path(".solution/continuation/next_action_decision_schema.yaml")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"required yaml not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a yaml object")
    return payload


def load_research_mission(path: Path | str | None = None) -> ResearchMission:
    mission_payload = _read_yaml(Path(path or MISSION_PATH))
    return ResearchMission.model_validate(mission_payload)


def load_replay_schema(path: Path | str | None = None) -> Dict[str, Any]:
    return _read_yaml(Path(path or DECISION_SCHEMA_PATH))


def load_mission_schema(path: Path | str | None = None) -> Dict[str, Any]:
    return _read_yaml(Path(path or MUSIC_SCHEMA_PATH))


def validate_mission_contract(
    mission: ResearchMission,
    *,
    schema: Dict[str, Any] | None = None,
) -> List[str]:
    schema_payload = schema or load_mission_schema()
    errors: List[str] = []

    mission_data = mission.model_dump()
    required_fields = schema_payload.get("required_fields", [])
    for field in required_fields:
        if field not in mission_data:
            errors.append(f"missing_required_field:{field}")

    if mission.status not in schema_payload.get("mission_status_values", []):
        errors.append(f"invalid_status:{mission.status}")

    for effect in schema_payload.get("required_side_effects", []):
        if effect not in GOVERNANCE_CLAIM_LIMITS:
            errors.append(f"required_side_effect_not_in_governance:{effect}")

    allowed_execution = schema_payload.get("allowed_execution_types", [])
    for action in mission.allowed_execution.cpu + mission.allowed_execution.gpu:
        if action not in allowed_execution:
            errors.append(f"invalid_execution_action:{action}")

    if set(mission.forbidden_code_roots) != set(mission.allowed_code_change.cannot_modify):
        errors.append("forbidden_code_roots_must_match_allowed_code_change_cannot_modify")

    objective_data = mission_data.get("objective", {})
    for objective_field in schema_payload.get("objective_required_keys", []):
        if not isinstance(objective_field, str):
            continue
        value = objective_field and objective_data.get(objective_field, "")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"objective_field_missing_or_empty:{objective_field}")

    return errors


def _decision_id(mission_id: str, scenario_id: str, action: DecisionAction) -> str:
    return f"{mission_id}:{scenario_id}:{action.value}"


def _append_claim_limits(*extra: str) -> List[str]:
    values = list(GOVERNANCE_CLAIM_LIMITS)
    for item in extra:
        if item and item not in values:
            raise ValueError(f"unknown_claim_limit:{item}")
    return values


def _load_decision_reason_codes() -> List[str]:
    return load_replay_schema().get("required_reason_codes", [])


def _validate_reason_codes(reason_codes: List[str]) -> List[str]:
    allowed = set(_load_decision_reason_codes())
    invalid = [code for code in reason_codes if code not in allowed]
    if invalid:
        raise ValueError(f"unknown_reason_code:{','.join(sorted(set(invalid)))}")
    return reason_codes


def build_activity_narration_event(
    *,
    mission: ResearchMission,
    decision: DecisionResult | None,
    phase: str,
    headline: str,
    current_action: str,
    rationale: str,
    next_step: str,
    branch_options: List[str] | None = None,
    blocked_by: List[str] | None = None,
    source_refs: List[str] | None = None,
    preflight_status: str = "",
    preflight_execution_task: str = "",
    preflight_branch: str = "",
    preflight_action: str = "",
    judge_execution_success: bool = False,
    judge_worth_continuing: bool = False,
    judge_next_action: str = "",
    judge_stop_reason: str = "",
    remote_boundary: str = "not_reached",
) -> ActivityNarrationEvent:
    """Create a public progress note without exposing private reasoning traces."""

    event_id_suffix = decision.decision_id if decision else phase
    safe_event_id = (
        f"{mission.mission_id}:{event_id_suffix}:narration"
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
    )
    claim_limits = decision.claim_limits if decision else _append_claim_limits()
    return ActivityNarrationEvent(
        mission_id=mission.mission_id,
        event_id=safe_event_id,
        phase=phase,
        headline=headline,
        current_action=current_action,
        rationale=rationale,
        branch_options=list(branch_options or []),
        blocked_by=list(blocked_by or []),
        next_step=next_step,
        source_refs=list(source_refs or []),
        claim_limits=claim_limits,
        preflight_status=_normalize(preflight_status),
        preflight_execution_task=_normalize(preflight_execution_task),
        preflight_branch=_normalize(preflight_branch),
        preflight_action=_normalize(preflight_action),
        judge_execution_success=bool(judge_execution_success),
        judge_worth_continuing=bool(judge_worth_continuing),
        judge_next_action=_normalize(judge_next_action),
        judge_stop_reason=_normalize(judge_stop_reason),
        remote_boundary=_normalize(remote_boundary) or "not_reached",
    )


def render_activity_narration(event: ActivityNarrationEvent) -> str:
    blocked = "; ".join(event.blocked_by) if event.blocked_by else "none"
    branches = "; ".join(event.branch_options) if event.branch_options else "none"
    return (
        f"[{event.phase}] {event.headline}\n"
        f"- current_action: {event.current_action}\n"
        f"- rationale: {event.rationale}\n"
        f"- branch_options: {branches}\n"
        f"- blocked_by: {blocked}\n"
        f"- next_step: {event.next_step}"
    )


def _emit_decision(
    mission_id: str,
    scenario_id: str,
    scenario_type: MissionScenario,
    parent_decision_id: str | None,
    *,
    status: DecisionStatus,
    chosen_action: DecisionAction,
    can_continue: bool,
    can_stage_to_night_auto: bool,
    requires_human_review: bool,
    requires_codex_review: bool,
    reason_codes: List[str],
    reason_text: str,
    claim_limits: List[str],
    checkpoint_needed: bool = True,
) -> DecisionResult:
    _validate_reason_codes(reason_codes)
    return DecisionResult(
        decision_id=_decision_id(mission_id, scenario_id, chosen_action),
        mission_id=mission_id,
        scenario_id=scenario_id,
        scenario_type=scenario_type,
        parent_decision_id=parent_decision_id,
        status=status,
        chosen_action=chosen_action,
        can_continue=can_continue,
        can_stage_to_night_auto=can_stage_to_night_auto,
        requires_human_review=requires_human_review,
        requires_codex_review=requires_codex_review,
        reason_codes=reason_codes,
        reason_text=reason_text,
        claim_limits=claim_limits,
        checkpoint_needed=checkpoint_needed,
    )


def decide_next_action(
    mission: ResearchMission,
    state: MissionState,
    evidence: NightReplayEvidence,
    scenario_id: str,
    project_knowledge: Dict[str, Any] | None = None,
    discussion_payload: Dict[str, Any] | None = None,
    *,
    parent_decision_id: str | None = None,
) -> DecisionResult:
    resolved_parent_decision_id = parent_decision_id or f"{mission.mission_id}:root"
    reason_codes: List[str] = []
    if mission.status != "active":
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_HUMAN_BOUNDARY,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=["mission_inactive"],
            reason_text="mission is not active; human boundary required before continuation",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if evidence.scenario_type == MissionScenario.UNKNOWN:
        reason_codes.append("unknown_scenario_type")
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_HUMAN_BOUNDARY,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=reason_codes,
            reason_text="scenario_type must be known before continuation",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if state.current_iteration >= mission.autonomy_budget.max_iterations:
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_HUMAN_BOUNDARY,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=["budget_exhausted"],
            reason_text="mission autonomy iteration budget is exhausted",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if project_knowledge is not None:
        prerequisite_gate = assess_research_prerequisites(project_knowledge, discussion_payload)
        if not prerequisite_gate.can_continue_to_mission_gate:
            return _emit_decision(
                mission_id=mission.mission_id,
                scenario_id=scenario_id,
                scenario_type=evidence.scenario_type,
                parent_decision_id=resolved_parent_decision_id,
                status=DecisionStatus(prerequisite_gate.status),
                chosen_action=DecisionAction(prerequisite_gate.chosen_action),
                can_continue=False,
                can_stage_to_night_auto=False,
                requires_human_review=DecisionAction(prerequisite_gate.chosen_action) == DecisionAction.REQUEST_HUMAN_BOUNDARY,
                requires_codex_review=False,
                reason_codes=prerequisite_gate.reason_codes,
                reason_text=prerequisite_gate.reason_text,
                claim_limits=_append_claim_limits(),
                checkpoint_needed=False,
            )

    if evidence.superseded_taskbook:
        reason_codes.append("superseded_taskbook")

    if evidence.source_behavior_change_required:
        reason_codes.append("source_behavior_change_needed")
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_CODEX_REVIEW,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=False,
            requires_codex_review=True,
            reason_codes=reason_codes,
            reason_text="source behavior edit requires codex/owner review",
            claim_limits=_append_claim_limits(),
        )

    if evidence.missing_evidence:
        reason_codes.append("missing_evidence")
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_INVENTORY,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=reason_codes,
            reason_text="missing evidence blocks bounded execution",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if (
        evidence.scenario_type == MissionScenario.P2_ONLY_NOT_FINAL_BBOX
        and not evidence.p2_only_bbox_final
    ):
        reason_codes.append("p2_only_not_final_path")
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.STOP,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=reason_codes,
            reason_text="P2-only bbox is not treated as final path in mission contract",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if reason_codes:
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.BLOCK,
            chosen_action=DecisionAction.REQUEST_HUMAN_BOUNDARY,
            can_continue=False,
            can_stage_to_night_auto=False,
            requires_human_review=True,
            requires_codex_review=False,
            reason_codes=reason_codes,
            reason_text="policy boundary encountered before continuation",
            claim_limits=_append_claim_limits(),
            checkpoint_needed=False,
        )

    if evidence.weak_metric_score is not None:
        if evidence.weak_metric_score < 0.3 and mission.claim_policy.weak_metric_not_execution_failure:
            reason_codes.append("weak_metric_not_formal_enough")
            return _emit_decision(
                mission_id=mission.mission_id,
                scenario_id=scenario_id,
                scenario_type=evidence.scenario_type,
                parent_decision_id=resolved_parent_decision_id,
                status=DecisionStatus.BLOCK,
                chosen_action=DecisionAction.STOP,
                can_continue=False,
                can_stage_to_night_auto=False,
                requires_human_review=False,
                requires_codex_review=False,
                reason_codes=reason_codes,
                reason_text="weak metric is bounded proxy; does not justify formal claim",
                claim_limits=_append_claim_limits(),
            )

    if evidence.bounded_decoder_smoke_available and evidence.bounded_probe_safe:
        reason_codes.append("bounded_decoder_smoke")
        return _emit_decision(
            mission_id=mission.mission_id,
            scenario_id=scenario_id,
            scenario_type=evidence.scenario_type,
            parent_decision_id=resolved_parent_decision_id,
            status=DecisionStatus.ALLOW,
            chosen_action=DecisionAction.CONTINUE_AND_STAGE,
            can_continue=True,
            can_stage_to_night_auto=True,
            requires_human_review=False,
            requires_codex_review=False,
            reason_codes=reason_codes,
            reason_text="bounded smoke path is allowed and keeps claim limits intact",
            claim_limits=_append_claim_limits(),
        )

    reason_codes.append("bounded_run_not_configured")
    return _emit_decision(
        mission_id=mission.mission_id,
        scenario_id=scenario_id,
        scenario_type=evidence.scenario_type,
        parent_decision_id=resolved_parent_decision_id,
        status=DecisionStatus.BLOCK,
        chosen_action=DecisionAction.HOLD,
        can_continue=False,
        can_stage_to_night_auto=False,
        requires_human_review=True,
        requires_codex_review=False,
        reason_codes=reason_codes,
        reason_text="bounded execution options are not configured",
        claim_limits=_append_claim_limits(),
    )


def load_replay_fixture(path: Path | str) -> NightReplayFixture:
    payload = _read_yaml(Path(path))
    return NightReplayFixture.model_validate(payload)


def mission_gate_result(
    mission: ResearchMission,
    state: MissionState,
    fixtures: List[NightReplayFixture],
) -> List[DecisionResult]:
    decisions: List[DecisionResult] = []
    for fixture in fixtures:
        parent_decision = fixture.parent_decision_id or state.parent_decision_id
        if parent_decision is None:
            parent_decision = f"{mission.mission_id}:root"
        decision = decide_next_action(
            mission=mission,
            state=state,
            evidence=fixture.evidence,
            scenario_id=fixture.scenario_id,
            parent_decision_id=parent_decision,
        )
        decisions.append(decision)
        state.current_iteration += 1
        state.total_evidence_runs += 1
        state.active_decision_count += 1
        state.last_decision_id = decision.decision_id
        state.parent_decision_id = decision.decision_id
        if decision.status == DecisionStatus.BLOCK:
            continue
    return decisions


def mission_state_skeleton(
    mission_id: str,
    *,
    status: str = "active",
    current_iteration: int = 0,
    total_evidence_runs: Optional[int] = None,
    active_decision_count: Optional[int] = None,
    parent_decision_id: Optional[str] = None,
) -> Dict[str, Any]:
    evidence_runs = (
        total_evidence_runs
        if total_evidence_runs is not None
        else current_iteration
    )
    decision_count = (
        active_decision_count
        if active_decision_count is not None
        else current_iteration
    )
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "status": status,
        "current_iteration": current_iteration,
        "total_evidence_runs": evidence_runs,
        "active_decision_count": decision_count,
        "last_decision_id": None,
        "parent_decision_id": parent_decision_id,
        "claim_limits": GOVERNANCE_CLAIM_LIMITS,
    }


def mission_checkpoint_skeleton(mission_id: str, decision_id: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "checkpoint_id": f"{mission_id}__{decision_id}",
        "status": "ready",
        "parent_decision_id": decision_id,
        "traceable_to_parent": True,
        "traceability_notes": "decision_id must be copied into every derived run artifact",
        "claim_limits": GOVERNANCE_CLAIM_LIMITS,
    }

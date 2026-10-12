from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from .night_researcher import ActivityNarrationEvent, build_activity_narration_event
from .mission_remote_loop import read_local_mission_remote_history
from .memory_observer import read_continuation_observations, summarize_continuation_observations
from .paths import (
    continuation_night_auto_local_execution_dir,
    continuation_night_auto_local_execution_report_path,
    continuation_night_auto_local_execution_root,
    solution_continuation_dir,
)
from .night_auto_morning_brief import write_morning_brief
from .schema import StrictModel


class LocalExecutionTask(str, Enum):
    CPU_INVENTORY = "cpu_inventory"
    BOUNDED_SMOKE = "bounded_smoke"
    DIAGNOSTIC = "diagnostic"


class LocalExecutionBranch(str, Enum):
    CACHE_REEXTRACTION = "cache_reextraction"
    GRAPH_TRAINING = "graph_training"


class LocalExecutionAction(str, Enum):
    INSPECT = "inspect"
    RUN = "run"
    REPAIR = "repair"
    HOLD = "hold"
    STOP = "stop"


class LocalExecutionDecision(BaseModel):
    execution_success: bool = False
    information_gain: float = 0.0
    worth_continuing: bool = False
    claim_limit: bool = True
    next_action: LocalExecutionAction = LocalExecutionAction.HOLD
    stop_reason: str = ""


class LocalExecutionIntent(StrictModel):
    schema_version: int = 1
    mission_id: str
    proposal_run_id: str
    candidate_id: str
    scenario_id: str
    parent_decision_id: str = ""
    execution_task: LocalExecutionTask = LocalExecutionTask.BOUNDED_SMOKE
    checkpoint_path: str = ""
    cache_path: str = ""
    script_path: str = ""
    script_cache_mapping_confirmed: bool = False
    actor: str = "system"
    available_cpu_cores: int | None = None
    branch_override: str = ""
    source_decision_id: str = ""


class LocalExecutionPreflight(StrictModel):
    schema_version: int = 1
    status: str = "hold"
    mission_id: str
    proposal_run_id: str
    candidate_id: str
    scenario_id: str
    source_decision_id: str = ""
    execution_task: str
    branch: str
    action: LocalExecutionAction = LocalExecutionAction.INSPECT
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    checks_hash: str = ""
    reasons: List[str] = Field(default_factory=list)
    side_effects: Dict[str, bool] = Field(default_factory=dict)
    claim_limits: List[str] = Field(default_factory=list)
    lineage: Dict[str, Any] = Field(default_factory=dict)
    generated_at: str = "1970-01-01T00:00:00Z"
    generated_by: str = "system"
    report_path: str = ""
    action_blockers: List[str] = Field(default_factory=list)


class RemoteBoundaryStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


class RemoteBoundaryReadiness(StrictModel):
    schema_version: int = 1
    status: RemoteBoundaryStatus = RemoteBoundaryStatus.BLOCKED
    mission_id: str
    proposal_run_id: str
    candidate_id: str
    case_id: str
    preflight_status: str
    judge_next_action: str = ""
    judge_stop_reason: str = ""
    can_request_remote_launch: bool = False
    remote_boundary: str = "not_reached"
    next_required_approval: str = "none"
    blockers: List[str] = Field(default_factory=list)
    claim_limits: List[str] = Field(default_factory=list)
    side_effects: Dict[str, bool] = Field(default_factory=dict)
    generated_at: str = "1970-01-01T00:00:00Z"


PR3_SIDE_EFFECTS = {
    "scheduler_dispatch": False,
    "active_inbox_write": False,
    "registry_mutation": False,
    "remote_api_call": False,
    "dataset_or_checkpoint_mutation": False,
}

B14_REHEARSAL_SCHEMA_VERSION = 1
B14_REHEARSAL_DIR = "pack_rehearsal"
B14_REHEARSAL_SUMMARY_NAME = "b14_local_execution_rehearsal_summary.json"
B14_REHEARSAL_REMOTE_BOUNDARY_REPORT_NAME = "remote_boundary_readiness_report.json"
REMOTE_BOUNDARY_PRE_FLIGHT_SCHEMA_NAME = "night_auto_remote_boundary_preflight_schema.yaml"
B14_PACK_REHEARSAL_CANDIDATE_ID = "b14_pack_rehearsal"

PR3_CLAIM_LIMITS = [
    "no_scheduler_dispatch",
    "no_active_inbox_write",
    "no_active_kb_mutation",
    "no_registry_mutation",
    "no_remote_api_call",
    "no_dataset_or_checkpoint_mutation",
    "claim_upgrade_via_human_only",
]

LOCAL_EXECUTION_PHASES = {
    LocalExecutionAction.INSPECT.value,
    LocalExecutionAction.RUN.value,
    LocalExecutionAction.REPAIR.value,
    LocalExecutionAction.HOLD.value,
    LocalExecutionAction.STOP.value,
}

LOCAL_EXECUTION_CYCLE_ID = "2026-05-24T_SPARK_P3_LOCAL_EXECUTION_01"


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return default
    normalized = _normalize_lower(value)
    if normalized in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return default


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _read_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _safe_path_value(value: str) -> str:
    return _normalize(value).replace("..", "_").replace("/", "_").replace("\\", "_") or "unknown"


def load_local_execution_policy() -> Dict[str, Any]:
    path = solution_continuation_dir() / "night_auto_local_execution_policy.yaml"
    if path.exists():
        payload = _read_yaml(path)
        if isinstance(payload, dict):
            return payload
    return {}


def load_local_execution_preflight_schema() -> Dict[str, Any]:
    path = solution_continuation_dir() / "night_auto_local_execution_preflight_schema.yaml"
    if path.exists():
        payload = _read_yaml(path)
        if isinstance(payload, dict):
            return payload
    return {}


def load_remote_boundary_preflight_schema() -> Dict[str, Any]:
    path = solution_continuation_dir() / REMOTE_BOUNDARY_PRE_FLIGHT_SCHEMA_NAME
    if path.exists():
        payload = _read_yaml(path)
        if isinstance(payload, dict):
            return payload
    return {}


def _coerce_task(task: str) -> LocalExecutionTask | None:
    normalized = _normalize_lower(task)
    if normalized in {item.value for item in LocalExecutionTask}:
        return LocalExecutionTask(normalized)
    return None


def _build_claim_limits(raw: Any) -> List[str]:
    if isinstance(raw, (list, tuple, set)):
        values = {_normalize(item) for item in raw}
        values -= {""}
        return sorted(set(PR3_CLAIM_LIMITS) | values) if values else list(PR3_CLAIM_LIMITS)
    if _normalize(raw):
        return sorted(set(PR3_CLAIM_LIMITS) | {_normalize(raw)})
    return list(PR3_CLAIM_LIMITS)


def _lineage_checks(intent: LocalExecutionIntent, branch: str, checks: List[Dict[str, Any]], reasons: List[str]) -> Dict[str, Any]:
    lineage = {
        "checkpoint_path": _normalize(intent.checkpoint_path),
        "cache_path": _normalize(intent.cache_path),
        "script_path": _normalize(intent.script_path),
        "mapping_confirmed": _coerce_bool(intent.script_cache_mapping_confirmed),
        "branch": branch,
    }

    if not lineage["checkpoint_path"]:
        checks.append({"id": "checkpoint_lineage_present", "status": "failed", "reason": "missing_checkpoint_path"})
        reasons.append("missing_checkpoint_path")
    else:
        checks.append({"id": "checkpoint_lineage_present", "status": "passed", "path": lineage["checkpoint_path"]})

    if not lineage["cache_path"]:
        checks.append({"id": "cache_lineage_present", "status": "failed", "reason": "missing_cache_path"})
        reasons.append("missing_cache_path")
    else:
        checks.append({"id": "cache_lineage_present", "status": "passed", "path": lineage["cache_path"]})

    if branch == LocalExecutionBranch.GRAPH_TRAINING.value:
        if not lineage["script_path"]:
            checks.append({"id": "script_lineage_present", "status": "failed", "reason": "missing_script_path"})
            reasons.append("missing_script_path")
        else:
            checks.append({"id": "script_lineage_present", "status": "passed", "path": lineage["script_path"]})

        if not lineage["mapping_confirmed"]:
            checks.append(
                {
                    "id": "cache_script_mapping",
                    "status": "failed",
                    "reason": "cache_script_mapping_not_confirmed",
                }
            )
            reasons.append("cache_script_mapping_not_confirmed")
        else:
            checks.append({"id": "cache_script_mapping", "status": "passed", "reason": "confirmed"})
    return lineage


def _determine_branch(intent: LocalExecutionIntent, reasons: List[str]) -> str:
    override = _normalize_lower(intent.branch_override)
    if override:
        if override == "graph_training":
            return LocalExecutionBranch.GRAPH_TRAINING.value
        if override == "cache_reextraction":
            return LocalExecutionBranch.CACHE_REEXTRACTION.value
        reasons.append("invalid_branch_override")

    if _coerce_bool(intent.script_cache_mapping_confirmed) and _normalize(intent.script_path) and _normalize(intent.cache_path) and _normalize(intent.checkpoint_path):
        return LocalExecutionBranch.GRAPH_TRAINING.value
    return LocalExecutionBranch.CACHE_REEXTRACTION.value


def build_local_execution_intent(
    *,
    mission_id: str,
    proposal_run_id: str,
    candidate_id: str,
    scenario_id: str,
    execution_task: str,
    checkpoint_path: str,
    cache_path: str,
    script_path: str,
    script_cache_mapping_confirmed: bool,
    parent_decision_id: str = "",
    actor: str = "system",
    available_cpu_cores: int | None = None,
    branch_override: str = "",
    source_decision_id: str = "",
) -> LocalExecutionIntent:
    normalized_task = _coerce_task(execution_task) or LocalExecutionTask.BOUNDED_SMOKE
    return LocalExecutionIntent(
        mission_id=_normalize(mission_id),
        proposal_run_id=_normalize(proposal_run_id),
        candidate_id=_safe_path_value(candidate_id),
        scenario_id=_normalize(scenario_id),
        parent_decision_id=_normalize(parent_decision_id),
        execution_task=normalized_task,
        checkpoint_path=_normalize(checkpoint_path),
        cache_path=_normalize(cache_path),
        script_path=_normalize(script_path),
        script_cache_mapping_confirmed=_coerce_bool(script_cache_mapping_confirmed),
        actor=_normalize(actor) or "system",
        available_cpu_cores=available_cpu_cores,
        branch_override=_normalize(branch_override),
        source_decision_id=_normalize(source_decision_id),
    )


def _safe_candidate_id(value: str) -> str:
    return _safe_path_value(value)


def assess_local_execution_preflight(
    intent: LocalExecutionIntent,
    *,
    write_report: bool = False,
    report_id: str | None = None,
    actor: str | None = None,
) -> Dict[str, Any]:
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []
    actor_name = _normalize(actor or intent.actor)
    mission_id = _normalize(intent.mission_id)
    proposal_run_id = _normalize(intent.proposal_run_id)
    candidate_id = _safe_candidate_id(intent.candidate_id)
    scenario_id = _normalize(intent.scenario_id)
    if not mission_id:
        reasons.append("missing_mission_id")
    if not proposal_run_id:
        reasons.append("missing_proposal_run_id")
    if not candidate_id:
        reasons.append("missing_candidate_id")
    if not actor_name:
        reasons.append("missing_actor")

    task = _coerce_task(intent.execution_task.value if isinstance(intent.execution_task, LocalExecutionTask) else intent.execution_task)
    if task is None:
        reasons.append("unknown_execution_task")
        task = LocalExecutionTask.BOUNDED_SMOKE
    checks.append(
        {
            "id": "execution_task",
            "status": "passed" if _normalize(intent.execution_task.value) else "failed",
            "task": task.value,
        }
    )

    branch = _determine_branch(intent, reasons)
    if task == LocalExecutionTask.CPU_INVENTORY:
        if intent.available_cpu_cores is None:
            checks.append({"id": "cpu_inventory", "status": "failed", "reason": "missing_cpu_inventory"})
            reasons.append("missing_cpu_inventory")
        elif intent.available_cpu_cores <= 0:
            checks.append({"id": "cpu_inventory", "status": "failed", "reason": "invalid_cpu_inventory"})
            reasons.append("invalid_cpu_inventory")
        else:
            checks.append(
                {
                    "id": "cpu_inventory",
                    "status": "passed",
                    "available_cpu_cores": intent.available_cpu_cores,
                }
            )

    lineage = _lineage_checks(intent, branch, checks, reasons)
    if branch == LocalExecutionBranch.GRAPH_TRAINING.value and _normalize(intent.branch_override) not in {
        "",
        "graph_training",
    }:
        reasons.append("invalid_graph_training_override")

    status = "blocked" if reasons else ("hold" if "cache_script_mapping_not_confirmed" in reasons else "pass")
    action = LocalExecutionAction.HOLD
    if status == "blocked":
        action = LocalExecutionAction.HOLD
    elif task == LocalExecutionTask.CPU_INVENTORY:
        action = LocalExecutionAction.RUN
    elif status == "pass":
        action = LocalExecutionAction.INSPECT if branch == LocalExecutionBranch.CACHE_REEXTRACTION.value else LocalExecutionAction.RUN

    payload = LocalExecutionPreflight(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        candidate_id=candidate_id,
        scenario_id=scenario_id,
        source_decision_id=_normalize(intent.source_decision_id),
        status=status,
        execution_task=task.value,
        branch=branch,
        checks=checks,
        checks_hash=_sha256_hex(checks),
        reasons=sorted(set(reasons)),
        action=action,
        claim_limits=_build_claim_limits(intent.model_dump().get("claim_limits")),
        side_effects=dict(PR3_SIDE_EFFECTS),
        lineage=lineage,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        generated_by=actor_name or "system",
    )
    if write_report:
        report_path = continuation_night_auto_local_execution_report_path(
            mission_id,
            proposal_run_id,
            candidate_id,
            task.value,
            report_name=report_id or "local_execution_preflight",
        )
        report_payload = payload.model_dump()
        report_payload["cycle_id"] = LOCAL_EXECUTION_CYCLE_ID
        report_payload["side_effects"] = dict(PR3_SIDE_EFFECTS)
        report_payload["checks_hash"] = _sha256_hex(checks)
        report_payload = _canonical_json(report_payload) + "\n"
        if report_path.exists():
            existing = report_path.read_text(encoding="utf-8")
            if existing == report_payload:
                payload.report_path = str(report_path)
                return {
                    **payload.model_dump(),
                    "action": "replayed",
                    "created": False,
                    "replayed": True,
                    "report_path": str(report_path),
                }
            payload.report_path = str(report_path)
            return {
                **payload.model_dump(),
                "action": "blocked",
                "created": False,
                "replayed": False,
                "report_path": str(report_path),
                "reasons": sorted(set(reasons + ["idempotency_collision"])),
            }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_payload, encoding="utf-8")
        payload.report_path = str(report_path)
        return {
            **payload.model_dump(),
            "action": "created",
            "created": True,
            "replayed": False,
            "report_path": str(report_path),
        }

    return payload.model_dump()


def assess_remote_boundary_readiness(
    *,
    mission_id: str,
    proposal_run_id: str,
    candidate_id: str,
    case_id: str,
    preflight: Dict[str, Any],
    decision: Dict[str, Any],
    actor: str | None = None,
) -> Dict[str, Any]:
    blockers: List[str] = []
    preflight_payload = dict(preflight or {})
    decision_payload = dict(decision or {})
    safe_mission = _safe_candidate_id(mission_id)
    safe_proposal = _safe_candidate_id(proposal_run_id)
    safe_candidate = _safe_candidate_id(candidate_id)
    safe_case = _safe_candidate_id(case_id)
    actor_name = _normalize(actor or "")

    preflight_status = _normalize(preflight_payload.get("status"))
    judge_next_action = _normalize(decision_payload.get("next_action"))
    judge_stop_reason = _normalize(decision_payload.get("stop_reason"))
    worth_continuing = _coerce_bool(decision_payload.get("worth_continuing"), default=False)

    if not safe_mission:
        blockers.append("missing_mission_id")
    if not safe_proposal:
        blockers.append("missing_proposal_run_id")
    if not safe_candidate:
        blockers.append("missing_candidate_id")
    if not safe_case:
        blockers.append("missing_case_id")

    if preflight_status != "pass":
        blockers.append("preflight_not_pass")

    blockers.extend(_to_string_list(preflight_payload.get("reasons")))
    blockers.extend(_to_string_list(decision_payload.get("reasons")))

    if not worth_continuing:
        blockers.append("judge_not_worth_continuing")
    if judge_next_action == LocalExecutionAction.HOLD.value:
        blockers.append("judge_next_action_hold")
    elif judge_next_action == LocalExecutionAction.STOP.value:
        blockers.append("judge_next_action_stop")
    if not judge_next_action:
        blockers.append("judge_next_action_missing")

    if judge_stop_reason and judge_stop_reason not in blockers:
        blockers.append(judge_stop_reason)

    blockers = list(dict.fromkeys(_to_string_list(blockers)))
    status = RemoteBoundaryStatus.READY if not blockers else RemoteBoundaryStatus.BLOCKED
    actionable_blockers = [item for item in blockers if item != "preflight_not_pass"]
    next_required_approval = (
        "none"
        if status == RemoteBoundaryStatus.READY
        else (
            actionable_blockers[0]
            if actionable_blockers
            else (blockers[0] if blockers else "resolve_remote_boundary_blockers")
        )
    )

    return {
        **RemoteBoundaryReadiness(
            mission_id=safe_mission,
            proposal_run_id=safe_proposal,
            candidate_id=safe_candidate,
            case_id=safe_case,
            preflight_status=preflight_status,
            status=status,
            judge_next_action=judge_next_action,
            judge_stop_reason=judge_stop_reason,
            can_request_remote_launch=status == RemoteBoundaryStatus.READY,
            next_required_approval=next_required_approval,
            blockers=blockers,
            remote_boundary="not_reached",
            claim_limits=_build_claim_limits(preflight_payload.get("claim_limits")),
            side_effects=dict(PR3_SIDE_EFFECTS),
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ).model_dump(),
        "generated_by": actor_name or "system",
    }


def _coerce_result_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    execution_success = _coerce_bool(raw.get("execution_success"), default=False)
    if "exit_code" in raw and not execution_success:
        execution_success = _coerce_bool(raw.get("exit_code") == 0, default=execution_success)
    information_gain = raw.get("information_gain")
    try:
        information_gain_value = float(information_gain)
    except (TypeError, ValueError):
        information_gain_value = 0.0
    worth_continuing_threshold = float(_normalize(raw.get("worth_continuing_threshold")) or 0.05)
    claim_limit = _coerce_bool(raw.get("claim_limit"), default=False)
    stop_reason = _normalize(raw.get("stop_reason"))
    return {
        "execution_success": bool(execution_success),
        "information_gain": information_gain_value,
        "worth_continuing_threshold": worth_continuing_threshold,
        "claim_limit": claim_limit,
        "stop_reason": stop_reason,
        "repair_available": _coerce_bool(raw.get("repair_available")),
    }


def judge_local_execution_after_result(
    preflight: Dict[str, Any],
    result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    normalized = dict(preflight or {})
    reasons = [_normalize(item) for item in normalized.get("reasons", []) if _normalize(item)]
    status = _normalize(normalized.get("status"))
    branch = _normalize(normalized.get("branch"))
    task = _normalize(normalized.get("execution_task"))

    if status != "pass":
        info = _coerce_result_info(result or {})
        return LocalExecutionDecision(
            execution_success=False,
            information_gain=info["information_gain"],
            worth_continuing=False,
            claim_limit=True,
            next_action=LocalExecutionAction.HOLD,
            stop_reason=reasons[0] if reasons else "preflight_blocked",
        ).model_dump()

    payload = _coerce_result_info(result or {})
    execution_success = payload["execution_success"]
    information_gain = payload["information_gain"]
    claim_limit = bool(payload["claim_limit"])
    worth_continuing = bool(execution_success and information_gain >= payload["worth_continuing_threshold"] and not claim_limit)

    next_action = LocalExecutionAction.HOLD
    stop_reason = ""
    if execution_success:
        if task == LocalExecutionTask.CPU_INVENTORY.value:
            next_action = LocalExecutionAction.INSPECT
        elif task == LocalExecutionTask.DIAGNOSTIC.value:
            next_action = LocalExecutionAction.RUN if branch == LocalExecutionBranch.GRAPH_TRAINING.value else LocalExecutionAction.INSPECT
        elif branch == LocalExecutionBranch.CACHE_REEXTRACTION.value:
            next_action = LocalExecutionAction.RUN
        else:
            next_action = LocalExecutionAction.INSPECT
        if not worth_continuing:
            next_action = LocalExecutionAction.HOLD
            stop_reason = "information_gain_insufficient"
    else:
        if claim_limit:
            next_action = LocalExecutionAction.REPAIR
        else:
            next_action = LocalExecutionAction.REPAIR
        stop_reason = reasons[0] if reasons else "execution_failed"

    if reasons and reasons[0] == "cache_script_mapping_not_confirmed":
        next_action = LocalExecutionAction.HOLD
        stop_reason = "cache_script_mapping_not_confirmed"
        worth_continuing = False

    decision = LocalExecutionDecision(
        execution_success=execution_success,
        information_gain=information_gain,
        worth_continuing=worth_continuing,
        claim_limit=claim_limit,
        next_action=next_action,
        stop_reason=stop_reason,
    )
    decision_payload = decision.model_dump()
    decision_payload["schema_version"] = 1
    decision_payload["preflight_status"] = status
    decision_payload["branch"] = branch
    decision_payload["execution_task"] = task
    decision_payload["reasons"] = reasons
    return decision_payload


def record_local_execution_cycle(
    *,
    mission,
    preflight: Dict[str, Any],
    result: Dict[str, Any] | None,
    phase: str | None = None,
    source_refs: List[str] | None = None,
    extra_blockers: List[str] | None = None,
) -> Dict[str, Any]:
    decision = judge_local_execution_after_result(preflight=preflight, result=result)
    preflight_payload = dict(preflight or {})

    mission_id = _normalize(preflight_payload.get("mission_id"))
    proposal_run_id = _normalize(preflight_payload.get("proposal_run_id"))
    candidate_id = _safe_candidate_id(_normalize(preflight_payload.get("candidate_id")))

    resolved_phase = _normalize(
        phase or decision.get("next_action") or preflight_payload.get("action") or LocalExecutionAction.HOLD.value
    )
    if resolved_phase not in LOCAL_EXECUTION_PHASES:
        resolved_phase = LocalExecutionAction.INSPECT.value

    event = build_local_execution_narration_event(
        mission=mission,
        intent=preflight_payload,
        decision=decision,
        phase=resolved_phase,
        remote_boundary="not_reached",
        source_refs=source_refs,
        extra_blockers=extra_blockers,
    )

    if mission_id and proposal_run_id and candidate_id:
        bundle_result = write_local_execution_narration_bundle(
            mission_id=mission_id,
            proposal_run_id=proposal_run_id,
            candidate_id=candidate_id,
            events=[event],
        )
    else:
        bundle_result = {
            "created": False,
            "replayed": False,
            "bundle_path": "",
            "error": "missing_scope_for_narration_output",
        }

    return {
        "decision": decision,
        "narration_event": event.model_dump(),
        "narration_bundle": bundle_result,
        "resolved_phase": resolved_phase,
    }


def build_local_execution_narration_event(
    *,
    mission,
    intent: LocalExecutionIntent | Dict[str, Any],
    decision: Dict[str, Any],
    phase: str | None = None,
    source_refs: List[str] | None = None,
    extra_blockers: List[str] | None = None,
    remote_boundary: str | None = None,
) -> ActivityNarrationEvent:
    source_payload = dict(intent or {})
    next_action = _normalize(decision.get("next_action") or decision.get("action"))
    resolved_phase = _normalize(phase or "inspect")
    if resolved_phase not in {LocalExecutionAction.INSPECT.value, LocalExecutionAction.RUN.value, LocalExecutionAction.REPAIR.value, LocalExecutionAction.HOLD.value, LocalExecutionAction.STOP.value}:
        resolved_phase = LocalExecutionAction.INSPECT.value

    mission_id = _normalize(source_payload.get("mission_id") or source_payload.get("mission", {}).get("mission_id"))
    if mission and not mission_id:
        mission_id = _normalize(getattr(mission, "mission_id", ""))
    claim_limits = source_payload.get("claim_limits") or PR3_CLAIM_LIMITS
    branch = _normalize(source_payload.get("branch") or source_payload.get("execution_branch") or "")
    task = _normalize(source_payload.get("execution_task") or source_payload.get("task", "bounded_smoke"))
    reasons = [item for item in decision.get("reasons", []) if _normalize(item)]
    reasons.extend([item for item in source_payload.get("reasons", []) if _normalize(item)])
    stop_reason = _normalize(decision.get("stop_reason"))
    if stop_reason:
        reasons.append(stop_reason)
    blocked_by = sorted(set((extra_blockers or []) + reasons))

    headline = f"PR-3 local execution {task}"
    if branch:
        headline += f" ({branch})"
    current_action = f"{resolved_phase} step: {next_action or 'inspect'}"
    rationale = (
        "Bounded local execution skeleton only: no scheduler dispatch, no inbox write, "
        "no registry write, no remote API/call, and no checkpoint mutation."
    )
    branches = [
        "run cache re-extraction when lineage map is incomplete",
        "run graph training/calibration after lineage is confirmed",
        "run bounded smoke or diagnostics with CPU-only inventory checks",
    ]

    event_id_suffix = decision.get("phase") or _normalize(source_payload.get("scenario_id")) or "local_execution"
    event = build_activity_narration_event(
        mission=mission,
        decision=None,
        phase=resolved_phase,
        headline=headline,
        current_action=current_action,
        rationale=rationale,
        next_step=next_action or "inspect",
        branch_options=branches,
        blocked_by=blocked_by,
        source_refs=source_refs or [],
        preflight_status=_normalize(source_payload.get("status")),
        preflight_execution_task=task,
        preflight_branch=branch,
        preflight_action=_normalize(source_payload.get("action")),
        judge_execution_success=bool(decision.get("execution_success")),
        judge_worth_continuing=bool(decision.get("worth_continuing")),
        judge_next_action=next_action,
        judge_stop_reason=_normalize(decision.get("stop_reason")),
    )
    event.remote_boundary = _normalize(remote_boundary) or "not_reached"
    event.claim_limits = claim_limits if isinstance(claim_limits, list) else PR3_CLAIM_LIMITS
    event.side_effects = dict(PR3_SIDE_EFFECTS)
    event.event_id = f"{mission_id}:{event_id_suffix}:{resolved_phase}:narration"
    return event


def write_local_execution_narration_bundle(
    *,
    mission_id: str,
    proposal_run_id: str,
    candidate_id: str,
    events: List[ActivityNarrationEvent],
) -> Dict[str, Any]:
    output_dir = continuation_night_auto_local_execution_dir(mission_id, proposal_run_id, candidate_id, "activity_narration")
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / f"{_safe_path_value(candidate_id)}_events.jsonl"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = [event.model_dump_json() for event in events]
    payload = "\n".join(rendered) + "\n"
    if bundle_path.exists():
        existing = bundle_path.read_text(encoding="utf-8")
        if existing == payload:
            return {"created": False, "replayed": True, "bundle_path": str(bundle_path)}
    bundle_path.write_text(payload, encoding="utf-8")
    return {"created": True, "replayed": False, "bundle_path": str(bundle_path)}


def _build_b14_launch_work_order(
    mission_id: str,
    proposal_run_id: str,
    case_id: str,
    preflight: Dict[str, Any],
    decision: Dict[str, Any],
    bundle_path: str,
) -> Dict[str, Any]:
    safe_mission = _safe_candidate_id(_normalize(mission_id))
    safe_proposal = _safe_path_value(proposal_run_id)
    safe_case = _safe_path_value(case_id)
    work_order_id = f"{safe_mission}_{safe_proposal}_{safe_case}"
    return {
        "work_order_id": work_order_id,
        "source_work_order_hash": _normalize(preflight.get("checks_hash")),
        "dispatch_request_id": f"dispatch_{safe_case}",
        "task_id": safe_case,
        "workplace_profile": "night_researcher_pr3",
        "host": "local-only-no-remote",
        "remote_workdir": f"/tmp/{safe_mission}_{safe_proposal}_{safe_case}/workdir",
        "remote_root": f"/tmp/{safe_mission}_{safe_proposal}_{safe_case}",
        "worker": "materialized_execution",
        "line": "b14",
        "batch": safe_proposal,
        "dag_root": f"{safe_mission}/b14_local_execution",
        "prefix": safe_mission,
        "resource_hint": _normalize(preflight.get("execution_task")),
        "resource_lane": "cpu",
        "resource_fields": {
            "case_id": case_id,
            "preflight_status": _normalize(preflight.get("status")),
            "decision_status": _normalize(decision.get("status", "")),
            "judge_next_action": _normalize(decision.get("next_action")),
            "judge_stop_reason": _normalize(decision.get("stop_reason")),
            "lineage": dict(preflight.get("lineage") or {}),
            "bundle_path": bundle_path,
        },
        "execution_mode": "dry_run",
        "status": "ready_for_materialized_execution_handoff",
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
        "required_artifacts": [_normalize(bundle_path)],
        "capability_checks": [],
    }


def _simulate_execution_result_from_preflight(preflight: Dict[str, Any], *, winner_gain: float = 0.06) -> Dict[str, Any]:
    if _normalize(preflight.get("status")) != "pass":
        return {"execution_success": False, "information_gain": 0.0, "worth_continuing_threshold": 0.05}
    return {"execution_success": True, "information_gain": float(winner_gain), "worth_continuing_threshold": 0.05}


def b14_pack_rehearsal_root(
    mission_id: str,
    proposal_run_id: str,
) -> Path:
    return continuation_night_auto_local_execution_dir(
        mission_id,
        proposal_run_id,
        B14_PACK_REHEARSAL_CANDIDATE_ID,
        B14_REHEARSAL_DIR,
    )


def b14_pack_rehearsal_summary_path(
    mission_id: str,
    proposal_run_id: str,
) -> Path:
    return b14_pack_rehearsal_root(mission_id, proposal_run_id) / B14_REHEARSAL_SUMMARY_NAME


def read_b14_pack_rehearsal_summary(
    *,
    mission_id: str,
    proposal_run_id: str,
) -> Dict[str, Any]:
    path = b14_pack_rehearsal_summary_path(mission_id, proposal_run_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_string_list(raw_values: Any) -> List[str]:
    if raw_values is None:
        return []
    if isinstance(raw_values, (list, tuple, set)):
        normalized_values = [_normalize(value) for value in raw_values]
        return [value for value in normalized_values if value]
    normalized_value = _normalize(raw_values)
    return [normalized_value] if normalized_value else []


def _mission_remote_report_summary(*, mission_id: str) -> Dict[str, Any]:
    return read_local_mission_remote_history(
        continuation_night_auto_local_execution_root(mission_id).parent,
        limit=3,
    )


def _continuation_observation_report_summary(*, mission_id: str) -> Dict[str, Any]:
    observations = read_continuation_observations(run_id=mission_id)
    return summarize_continuation_observations(observations=observations)


def build_remote_boundary_readiness_report(
    *,
    mission_id: str,
    proposal_run_id: str,
    cases: List[Dict[str, Any]],
    actor: str | None = None,
) -> Dict[str, Any]:
    safe_mission = _safe_candidate_id(_normalize(mission_id))
    safe_proposal = _safe_candidate_id(_normalize(proposal_run_id))
    actor_name = _normalize(actor or "system")
    report_case_summaries: List[Dict[str, Any]] = []
    blocked_case_ids: List[str] = []
    ready_case_ids: List[str] = []
    aggregated_blockers: List[str] = []

    if not cases:
        return {
            "schema_version": 1,
            "artifact_type": "remote_boundary_readiness_report",
            "status": "blocked",
            "mission_id": safe_mission,
            "proposal_run_id": safe_proposal,
            "remote_boundary": "not_reached",
            "case_count": 0,
            "ready_case_count": 0,
            "blocked_case_count": 0,
            "ready_case_ids": [],
            "blocked_case_ids": [],
            "generated_by": actor_name,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "cases": [],
            "summary": {
                "status_values": ["blocked"],
                "remote_boundary_status": "not_reached",
                "all_case_blockers": [],
                "next_steps": [],
            },
            "mission_remote": _mission_remote_report_summary(mission_id=safe_mission),
            "continuation_observations": _continuation_observation_report_summary(
                mission_id=safe_mission
            ),
            "path": "",
            "created": False,
            "replayed": False,
        }

    for case in cases:
        case_id = _safe_candidate_id(_normalize(case.get("case_id"))) or "unknown_case"
        preflight = dict(case.get("preflight") or {})
        decision = dict(case.get("decision") or {})
        launch_intent = dict(case.get("launch_intent") or {})
        status = _normalize(launch_intent.get("status"))
        case_blockers = _to_string_list(case.get("blockers"))
        case_blockers.extend(_to_string_list(launch_intent.get("blockers")))
        case_blockers = list(dict.fromkeys([item for item in case_blockers if item]))
        aggregated_blockers.extend(case_blockers)

        if status == "ready":
            ready_case_ids.append(case_id)
            next_step = _normalize(decision.get("next_action")) or "inspect"
        else:
            blocked_case_ids.append(case_id)
            next_step = _normalize(
                decision.get("next_action") or launch_intent.get("judge_next_action") or preflight.get("action") or "hold"
            )

        report_case_summaries.append(
            {
                "case_id": case_id,
                "candidate_id": _normalize(case.get("narration_event", {}).get("candidate_id") or case.get("candidate_id")),
                "preflight_status": _normalize(preflight.get("status")),
                "judge_next_action": _normalize(decision.get("next_action")),
                "judge_stop_reason": _normalize(decision.get("stop_reason")),
                "status": status,
                "next_step": next_step,
                "blockers": case_blockers,
                "launch_intent_id": _normalize(launch_intent.get("launch_intent_id")),
                "can_request_remote_launch": _coerce_bool(launch_intent.get("can_request_remote_launch"), default=False),
            }
        )

    status_values = {_normalize(case.get("status")) for case in report_case_summaries}
    unique_next_steps = list(
        dict.fromkeys([_normalize(item) for item in [_normalize(case.get("next_step")) for case in report_case_summaries] if _normalize(item)])
    )
    aggregated_blockers = list(dict.fromkeys([item for item in aggregated_blockers if item]))
    mission_remote_summary = _mission_remote_report_summary(mission_id=safe_mission)
    continuation_observations_summary = _continuation_observation_report_summary(
        mission_id=safe_mission
    )

    return {
        "schema_version": 1,
        "artifact_type": "remote_boundary_readiness_report",
        "status": "ready" if not blocked_case_ids else "blocked",
        "mission_id": safe_mission,
        "proposal_run_id": safe_proposal,
        "remote_boundary": "not_reached",
        "case_count": len(cases),
        "ready_case_count": len(set(ready_case_ids)),
        "blocked_case_ids": sorted(set(blocked_case_ids)),
        "ready_case_ids": sorted(set(ready_case_ids)),
        "blocked_case_count": len(set(blocked_case_ids)),
        "generated_by": actor_name,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "cases": report_case_summaries,
        "summary": {
            "status_values": sorted(status_values),
            "remote_boundary_status": "not_reached",
            "all_case_blockers": aggregated_blockers,
            "next_steps": unique_next_steps,
        },
        "mission_remote": mission_remote_summary,
        "continuation_observations": continuation_observations_summary,
        "path": "",
        "created": False,
        "replayed": False,
    }


def write_remote_boundary_readiness_report(
    *,
    mission_id: str,
    proposal_run_id: str,
    cases: List[Dict[str, Any]],
    actor: str | None = None,
    write_report: bool = True,
) -> Dict[str, Any]:
    report = build_remote_boundary_readiness_report(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        cases=cases,
        actor=actor,
    )
    if not write_report:
        return report

    report_dir = continuation_night_auto_local_execution_dir(
        mission_id,
        proposal_run_id,
        _safe_candidate_id("b14_pack_rehearsal"),
        B14_REHEARSAL_DIR,
    )
    report_path = report_dir / B14_REHEARSAL_REMOTE_BOUNDARY_REPORT_NAME
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    created = True
    replayed = False
    if report_path.exists():
        existing = report_path.read_text(encoding="utf-8")
        if existing == serialized:
            created = False
            replayed = True
    if not replayed:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(serialized, encoding="utf-8")

    report["path"] = str(report_path)
    report["created"] = created
    report["replayed"] = replayed
    return report


def run_b14_local_execution_dry_run_pack(
    mission,
    *,
    cases: List[Dict[str, Any]],
    proposal_run_id: str,
    actor: str = "system",
    write_summary: bool = True,
) -> Dict[str, Any]:
    """
    Run a local-only B14 rehearsal pack through existing PR-3 flow:
    preflight -> judge -> narration -> morning brief -> launch-readiness check.
    """
    mission_id = _normalize(getattr(mission, "mission_id", ""))
    if not mission_id:
        raise ValueError("mission must include mission_id")

    if not cases:
        return {
            "schema_version": B14_REHEARSAL_SCHEMA_VERSION,
            "artifact_type": "b14_local_execution_dry_run_pack",
            "status": "blocked",
            "mission_id": mission_id,
            "proposal_run_id": proposal_run_id,
            "reasons": ["no_cases"],
            "cases": [],
            "launch_ready_cases": [],
            "blocked_cases": [],
            "summary_path": "",
            "created": False,
            "replayed": False,
            "morning_brief": {},
        }

    rehearsal_results: List[Dict[str, Any]] = []
    all_blockers: List[str] = []
    launch_ready_case_ids: List[str] = []
    blocked_case_ids: List[str] = []

    for idx, case in enumerate(cases):
        case_payload = dict(case or {})
        case_id = _normalize(case_payload.get("case_id") or f"case_{idx + 1}")
        proposal_token = _safe_candidate_id(proposal_run_id)[:6] or "pr3"
        candidate_id = f"{proposal_token}_{_sha256_hex(f'{proposal_run_id}:{case_id}:{idx}')[:10]}"
        intent = build_local_execution_intent(
            mission_id=mission_id,
            proposal_run_id=proposal_run_id,
            candidate_id=candidate_id,
            scenario_id="b14_pack_rehearsal",
            execution_task=_normalize(case_payload.get("execution_task")),
            checkpoint_path=_normalize(case_payload.get("checkpoint_path")),
            cache_path=_normalize(case_payload.get("cache_path")),
            script_path=_normalize(case_payload.get("script_path")),
            script_cache_mapping_confirmed=bool(case_payload.get("script_cache_mapping_confirmed")),
            actor=actor,
            available_cpu_cores=case_payload.get("available_cpu_cores"),
            branch_override=_normalize(case_payload.get("branch_override")),
        )
        preflight = assess_local_execution_preflight(intent)
        synthetic = _simulate_execution_result_from_preflight(preflight)
        decision = judge_local_execution_after_result(preflight, synthetic)
        cycle = record_local_execution_cycle(
            mission=mission,
            preflight=preflight,
            result=synthetic,
            phase=decision.get("next_action") or LocalExecutionAction.HOLD.value,
        )
        bundle_path = _normalize(cycle.get("narration_bundle", {}).get("bundle_path"))
        case_launch: Dict[str, Any] = {}
        launch_blockers: List[str] = []
        if _normalize(preflight.get("status")) == "pass" and decision.get("worth_continuing"):
            case_launch = assess_remote_boundary_readiness(
                mission_id=mission_id,
                proposal_run_id=proposal_run_id,
                candidate_id=candidate_id,
                case_id=case_id,
                preflight=preflight,
                decision=decision,
                actor=actor,
            )
            if case_launch.get("status") == "ready":
                launch_ready_case_ids.append(case_id)
            else:
                launch_blockers.extend(_to_string_list(case_launch.get("blockers")))
        else:
            fallback_reason = _normalize(preflight.get("status")) or "preflight_blocked"
            reasons = _to_string_list(preflight.get("reasons"))
            reason = reasons[0] if reasons else fallback_reason
            if reason == "blocked":
                reason = fallback_reason
            case_launch = {
                "status": "blocked",
                "candidate_id": candidate_id,
                "reasons": [reason],
                "next_required_approval": reason or "resolve_preflight_blockers",
                "source_work_order_id": f"{proposal_run_id}_{case_id}",
                "launch_intent_id": "",
                "idempotency_key": "",
            }
            launch_blockers.extend(reasons if reasons else [reason])

        blockers = [item for item in _to_string_list(preflight.get("reasons")) if item != "blocked"]
        blockers.extend(_normalize(item) for item in launch_blockers if _normalize(item))
        blockers = list(dict.fromkeys(blockers))
        rehearsal_results.append(
            {
                "case_id": case_id,
                "preflight": preflight,
                "decision": decision,
                "narration_bundle": cycle.get("narration_bundle", {}),
                "narration_event": cycle.get("narration_event", {}),
                "launch_intent": case_launch,
                "blockers": blockers,
            }
        )
        if launch_blockers:
            blocked_case_ids.append(case_id)
        all_blockers.extend(blockers)

    morning_brief = write_morning_brief(mission_id=mission_id)
    remote_boundary_report = write_remote_boundary_readiness_report(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        cases=rehearsal_results,
        actor=actor,
        write_report=write_summary,
    )

    launch_ready = len(launch_ready_case_ids) > 0 and len(blocked_case_ids) == 0
    status = "ready" if launch_ready else "blocked"
    status_reasons = sorted({_normalize(item) for item in all_blockers if _normalize(item)})

    morning_brief_summary = dict(morning_brief.get("summary") or {})
    mission_remote_summary = dict(morning_brief_summary.get("mission_remote") or {})
    continuation_observations_summary = dict(
        morning_brief_summary.get("continuation_observations") or {}
    )

    result_payload: Dict[str, Any] = {
        "schema_version": B14_REHEARSAL_SCHEMA_VERSION,
        "artifact_type": "b14_local_execution_dry_run_pack",
        "status": status,
        "mission_id": mission_id,
        "proposal_run_id": proposal_run_id,
        "case_count": len(rehearsal_results),
        "launch_ready_case_count": len(launch_ready_case_ids),
        "blocked_case_count": len(blocked_case_ids),
        "launch_ready_cases": sorted(set(launch_ready_case_ids)),
        "blocked_cases": sorted(set(blocked_case_ids)),
        "remote_boundary": "not_reached",
        "reasons": status_reasons,
        "cases": rehearsal_results,
        "remote_boundary_report_path": remote_boundary_report.get("path", ""),
        "remote_boundary_readiness_report_path": remote_boundary_report.get("path", ""),
        "mission_remote": mission_remote_summary,
        "continuation_observations": continuation_observations_summary,
        "morning_brief": {
            "status": morning_brief.get("status"),
            "event_count": morning_brief.get("event_count"),
            "summary_path": morning_brief.get("summary_path"),
            "brief_path": morning_brief.get("brief_path"),
            "remote_boundary": morning_brief.get("remote_boundary"),
            "mission_remote": mission_remote_summary,
            "continuation_observations": continuation_observations_summary,
            "b14_l74_artifact_readiness_report_path": (
                morning_brief_summary.get("b14_l74_artifact_readiness_report_path", "")
            ),
            "b14_l74_artifact_readiness_status": (
                morning_brief_summary.get("b14_l74_artifact_readiness_status", "")
            ),
            "b14_l74_artifact_readiness_next_recommendation": (
                morning_brief_summary.get("b14_l74_artifact_readiness_next_recommendation", "")
            ),
            "b14_l74_artifact_readiness_route_reasons": (
                morning_brief_summary.get("b14_l74_artifact_readiness_route_reasons", [])
            ),
            "b14_l74_artifact_readiness_present_valid_artifact_ids": (
                morning_brief_summary.get("b14_l74_artifact_readiness_present_valid_artifact_ids", [])
            ),
            "b14_l74_artifact_readiness_missing_artifact_ids": (
                morning_brief_summary.get("b14_l74_artifact_readiness_missing_artifact_ids", [])
            ),
            "b14_l74_artifact_readiness_invalid_artifact_ids": (
                morning_brief_summary.get("b14_l74_artifact_readiness_invalid_artifact_ids", [])
            ),
        },
        "remote_boundary_report": {
            "status": remote_boundary_report.get("status"),
            "path": remote_boundary_report.get("path"),
            "case_count": remote_boundary_report.get("case_count"),
            "ready_case_count": remote_boundary_report.get("ready_case_count"),
            "blocked_case_count": remote_boundary_report.get("blocked_case_count"),
            "created": remote_boundary_report.get("created"),
            "replayed": remote_boundary_report.get("replayed"),
            "mission_remote": remote_boundary_report.get("mission_remote", {}),
            "continuation_observations": remote_boundary_report.get(
                "continuation_observations", {}
            ),
        },
    }
    if not write_summary:
        result_payload["summary_path"] = ""
        result_payload["created"] = False
        result_payload["replayed"] = False
        result_payload["remote_boundary_report"]["path"] = ""
        result_payload["remote_boundary_report_path"] = ""
        result_payload["remote_boundary_readiness_report_path"] = ""
        return result_payload

    summary_dir = continuation_night_auto_local_execution_dir(
        mission_id,
        proposal_run_id,
        B14_PACK_REHEARSAL_CANDIDATE_ID,
        B14_REHEARSAL_DIR,
    )
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / B14_REHEARSAL_SUMMARY_NAME
    serialized = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    created = True
    replayed = False
    if summary_path.exists():
        existing = summary_path.read_text(encoding="utf-8")
        if existing == serialized:
            created = False
            replayed = True
    if not replayed:
        summary_path.write_text(serialized, encoding="utf-8")
    result_payload["summary_path"] = str(summary_path)
    result_payload["created"] = created
    result_payload["replayed"] = replayed
    return result_payload


def list_local_execution_roots(*, mission_id: str) -> List[str]:
    root = continuation_night_auto_local_execution_root(mission_id)
    if not root.exists():
        return []
    return [str(path) for path in root.rglob("*.json")]


__all__ = [
    "LocalExecutionAction",
    "LocalExecutionBranch",
    "LocalExecutionDecision",
    "LocalExecutionIntent",
    "LocalExecutionPreflight",
    "LocalExecutionTask",
    "assess_local_execution_preflight",
    "build_local_execution_intent",
    "build_local_execution_narration_event",
    "record_local_execution_cycle",
    "judge_local_execution_after_result",
    "list_local_execution_roots",
    "load_local_execution_policy",
    "load_local_execution_preflight_schema",
    "load_remote_boundary_preflight_schema",
    "build_remote_boundary_readiness_report",
    "write_remote_boundary_readiness_report",
    "write_local_execution_narration_bundle",
    "assess_remote_boundary_readiness",
    "run_b14_local_execution_dry_run_pack",
    "read_b14_pack_rehearsal_summary",
    "b14_pack_rehearsal_root",
    "b14_pack_rehearsal_summary_path",
]

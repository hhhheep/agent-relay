from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import subprocess

from . import knowledge_to_proposal
import solution_agent.orchestrator.paths as orchestrator_paths
from solution_agent.tools.patch_tools import build_patch_from_intent
from .repair_executor_selector import (
    select_repair_executor,
    write_repair_execution_artifacts,
    write_repair_executor_decision,
)
from .source_repair import build_source_repair_plan, write_source_repair_plan


REHEARSAL_SCHEMA_VERSION = 1
DEFAULT_B14_L72_TESTS = [
    "script_registry_schema_test",
    "execution_plan_gate_test",
    "relevant_golden_routing_test",
    "wrapper_import_test",
    "wrapper_help_or_dry_run",
    "expected_outputs_contract_test",
]


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _coerce_tests(value: List[str] | None) -> List[str]:
    return sorted({_normalize(item) for item in (value or []) if _normalize(item)})


def _read_payload(payload: dict | Path | str) -> Dict[str, Any]:
    if isinstance(payload, (Path, str)):
        data = json.loads(Path(payload).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("summary payload must be a JSON object")
        return data
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("summary payload must be a dict, Path, or string path")


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


def _coerce_rehearsal_readiness(summary: Dict[str, Any], *, reasons: List[str]) -> Dict[str, Any]:
    apply_readiness = summary.get("apply_readiness")
    if not isinstance(apply_readiness, dict):
        if apply_readiness is None:
            reasons.append("self_repair_apply_readiness_missing")
        else:
            reasons.append("self_repair_apply_readiness_invalid")
        return {}
    blockers = _coerce_str_list(apply_readiness.get("blockers"))
    status = _normalize(apply_readiness.get("status")).lower()
    allowed_statuses = {"", "ready", "blocked", "unknown", "partial"}
    if status and status not in allowed_statuses:
        reasons.append("self_repair_apply_readiness_status_invalid")
    route = (_normalize(apply_readiness.get("route")) or _normalize(summary.get("executor_route"))).lower()
    can_apply = _coerce_bool(apply_readiness.get("can_apply"), default=False)
    return {
        "route": route,
        "can_apply": can_apply,
        "status": status,
        "blockers": blockers,
    }


def _coerce_rehearsal_artifact(value: Any) -> Dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (Path, str)):
        try:
            return _read_payload(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
    return None


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _normalize(value)
        return [normalized] if normalized else []
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value if _normalize(item)]
    return [_normalize(value)]


def _write_execution_audit_to_repair_result(
    path: Path | str,
    *,
    route: str,
    apply_result: Dict[str, Any],
) -> None:
    result_path = Path(path)
    normalized_route = _normalize(route).lower()
    if not result_path.exists():
        return
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    execution_status = (
        "applied"
        if normalized_route in {"deterministic_self", "bounded_self_repair"} and _coerce_bool(apply_result.get("patch_applied"))
        else "blocked"
        if normalized_route in {"deterministic_self", "bounded_self_repair"}
        else "not_attempted"
    )
    blockers = _coerce_str_list(apply_result.get("blockers"))
    payload.update(
        {
            "execution_status": execution_status,
            "execution_blockers": blockers,
            "execution_side_effects": {
                "source_edited": _coerce_bool(apply_result.get("source_edited")),
                "dispatch_performed": _coerce_bool(apply_result.get("dispatch_performed")),
                "remote_execution_performed": _coerce_bool(apply_result.get("remote_execution_performed")),
            },
        }
    )
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def _run_controlled_self_repair(
    *,
    decision: Dict[str, Any],
    source_repair_plan: Dict[str, Any],
) -> Dict[str, Any]:
    route = _normalize(decision.get("decision", {}).get("route")).lower()
    patch_intent = source_repair_plan.get("patch_intent") if isinstance(source_repair_plan, dict) else None
    blockers = _coerce_str_list(decision.get("blockers"))
    if route not in {"deterministic_self", "bounded_self_repair"}:
        return {
            "patch_applied": False,
            "source_edited": False,
            "dispatch_performed": False,
            "remote_execution_performed": False,
            "blockers": blockers,
            "decision_route": route,
        }

    if blockers:
        return {
            "patch_applied": False,
            "source_edited": False,
            "dispatch_performed": False,
            "remote_execution_performed": False,
            "blockers": blockers,
            "decision_route": route,
            "blocked_by_preflight": True,
        }

    if not isinstance(patch_intent, dict):
        return {
            "patch_applied": False,
            "source_edited": False,
            "dispatch_performed": False,
            "remote_execution_performed": False,
            "blockers": blockers + ["repair_plan_missing_patch_intent"],
            "decision_route": route,
        }

    target_files = _coerce_str_list(source_repair_plan.get("proposed_patch_scope", {}).get("files_or_modules"))
    if not target_files:
        target_files = [_normalize(patch_intent.get("target_file"))]
    target_files = [item for item in target_files if item]

    patch_result = build_patch_from_intent(
        orchestrator_paths.root(),
        patch_intent,
        write_scope=target_files or [_normalize(patch_intent.get("target_file"))],
    )
    if not patch_result.ok:
        return {
            "patch_applied": False,
            "source_edited": False,
            "dispatch_performed": False,
            "remote_execution_performed": False,
            "blockers": blockers + [f"self_repair_apply_validation:{error}" for error in patch_result.errors],
            "decision_route": route,
            "quality_report": patch_result.quality_report,
        }

    apply_result = subprocess.run(
        ["git", "apply", "-"],
        cwd=orchestrator_paths.root(),
        input=patch_result.patch_text,
        text=True,
        capture_output=True,
    )
    if apply_result.returncode != 0:
        reason = _normalize(apply_result.stderr) or _normalize(apply_result.stdout) or "git_apply_failed"
        return {
            "patch_applied": False,
            "source_edited": False,
            "dispatch_performed": False,
            "remote_execution_performed": False,
            "blockers": blockers + [f"self_repair_apply_failed:{reason}"],
            "decision_route": route,
            "patch_text": patch_result.patch_text,
            "quality_report": patch_result.quality_report,
        }

    return {
        "patch_applied": True,
        "source_edited": True,
        "dispatch_performed": False,
        "remote_execution_performed": False,
        "blockers": blockers,
        "decision_route": route,
        "patch_text": patch_result.patch_text,
        "quality_report": patch_result.quality_report,
    }

def run_self_repair_rehearsal(
    *,
    result_dir: Path | str,
    output_dir: Path | str,
    source_task_id: str,
    downstream_task_id: str | None = None,
    repair_type: str | None = None,
    available_tests: List[str] | None = None,
) -> Dict[str, Any]:
    """Run a locally bounded self-repair rehearsal from result to rerun intent."""

    output_path = Path(output_dir)
    tests = _coerce_tests(available_tests)
    plan = build_source_repair_plan(
        result_dir,
        source_task_id=source_task_id,
        downstream_task_id=downstream_task_id,
    )
    plan_write = write_source_repair_plan(plan)
    decision = select_repair_executor(
        plan,
        repair_type=repair_type,
        available_tests=tests,
    )
    decision_path = output_path / "repair_executor_decision.json"
    decision_write = write_repair_executor_decision(decision, decision_path)
    lifecycle_write = write_repair_execution_artifacts(
        decision,
        source_repair_plan=plan,
        output_dir=output_path,
    )

    route = _normalize(decision.get("decision", {}).get("route")).lower()
    decision_blockers = [
        _normalize(blocker)
        for blocker in decision.get("blockers", [])
        if _normalize(blocker)
    ]
    apply_result = _run_controlled_self_repair(decision=decision, source_repair_plan=plan)
    if lifecycle_write.get("status") in {"written", "replayed"}:
        result_path = lifecycle_write.get("repair_result_path")
        if result_path:
            _write_execution_audit_to_repair_result(
                result_path,
                route=route,
                apply_result=apply_result,
            )
    apply_blockers = _coerce_str_list(apply_result.get("blockers"))
    summary_blockers = list(dict.fromkeys(decision_blockers + apply_blockers))
    apply_readiness = {
        "route": route,
        "can_apply": bool(route in {"deterministic_self", "bounded_self_repair"} and not apply_blockers),
        "status": "ready" if route in {"deterministic_self", "bounded_self_repair"} and not apply_blockers else "blocked",
        "blockers": apply_blockers,
    }
    can_self_repair = bool(apply_readiness["can_apply"])
    summary = {
        "schema_version": REHEARSAL_SCHEMA_VERSION,
        "artifact_type": "self_repair_rehearsal_summary",
        "status": "completed",
        "source_task_id": source_task_id,
        "downstream_task_id": downstream_task_id or plan.get("rerun_intent", {}).get("downstream_task_id", ""),
        "needs_source_repair": plan.get("needs_source_repair", False),
        "repair_type": decision.get("repair_type", ""),
        "executor_route": route,
        "can_self_repair": can_self_repair,
        "apply_readiness": apply_readiness,
        "blocked_by": summary_blockers,
        "patch_intent_validation_errors": decision.get("patch_intent_validation_errors", []),
        "patch_intent_generated": isinstance(plan.get("patch_intent"), dict),
        "patch_applied": apply_result.get("patch_applied", False),
        "source_edited": apply_result.get("source_edited", False),
        "dispatch_performed": apply_result.get("dispatch_performed", False),
        "remote_execution_performed": apply_result.get("remote_execution_performed", False),
        "artifacts": {
            "source_repair_plan": plan_write.get("plan_path"),
            "patch_intent": plan_write.get("patch_intent_path"),
            "repair_executor_decision": str(decision_path),
            "repair_result": lifecycle_write.get("repair_result_path"),
            "rerun_intent": lifecycle_write.get("rerun_intent_path"),
        },
        "next_recommendation": (
            "apply_bounded_self_repair_in_controlled_worktree"
            if can_self_repair and route in {"deterministic_self", "bounded_self_repair"}
            else "route_to_codex_or_human_before_rerun"
        ),
    }
    summary_path = output_path / "self_repair_rehearsal_summary.json"
    _write_json(summary_path, summary)
    return {
        "status": "written",
        "summary_path": str(summary_path),
        "summary": summary,
        "source_repair_plan_write": plan_write,
        "repair_executor_decision_write": decision_write,
        "repair_lifecycle_write": lifecycle_write,
    }


def run_b14_l72_self_repair_rehearsal(
    *,
    result_dir: Path | str,
    output_dir: Path | str,
    available_tests: List[str] | None = None,
) -> Dict[str, Any]:
    return run_self_repair_rehearsal(
        result_dir=result_dir,
        output_dir=output_dir,
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        available_tests=available_tests or DEFAULT_B14_L72_TESTS,
    )


def build_proposal_candidates_from_rehearsal_summary(
    summary: dict | Path | str,
    knowledge_subset: dict | Path | str | None = None,
    *,
    actor: str = "system",
) -> Dict[str, Any]:
    summary_payload = _read_payload(summary)
    source_task_id = _normalize(summary_payload.get("source_task_id")) or "unknown_task"
    knowledge = _read_payload(knowledge_subset) if knowledge_subset is not None else {}
    artifacts = summary_payload.get("artifacts") if isinstance(summary_payload.get("artifacts"), dict) else {}
    readiness_blockers: List[str] = []
    apply_readiness = _coerce_rehearsal_readiness(summary_payload, reasons=readiness_blockers)
    apply_status = _normalize(apply_readiness.get("status"))
    apply_blockers = _coerce_str_list(apply_readiness.get("blockers"))
    repair_result_payload = _coerce_rehearsal_artifact(artifacts.get("repair_result"))
    repair_execution_status = _normalize(repair_result_payload.get("execution_status")).lower()
    execution_blocked = False

    can_apply = _coerce_bool(apply_readiness.get("can_apply"), default=False)
    if apply_status and apply_status not in {"", "ready"}:
        status_blocker = _normalize(apply_status)
        if status_blocker == "partial":
            readiness_blockers.append("self_repair_apply_readiness_partial")
        elif status_blocker == "blocked":
            readiness_blockers.append("self_repair_apply_readiness_blocked")
        elif status_blocker == "unknown":
            readiness_blockers.append("self_repair_apply_readiness_unknown")
        else:
            readiness_blockers.append(f"self_repair_apply_readiness_status:{status_blocker}")
    if repair_execution_status == "blocked":
        readiness_blockers.append("self_repair_repair_result_execution_blocked")
        execution_blocked = True
    elif repair_execution_status and repair_execution_status not in {"applied", "not_attempted"}:
        readiness_blockers.append(f"self_repair_repair_result_execution_status_invalid:{repair_execution_status}")
        readiness_blockers.append(f"self_repair_repair_result_execution_status:{repair_execution_status}")
        execution_blocked = True
    readiness_blocked = apply_status not in {"", "ready"} or not can_apply or bool(apply_blockers) or execution_blocked
    next_step_readiness = {
        "schema_version": 1,
        "can_stage": not readiness_blocked,
        "route": (
            _normalize(apply_readiness.get("route"))
            or _normalize(summary_payload.get("executor_route"))
            or _normalize(summary_payload.get("next_recommendation"))
        ).lower(),
        "blockers": apply_blockers,
    }
    next_step_readiness["blockers"].extend(readiness_blockers)
    if readiness_blocked and not apply_blockers:
        next_step_readiness["blockers"].append("self_repair_apply_readiness_not_ready")

    patch_candidate: Dict[str, Any] = {
        "kind": "bounded_probe",
        "reason": (
            "Self-repair rehearsal candidate for bounded continuation."
            if can_apply
            else "Self-repair rehearsal candidate blocked by apply readiness; staging guard must block."
        ),
        "source_task_id": _normalize(knowledge.get("source_task_id")) or source_task_id,
        "line": (
            _normalize(knowledge.get("line_filter"))
            or _normalize(knowledge.get("line"))
            or "unknown"
        ),
        "workplace_profile": (
            _normalize(knowledge.get("workplace_profile_filter"))
            or _normalize(knowledge.get("workplace_profile"))
            or "unknown"
        ),
        "risk_flags": ["self_repair_rehearsal"],
        "claim_limitations": [
            "Self-repair replay should pass controlled staging and repair-readiness checks before any continuation."
        ],
        "expected_outputs": sorted({_normalize(value) for value in {
            "repair_result",
            "rerun_intent",
            "repair_executor_decision",
        } if _normalize(artifacts.get(value))}),
        "can_stage": bool(can_apply),
        "repair_result": repair_result_payload,
        "rerun_intent": _coerce_rehearsal_artifact(artifacts.get("rerun_intent")),
        "apply_readiness": apply_readiness,
        "next_step_readiness": next_step_readiness,
    }
    repair_plan_artifact = artifacts.get("repair_executor_decision")
    if _normalize(repair_plan_artifact):
        patch_candidate["repair_executor_decision"] = repair_plan_artifact

    council_response = {
        "schema_version": knowledge_to_proposal.PROPOSAL_CANDIDATE_SCHEMA_VERSION,
        "council_id": f"self_repair_rehearsal_{source_task_id}",
        "status": "valid",
        "interpretation_votes": [
            {
                "model": "self_repair_rehearsal",
                "vote": "bounded_self_repair_candidate",
            }
        ],
        "risk_flags": ["self_repair_rehearsal"],
        "next_step_candidates": [patch_candidate],
        "disagreement_summary": f"Rehearsal summary for {source_task_id}",
        "category": "source_code_required",
    }

    return knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(
        council_response,
        knowledge_subset=knowledge_subset,
        actor=actor,
    )


__all__ = [
    "run_self_repair_rehearsal",
    "run_b14_l72_self_repair_rehearsal",
    "build_proposal_candidates_from_rehearsal_summary",
]

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from . import mission_remote_loop
from .bounded_validation_bucket import ARTIFACT_READINESS_REPORT_NAME
from .paths import (
    continuation_launch_preflight_dir,
    continuation_night_auto_local_execution_root,
    continuation_night_auto_morning_brief_path,
)
from .memory_observer import read_continuation_observations, summarize_continuation_observations

B14_REHEARSAL_SUMMARY_NAME = "b14_local_execution_rehearsal_summary.json"
B14_REHEARSAL_REMOTE_BOUNDARY_REPORT_NAME = "remote_boundary_readiness_report.json"


BRIEF_SIDE_EFFECTS = {
    "scheduler_dispatch": False,
    "active_inbox_write": False,
    "registry_mutation": False,
    "remote_api_call": False,
    "dataset_or_checkpoint_mutation": False,
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["event_source_path"] = str(path)
            events.append(payload)
    return events


def _load_manual_launch_preflight_records() -> List[Dict[str, Any]]:
    root = continuation_launch_preflight_dir()
    records: List[Dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload["preflight_path"] = str(path)
        records.append(payload)
    return records


def _load_latest_manual_launch_preflight_record() -> Dict[str, Any]:
    records = _load_manual_launch_preflight_records()
    if not records:
        return {}
    best_record = records[0]
    root = continuation_launch_preflight_dir()
    latest_mtime = -1.0
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            current_mtime = latest_mtime
        if current_mtime >= latest_mtime:
            latest_mtime = current_mtime
            payload = dict(payload)
            payload["preflight_path"] = str(path)
            best_record = payload
    return best_record


def _load_latest_remote_boundary_report(*, mission_id: str) -> str:
    root = continuation_night_auto_local_execution_root(mission_id)
    if not root.exists():
        return ""

    latest_path = ""
    latest_mtime = -1.0
    for summary_path in root.rglob(B14_REHEARSAL_SUMMARY_NAME):
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        report_path = _normalize(payload.get("remote_boundary_report", {}).get("path"))
        if not report_path:
            continue
        try:
            current_mtime = summary_path.stat().st_mtime
        except OSError:
            current_mtime = latest_mtime
            if current_mtime >= latest_mtime:
                latest_mtime = current_mtime
                latest_path = report_path
    if latest_path:
        return latest_path

    for report_path in root.rglob(B14_REHEARSAL_REMOTE_BOUNDARY_REPORT_NAME):
        try:
            current_mtime = report_path.stat().st_mtime
        except OSError:
            current_mtime = latest_mtime
        if current_mtime >= latest_mtime:
            latest_mtime = current_mtime
            latest_path = str(report_path)
    return latest_path


def _load_remote_boundary_report_payload(*, path: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_latest_b14_l74_artifact_readiness_report(*, mission_id: str) -> str:
    root = continuation_night_auto_local_execution_root(mission_id)
    if not root.exists():
        return ""

    latest_path = ""
    latest_mtime = -1.0
    for report_path in root.rglob(ARTIFACT_READINESS_REPORT_NAME):
        try:
            current_mtime = report_path.stat().st_mtime
        except OSError:
            current_mtime = latest_mtime
        if current_mtime >= latest_mtime:
            latest_mtime = current_mtime
            latest_path = str(report_path)
    return latest_path


def _load_manual_launch_preflight_statuses() -> List[str]:
    records = _load_manual_launch_preflight_records()
    if not records:
        return []
    return _unique_ordered(
        [_normalize(record.get("status")) for record in records if _normalize(record.get("status"))]
    )


def collect_local_execution_activity_events(*, mission_id: str) -> List[Dict[str, Any]]:
    root = continuation_night_auto_local_execution_root(mission_id)
    if not root.exists():
        return []
    events: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*_events.jsonl")):
        events.extend(_read_jsonl(path))
    return events


def _unique_ordered(values: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def summarize_local_execution_activity(*, mission_id: str) -> Dict[str, Any]:
    events = collect_local_execution_activity_events(mission_id=mission_id)
    observations = read_continuation_observations(run_id=mission_id)
    mission_remote_summary = mission_remote_loop.read_local_mission_remote_history(
        continuation_night_auto_local_execution_root(mission_id).parent,
        limit=3,
    )
    phase_counts = Counter(str(event.get("phase") or "unknown") for event in events)
    preflight_statuses = _unique_ordered(
        [
            str(event.get("preflight_status") or "").strip()
            for event in events
            if str(event.get("preflight_status") or "").strip()
        ]
    )
    judge_next_actions = _unique_ordered(
        [
            str(event.get("judge_next_action") or "").strip()
            for event in events
            if str(event.get("judge_next_action") or "").strip()
        ]
    )
    judge_stop_reasons = _unique_ordered(
        [
            str(event.get("judge_stop_reason") or "").strip()
            for event in events
            if str(event.get("judge_stop_reason") or "").strip()
        ]
    )
    judge_decisions = [
        {"next_action": str(event.get("judge_next_action") or "none").strip() or "none", "stop_reason": str(event.get("judge_stop_reason") or "none").strip() or "none"}
        for event in events
        if str(event.get("judge_next_action") or "").strip()
    ]
    blockers = _unique_ordered(
        [
            str(blocker)
            for event in events
            for blocker in event.get("blocked_by", [])
            if str(blocker or "").strip()
        ]
    )
    next_steps = _unique_ordered([str(event.get("next_step") or "") for event in events])
    headlines = _unique_ordered([str(event.get("headline") or "") for event in events])
    source_refs = _unique_ordered(
        [
            str(ref)
            for event in events
            for ref in event.get("source_refs", [])
            if str(ref or "").strip()
        ]
    )
    claim_limits = sorted(
        {
            str(limit)
            for event in events
            for limit in event.get("claim_limits", [])
            if str(limit or "").strip()
        }
    )
    side_effects = dict(BRIEF_SIDE_EFFECTS)
    for event in events:
        event_side_effects = event.get("side_effects")
        if not isinstance(event_side_effects, dict):
            continue
        for key in side_effects:
            side_effects[key] = side_effects[key] or bool(event_side_effects.get(key))

    remote_boundary = "not_reached"
    latest_preflight_status = "none"
    latest_judge_next_action = "none"
    latest_judge_stop_reason = "none"
    latest_next_step = "none"
    if events:
        latest = events[-1]
        remote_boundary = str(latest.get("remote_boundary") or "not_reached").strip() or "not_reached"
        latest_preflight_status = str(latest.get("preflight_status") or "none").strip()
        latest_judge_next_action = str(latest.get("judge_next_action") or "none").strip()
        latest_judge_stop_reason = str(latest.get("judge_stop_reason") or "none").strip()
        latest_next_step = str(latest.get("next_step") or "none").strip()
    if latest_preflight_status == "":
        latest_preflight_status = "none"
    if latest_judge_next_action == "":
        latest_judge_next_action = "none"
    if latest_judge_stop_reason == "":
        latest_judge_stop_reason = "none"
    if latest_next_step == "":
        latest_next_step = "none"

    manual_launch_preflight_statuses = _load_manual_launch_preflight_statuses()
    latest_manual_launch_status = manual_launch_preflight_statuses[-1] if manual_launch_preflight_statuses else "not_available"
    latest_manual_launch_record = _load_latest_manual_launch_preflight_record()
    manual_launch_blockers = list(
        latest_manual_launch_record.get("blockers")
        or latest_manual_launch_record.get("reasons")
        or []
    )
    manual_launch_ready = bool(
        latest_manual_launch_record.get("ready_for_manual_remote_launch")
        or latest_manual_launch_record.get("ready_for_remote_launch")
    )
    manual_launch_next_required_approval = latest_manual_launch_record.get("next_required_approval") or "none"
    manual_launch_preflight_path = _normalize(latest_manual_launch_record.get("preflight_path"))
    remote_boundary_report_path = _load_latest_remote_boundary_report(mission_id=mission_id)
    remote_boundary_payload = _load_remote_boundary_report_payload(path=remote_boundary_report_path)
    remote_boundary_report_summary = remote_boundary_payload.get("summary")
    remote_boundary_next_steps = (
        remote_boundary_report_summary.get("next_steps")
        if isinstance(remote_boundary_report_summary, dict)
        else []
    )
    remote_boundary_all_blockers = (
        remote_boundary_report_summary.get("all_case_blockers")
        if isinstance(remote_boundary_report_summary, dict)
        else []
    )
    artifact_readiness_report_path = _load_latest_b14_l74_artifact_readiness_report(mission_id=mission_id)
    artifact_readiness_payload = _load_remote_boundary_report_payload(path=artifact_readiness_report_path)
    artifact_readiness_summary = artifact_readiness_payload.get("summary")
    artifact_readiness_route_reasons = (
        artifact_readiness_summary.get("route_reasons")
        if isinstance(artifact_readiness_summary, dict)
        else []
    )
    artifact_readiness_present_valid_ids = (
        artifact_readiness_summary.get("present_valid_artifact_ids")
        if isinstance(artifact_readiness_summary, dict)
        else []
    )
    artifact_readiness_missing_ids = (
        artifact_readiness_summary.get("missing_artifact_ids")
        if isinstance(artifact_readiness_summary, dict)
        else []
    )
    artifact_readiness_invalid_ids = (
        artifact_readiness_summary.get("invalid_artifact_ids")
        if isinstance(artifact_readiness_summary, dict)
        else []
    )
    continuation_observations_summary = summarize_continuation_observations(observations=observations)

    remote_boundary_reached = remote_boundary == "reached"

    status = "ready" if events or mission_remote_summary.get("present") else "empty"
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "status": status,
        "event_count": len(events),
        "phase_counts": dict(sorted(phase_counts.items())),
        "preflight_statuses": preflight_statuses,
        "judge_next_actions": judge_next_actions,
        "judge_stop_reasons": judge_stop_reasons,
        "latest_preflight_status": latest_preflight_status,
        "latest_judge_next_action": latest_judge_next_action,
        "latest_judge_stop_reason": latest_judge_stop_reason,
        "latest_judge_decision": {"next_action": latest_judge_next_action, "stop_reason": latest_judge_stop_reason},
        "latest_next_step": latest_next_step,
        "remote_boundary": remote_boundary,
        "judge_decisions": judge_decisions,
        "headlines": headlines,
        "blockers": blockers,
        "next_steps": next_steps,
        "source_refs": source_refs,
        "claim_limits": claim_limits,
        "manual_launch_preflight_statuses": manual_launch_preflight_statuses,
        "latest_manual_launch_preflight_status": latest_manual_launch_status,
        "manual_launch_ready_for_remote_launch": manual_launch_ready,
        "manual_launch_blockers": manual_launch_blockers,
        "manual_launch_next_required_approval": manual_launch_next_required_approval,
        "manual_launch_preflight_path": manual_launch_preflight_path,
        "remote_boundary_reached": remote_boundary_reached,
        "remote_boundary_report_path": remote_boundary_report_path,
        "remote_boundary_report_status": _normalize(remote_boundary_payload.get("status")),
        "remote_boundary_case_count": int(remote_boundary_payload.get("case_count") or 0),
        "remote_boundary_ready_case_count": int(remote_boundary_payload.get("ready_case_count") or 0),
        "remote_boundary_blocked_case_count": int(remote_boundary_payload.get("blocked_case_count") or 0),
        "remote_boundary_next_steps": _unique_ordered([_normalize(item) for item in remote_boundary_next_steps]),
        "remote_boundary_blockers": _unique_ordered([_normalize(item) for item in remote_boundary_all_blockers]),
        "b14_l74_artifact_readiness_report_path": artifact_readiness_report_path,
        "b14_l74_artifact_readiness_status": _normalize(artifact_readiness_payload.get("status")),
        "b14_l74_artifact_readiness_next_recommendation": _normalize(
            artifact_readiness_payload.get("next_recommendation")
        ),
        "b14_l74_artifact_readiness_reasons": _unique_ordered(
            [_normalize(item) for item in artifact_readiness_payload.get("reasons", [])]
        ),
        "b14_l74_artifact_readiness_route_reasons": _unique_ordered(
            [_normalize(item) for item in artifact_readiness_route_reasons]
        ),
        "b14_l74_artifact_readiness_present_valid_artifact_ids": _unique_ordered(
            [_normalize(item) for item in artifact_readiness_present_valid_ids]
        ),
        "b14_l74_artifact_readiness_missing_artifact_ids": _unique_ordered(
            [_normalize(item) for item in artifact_readiness_missing_ids]
        ),
        "b14_l74_artifact_readiness_invalid_artifact_ids": _unique_ordered(
            [_normalize(item) for item in artifact_readiness_invalid_ids]
        ),
        "mission_remote": mission_remote_summary,
        "continuation_observations": continuation_observations_summary,
        "side_effects": side_effects,
        "events": events,
    }


def render_morning_brief(summary: Dict[str, Any]) -> str:
    mission_id = str(summary.get("mission_id") or "unknown_mission")
    lines = [
        f"# Night Researcher Morning Brief: {mission_id}",
        "",
        f"- status: {summary.get('status')}",
        f"- activity_events: {summary.get('event_count', 0)}",
        f"- phase_counts: {json.dumps(summary.get('phase_counts', {}), sort_keys=True)}",
        "",
        "## What Happened",
    ]
    headlines = summary.get("headlines") or []
    if headlines:
        lines.extend(f"- {headline}" for headline in headlines)
    else:
        lines.append("- No local execution activity narration events found.")

    lines.extend(["", "## Blockers"])
    blockers = summary.get("blockers") or []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")

    lines.extend(["", "## Next Steps"])
    next_steps = summary.get("next_steps") or []
    lines.extend(f"- {item}" for item in next_steps) if next_steps else lines.append("- none")

    lines.extend(["", "## Safety"])
    side_effects = summary.get("side_effects") or {}
    for key in sorted(side_effects):
        lines.append(f"- {key}: {str(bool(side_effects[key])).lower()}")

    remote_boundary_reached = bool(summary.get("remote_boundary_reached"))
    lines.extend(["", "## Remote Boundary"])
    lines.append(f"- remote_boundary: {summary.get('remote_boundary')}")
    lines.append(f"- remote_boundary_reached: {str(remote_boundary_reached).lower()}")
    if _normalize(summary.get("remote_boundary_report_status")):
        lines.append(f"- remote_boundary_report_status: {summary.get('remote_boundary_report_status')}")
    if summary.get("remote_boundary_case_count") is not None:
        lines.append(f"- remote_boundary_case_count: {summary.get('remote_boundary_case_count')}")
        lines.append(f"- remote_boundary_ready_case_count: {summary.get('remote_boundary_ready_case_count')}")
        lines.append(f"- remote_boundary_blocked_case_count: {summary.get('remote_boundary_blocked_case_count')}")
    remote_boundary_next_steps = summary.get("remote_boundary_next_steps") or []
    remote_boundary_blockers = summary.get("remote_boundary_blockers") or []
    if remote_boundary_next_steps:
        lines.append("- remote_boundary_next_steps:")
        lines.extend(f"  - {item}" for item in remote_boundary_next_steps)
    if remote_boundary_blockers:
        lines.append("- remote_boundary_blockers:")
        lines.extend(f"  - {item}" for item in remote_boundary_blockers)
    if _normalize(summary.get("remote_boundary_report_path")):
        lines.append(f"- remote_boundary_report_path: {summary.get('remote_boundary_report_path')}")
    if _normalize(summary.get("b14_l74_artifact_readiness_status")):
        lines.append(f"- b14_l74_artifact_readiness_status: {summary.get('b14_l74_artifact_readiness_status')}")
    if _normalize(summary.get("b14_l74_artifact_readiness_next_recommendation")):
        lines.append(
            "- b14_l74_artifact_readiness_next_recommendation: "
            f"{summary.get('b14_l74_artifact_readiness_next_recommendation')}"
        )
    artifact_readiness_route_reasons = summary.get("b14_l74_artifact_readiness_route_reasons") or []
    if artifact_readiness_route_reasons:
        lines.append("- b14_l74_artifact_readiness_route_reasons:")
        lines.extend(f"  - {item}" for item in artifact_readiness_route_reasons)
    artifact_readiness_reasons = summary.get("b14_l74_artifact_readiness_reasons") or []
    if artifact_readiness_reasons:
        lines.append("- b14_l74_artifact_readiness_reasons:")
        lines.extend(f"  - {item}" for item in artifact_readiness_reasons)
    artifact_readiness_present_valid_ids = summary.get("b14_l74_artifact_readiness_present_valid_artifact_ids") or []
    if artifact_readiness_present_valid_ids:
        lines.append("- b14_l74_artifact_readiness_present_valid_artifact_ids:")
        lines.extend(f"  - {item}" for item in artifact_readiness_present_valid_ids)
    artifact_readiness_missing_ids = summary.get("b14_l74_artifact_readiness_missing_artifact_ids") or []
    if artifact_readiness_missing_ids:
        lines.append("- b14_l74_artifact_readiness_missing_artifact_ids:")
        lines.extend(f"  - {item}" for item in artifact_readiness_missing_ids)
    artifact_readiness_invalid_ids = summary.get("b14_l74_artifact_readiness_invalid_artifact_ids") or []
    if artifact_readiness_invalid_ids:
        lines.append("- b14_l74_artifact_readiness_invalid_artifact_ids:")
        lines.extend(f"  - {item}" for item in artifact_readiness_invalid_ids)
    if _normalize(summary.get("b14_l74_artifact_readiness_report_path")):
        lines.append(
            "- b14_l74_artifact_readiness_report_path: "
            f"{summary.get('b14_l74_artifact_readiness_report_path')}"
        )
    mission_remote = summary.get("mission_remote") or {}
    if mission_remote.get("present"):
        lines.extend(["", "## Mission Remote Rehearsal"])
        lines.append(f"- iteration_count: {mission_remote.get('iteration_count', 0)}")
        lines.append(f"- checkpoint_count: {mission_remote.get('checkpoint_count', 0)}")
        if _normalize(mission_remote.get("latest_iteration_id")):
            lines.append(f"- latest_iteration_id: {mission_remote.get('latest_iteration_id')}")
        if _normalize(mission_remote.get("latest_gate_status")):
            lines.append(f"- latest_gate_status: {mission_remote.get('latest_gate_status')}")
        latest_gate_reason_codes = mission_remote.get("latest_gate_reason_codes") or []
        if latest_gate_reason_codes:
            lines.append("- latest_gate_reason_codes:")
            lines.extend(f"  - {item}" for item in latest_gate_reason_codes)
        if _normalize(mission_remote.get("latest_judge_status")):
            lines.append(f"- latest_judge_status: {mission_remote.get('latest_judge_status')}")
        if _normalize(mission_remote.get("latest_continuation_status")):
            lines.append(
                f"- latest_continuation_status: {mission_remote.get('latest_continuation_status')}"
            )
        if _normalize(mission_remote.get("latest_next_action")):
            lines.append(f"- latest_next_action: {mission_remote.get('latest_next_action')}")
        latest_claim_limits = mission_remote.get("latest_claim_limits") or []
        if latest_claim_limits:
            lines.append("- latest_claim_limits:")
            lines.extend(f"  - {item}" for item in latest_claim_limits)
        if mission_remote.get("latest_information_gain_total_score") is not None:
            lines.append(
                f"- latest_information_gain_total_score: {mission_remote.get('latest_information_gain_total_score')}"
            )
        if _normalize(mission_remote.get("latest_information_gain_routing")):
            lines.append(
                f"- latest_information_gain_routing: {mission_remote.get('latest_information_gain_routing')}"
            )
        lines.append(f"- budget_ledger_entries: {mission_remote.get('budget_ledger_entries', 0)}")
        if _normalize(mission_remote.get("budget_ledger_path")):
            lines.append(f"- budget_ledger_path: {mission_remote.get('budget_ledger_path')}")
        if _normalize(mission_remote.get("latest_iteration_dir")):
            lines.append(f"- latest_iteration_dir: {mission_remote.get('latest_iteration_dir')}")
        if _normalize(mission_remote.get("latest_checkpoint_path")):
            lines.append(f"- latest_checkpoint_path: {mission_remote.get('latest_checkpoint_path')}")
        if _normalize(mission_remote.get("latest_factual_memory_path")):
            lines.append(f"- latest_factual_memory_path: {mission_remote.get('latest_factual_memory_path')}")
        if _normalize(mission_remote.get("latest_kb_update_status")):
            lines.append(f"- latest_kb_update_status: {mission_remote.get('latest_kb_update_status')}")
        if _normalize(mission_remote.get("latest_kb_update_proposal_id")):
            lines.append(
                f"- latest_kb_update_proposal_id: {mission_remote.get('latest_kb_update_proposal_id')}"
            )
        if _normalize(mission_remote.get("latest_kb_update_proposal_dir")):
            lines.append(
                f"- latest_kb_update_proposal_dir: {mission_remote.get('latest_kb_update_proposal_dir')}"
            )
        if _normalize(mission_remote.get("latest_kb_update_global_proposal_dir")):
            lines.append(
                "- latest_kb_update_global_proposal_dir: "
                f"{mission_remote.get('latest_kb_update_global_proposal_dir')}"
            )
        if _normalize(mission_remote.get("latest_followup_proposal_status")):
            lines.append(
                f"- latest_followup_proposal_status: {mission_remote.get('latest_followup_proposal_status')}"
            )
            lines.append(
                "- latest_followup_can_write_proposal: "
                f"{str(bool(mission_remote.get('latest_followup_can_write_proposal'))).lower()}"
            )
            lines.append(
                f"- latest_followup_candidate_count: {mission_remote.get('latest_followup_candidate_count', 0)}"
            )
        latest_followup_reasons = mission_remote.get("latest_followup_reasons") or []
        if latest_followup_reasons:
            lines.append("- latest_followup_reasons:")
            lines.extend(f"  - {item}" for item in latest_followup_reasons)
        recent_history = mission_remote.get("history") or []
        if recent_history:
            lines.append("- recent_history:")
            for item in recent_history:
                iteration_id = _normalize(item.get("iteration_id")) or "unknown_iteration"
                gate_status = _normalize(item.get("gate_status")) or "unknown_gate"
                judge_status = _normalize(item.get("judge_status")) or "unknown_judge"
                next_action = _normalize(item.get("next_action")) or "unknown_action"
                lines.append(
                    f"  - {iteration_id}: gate={gate_status} judge={judge_status} next_action={next_action}"
                )
    lines.append(
        f"- manual_launch_preflight_status: {summary.get('latest_manual_launch_preflight_status')}"
    )
    lines.append(
        f"- manual_launch_ready_for_remote_launch: "
        f"{str(summary.get('manual_launch_ready_for_remote_launch')).lower()}"
    )
    lines.append(
        f"- manual_launch_next_required_approval: {summary.get('manual_launch_next_required_approval')}"
    )
    manual_launch_preflight_path = summary.get("manual_launch_preflight_path")
    if _normalize(manual_launch_preflight_path):
        lines.append(f"- manual_launch_preflight_path: {manual_launch_preflight_path}")
    manual_launch_blockers = summary.get("manual_launch_blockers") or []
    if manual_launch_blockers:
        lines.append("- manual_launch_blockers:")
        lines.extend(f"  - {item}" for item in manual_launch_blockers)
    else:
        lines.append("- manual_launch_blockers: none")

    continuation_observations = summary.get("continuation_observations") or {}
    lines.extend(["", "## Continuation Observations"])
    lines.append(f"- count: {int(continuation_observations.get('count') or 0)}")
    latest_stage = continuation_observations.get("latest_stage")
    if _normalize(latest_stage):
        lines.append(f"- latest_stage: {latest_stage}")
    latest_status = continuation_observations.get("latest_status")
    if _normalize(latest_status):
        lines.append(f"- latest_status: {latest_status}")
    latest_source_task_id = continuation_observations.get("latest_source_task_id")
    if _normalize(latest_source_task_id):
        lines.append(f"- latest_source_task_id: {latest_source_task_id}")
    latest_reasons = continuation_observations.get("latest_reasons") or []
    if latest_reasons:
        lines.append("- latest_reasons:")
        lines.extend(f"  - {item}" for item in latest_reasons)
    latest_payload = continuation_observations.get("latest_payload") or {}
    if isinstance(latest_payload, dict) and latest_payload:
        lines.append(f"- latest_payload: {json.dumps(latest_payload, ensure_ascii=False, sort_keys=True)}")
    stages = continuation_observations.get("stages") or []
    actors = continuation_observations.get("actors") or []
    paths = continuation_observations.get("paths") or []
    if stages:
        lines.append("- stages:")
        lines.extend(f"  - {item}" for item in stages)
    if actors:
        lines.append("- actors:")
        lines.extend(f"  - {item}" for item in actors)
    if paths:
        lines.append("- paths:")
        lines.extend(f"  - {item}" for item in paths)

    claim_limits = summary.get("claim_limits") or []
    if claim_limits:
        lines.extend(["", "## Claim Limits"])
        lines.extend(f"- {item}" for item in claim_limits)

    lines.extend(["", "## Preflight and Judge"])
    lines.append(f"- preflight_statuses: {json.dumps(summary.get('preflight_statuses') or [])}")
    lines.append(f"- latest_preflight_status: {summary.get('latest_preflight_status')}")
    lines.append(f"- judge_next_actions: {json.dumps(summary.get('judge_next_actions') or [])}")
    lines.append(f"- latest_judge_next_action: {summary.get('latest_judge_next_action')}")
    lines.append(f"- latest_judge_stop_reason: {summary.get('latest_judge_stop_reason')}")
    lines.append(f"- latest_judge_decision: {json.dumps(summary.get('latest_judge_decision') or {})}")
    lines.append(f"- latest_next_step: {summary.get('latest_next_step')}")
    lines.append(f"- remote_boundary: {summary.get('remote_boundary')}")

    return "\n".join(lines).rstrip() + "\n"


def write_morning_brief(*, mission_id: str) -> Dict[str, Any]:
    summary = summarize_local_execution_activity(mission_id=mission_id)
    markdown = render_morning_brief(summary)
    md_path = continuation_night_auto_morning_brief_path(mission_id, suffix="md")
    json_path = continuation_night_auto_morning_brief_path(mission_id, suffix="json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    created = True
    replayed = False
    if md_path.exists() and json_path.exists():
        if md_path.read_text(encoding="utf-8") == markdown and json_path.read_text(encoding="utf-8") == json_payload:
            created = False
            replayed = True

    if not replayed:
        md_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(json_payload, encoding="utf-8")

    return {
        "status": summary["status"],
        "mission_id": mission_id,
        "event_count": summary["event_count"],
        "remote_boundary": summary.get("remote_boundary", "not_reached"),
        "remote_boundary_reached": summary.get("remote_boundary_reached", False),
        "created": created,
        "replayed": replayed,
        "brief_path": str(md_path),
        "summary_path": str(json_path),
        "summary": summary,
    }


__all__ = [
    "collect_local_execution_activity_events",
    "render_morning_brief",
    "summarize_local_execution_activity",
    "write_morning_brief",
]

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .bounded_validation_bucket import ARTIFACT_READINESS_REPORT_NAME
from .interpreter import interpret_result_dir_stub
from .planner import plan_deterministic_stub
from .line_progression import assess_line_progression, _coerce_bounded_validation_readiness
from .source_repair import build_source_repair_plan
from .writer import stage_plan_stub
from .paths import continuation_audit_events_path, continuation_root
from .gate import (
    review_draft_payload_stub,
    review_draft_text_stub,
    review_staging_manifest_stub,
)


_DISPATCH_TEXT_RULES = {"direct_dispatch", "scheduler_submit", "auto_promotion"}
_SOURCE_REPAIR_KIND_HINTS = {
    "missing_capability",
    "source_code_required",
    "request_codex_review",
    "needs_bounded_smoke_runner",
    "codex_debug_request",
}
_SOURCE_REPAIR_REASON_HINTS = {
    "needs_bounded_smoke_runner",
    "missing_capability",
    "request_codex_review",
    "source_code_required",
    "codex_debug_request",
}


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_json_payload(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _action_requests_from_payload(payload: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("action_requests")
    if raw is None:
        raw = payload.get("requests")
    if raw is None:
        raw = payload.get("action_request")
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [dict(raw)]
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _needs_source_repair_gating(
    interpretation,
    decision_summary: Dict[str, Any],
    action_requests: List[Dict[str, Any]],
) -> bool:
    reason = _normalize_text(decision_summary.get("reason"))
    category = _normalize_text(interpretation.category)
    if category in _SOURCE_REPAIR_REASON_HINTS:
        return True
    if reason in _SOURCE_REPAIR_REASON_HINTS:
        return True
    for request in action_requests:
        kind = _normalize_text(request.get("kind")).lower()
        if kind in _SOURCE_REPAIR_KIND_HINTS:
            return True
    return False


def _iter_audit_events() -> List[Dict[str, Any]]:
    path = continuation_audit_events_path()
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _blocked_by_terminal_lifecycle(source_task_id: str, force: bool = False) -> bool:
    if force:
        return False
    for event in _iter_audit_events():
        if event.get("source_task_id") != source_task_id:
            continue
        if event.get("action") in {"reject", "revoke"}:
            return True
    return False


def _parse_front_matter(text: str) -> Dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    try:
        _, front_matter, _ = text.split("---", 2)
    except ValueError:
        return {}
    payload = yaml.safe_load(front_matter)
    return payload if isinstance(payload, dict) else {}


def _run_scheduler_dry_run_checks(
    stage_root: Path,
    manifest: Dict[str, Any],
    draft_payloads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    checks: List[Dict[str, str]] = []
    violations: List[Dict[str, str]] = []
    writes_allowed = list(manifest.get("writes_allowed", []))
    forbidden_roots = list(manifest.get("writes_forbidden", []))

    if any(
        "tasks/inbox/" in str(item) and "_proposed" not in str(item)
        for item in writes_allowed
    ):
        violations.append(
            {
                "rule_id": "scheduler_active_inbox_path",
                "matched": "active tasks/inbox write target detected",
                "severity": "error",
            }
        )
    if not any("tasks/inbox/" in str(item) and "_proposed" in str(item) for item in writes_allowed):
        checks.append({"name": "scheduler_visibility_guard", "status": "failed"})
        violations.append(
            {
                "rule_id": "scheduler_missing_proposed_root",
                "matched": "tasks/inbox/_proposed not explicitly allowed",
                "severity": "error",
            }
        )
    else:
        checks.append({"name": "scheduler_visibility_guard", "status": "passed"})

    if not any("tasks/inbox/" in str(item) for item in forbidden_roots):
        violations.append(
            {
                "rule_id": "scheduler_missing_forbidden_active_inbox",
                "matched": "manifest writes_forbidden misses active inbox root",
                "severity": "error",
            }
        )
        checks.append({"name": "scheduler_forbidden_roots", "status": "failed"})
    else:
        checks.append({"name": "scheduler_forbidden_roots", "status": "passed"})

    for payload in draft_payloads:
        draft_id = payload.get("draft_id", "")
        markdown_path = stage_root / f"{draft_id}.md"
        if markdown_path.exists():
            draft_text = markdown_path.read_text(encoding="utf-8")
            text_checks = review_draft_text_stub(
                proposed_task_id=draft_id,
                source_task_id=payload.get("source_task_id", ""),
                source_result_hash=payload.get("source_result_hash", ""),
                draft_text=draft_text,
            )
            if text_checks.gate_status == "failed":
                for violation in text_checks.violations:
                    if violation.get("rule_id") in _DISPATCH_TEXT_RULES:
                        violations.append(
                            {
                                "rule_id": f"scheduler_blocked_by_text_{violation.get('rule_id')}",
                                "matched": f"{draft_id}:{violation.get('matched', '')}",
                                "severity": "error",
                            }
                        )
        checks.append(
            {
                "name": f"scheduler_text_guard::{payload.get('draft_id', 'unknown')}",
                "status": "failed" if violations else "passed",
            }
        )

    return {
        "check_name": "scheduler_dry_run_no_dispatch",
        "checks": checks,
        "violations": violations,
        "status": "failed" if violations else "manual_review",
    }


def run_local_continuation_pipeline(
    source_task_id: str,
    result_dir: Path,
    *,
    force_reproposal: bool = False,
    write_report: bool = True,
) -> Dict[str, Any]:
    blocked_reasons: List[str] = []
    proposal_count = 0
    draft_count = 0
    checks: List[Dict[str, Any]] = []

    if _blocked_by_terminal_lifecycle(source_task_id, force=force_reproposal):
        blocked_reasons.append("blocked_by_terminal_lifecycle_action")
        result: Dict[str, Any] = {
            "schema_version": 1,
            "pipeline_id": f"{source_task_id}__local_pipeline",
            "source_task_id": source_task_id,
            "status": "blocked",
            "selected": [],
            "blocked_reasons": blocked_reasons,
            "written": False,
            "checks": checks,
            "proposal_count": proposal_count,
            "draft_count": draft_count,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        report_path = None
        if write_report:
            report_path = continuation_root() / "reports" / f"{source_task_id}_pipeline_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result["report_path"] = str(report_path)
        return result

    interpretation = interpret_result_dir_stub(source_task_id, result_dir)
    checks.append({"name": "interpret", "status": "passed"})

    decision_summary = _read_json_payload(result_dir / "decision_summary.json")
    if not isinstance(decision_summary, dict):
        decision_summary = {}

    action_requests = _action_requests_from_payload(decision_summary)
    if not action_requests:
        action_requests = _action_requests_from_payload(_read_json_payload(result_dir / "action_requests.json"))

    validation_readiness_reasons: List[str] = []
    bounded_validation_readiness = _coerce_bounded_validation_readiness(
        _read_json_payload(result_dir / ARTIFACT_READINESS_REPORT_NAME),
        reasons=validation_readiness_reasons,
    )

    source_repair_blocked = False
    next_step_readiness: Dict[str, Any] | None = None
    source_repair_plan: Dict[str, Any] | None = None
    line_progression: Dict[str, Any] = {}

    if _needs_source_repair_gating(interpretation, decision_summary, action_requests):
        source_repair_plan = build_source_repair_plan(
            result_dir,
            source_task_id=source_task_id,
        )
        repair_result = _read_json_payload(result_dir / "repair_result.yaml")
        rerun_intent = _read_json_payload(result_dir / "rerun_intent.yaml")
        line_progression = assess_line_progression(
            source_task_id=source_task_id,
            decision_summary={
                "status": _normalize_text(decision_summary.get("status"))
                or ("partial" if interpretation.blocks_execution or interpretation.needs_user_decision else "completed" if interpretation.execution_success else "failed"),
                "reason": _normalize_text(decision_summary.get("reason")) or str(interpretation.category),
                "next_action": _normalize_text(decision_summary.get("next_action")),
            },
            action_requests=action_requests,
            source_repair_plan=source_repair_plan,
            repair_result=repair_result,
            rerun_intent=rerun_intent,
            validation_readiness_report=result_dir / ARTIFACT_READINESS_REPORT_NAME,
        )
        next_step_readiness = dict(line_progression.get("next_step_readiness", {}))
        if not bool(line_progression.get("continue_allowed")):
            source_repair_blocked = True
            blocked_reasons.append("line_progression_blocked")
            blocked_reasons.append(
                f"line_progression_reasons={','.join(line_progression.get('reasons', []))}"
            )

    plan = plan_deterministic_stub(interpretation)
    checks.append({"name": "plan", "status": "passed", "no_safe_continuation_reason": plan.no_safe_continuation_reason})

    proposal_payload: Dict[str, Any] = {"proposal_count": 0}
    manifest_payload: Dict[str, Any] = {}
    stage_root = Path()
    stage: Dict[str, Any] | None = None
    draft_payloads: List[Dict[str, Any]] = []

    if source_repair_blocked:
        checks.append(
            {
                "name": "line_progression",
                "status": "blocked",
                "next_step": "ask_human",
                "next_step_readiness": next_step_readiness or {},
            }
        )
        checks.append(
            {
                "name": "pipeline_blocked",
                "status": "blocked",
                "reasons": next_step_readiness.get("blockers", []) if next_step_readiness else [],
            }
        )
    else:
        stage = stage_plan_stub(plan)
        checks.extend(stage.get("proposal", []))
        checks.append({"name": "stage_plan", "status": "passed" if stage.get("staged") is not None else "passed"})
        draft_count = stage.get("draft_count", 0)
        proposal_count = stage.get("proposal_count", 0)
        stage_root = Path(stage["path"])

        manifest = review_staging_manifest_stub(Path(stage["manifest_path"]))
        checks.append(
            {
                "name": "staging_manifest_gate",
                "status": manifest.gate_status,
                "violations": manifest.violations,
            }
        )
        if manifest.gate_status == "failed":
            blocked_reasons.append("staging_manifest_gate_failed")

        draft_payload_paths = [
            path
            for path in stage_root.glob("*.json")
            if path.name
            not in {
                "manifest.json",
                "proposal_dry_run_manifest.json",
                "proposal.json",
                "gate_report.json",
            }
        ]
        for draft_path in sorted(draft_payload_paths):
            draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
            draft_payloads.append(draft_payload)
            draft_gate = review_draft_payload_stub(draft_payload=draft_payload)
            checks.append(
                {
                    "name": f"draft_payload_gate:{draft_payload.get('draft_id', draft_path.name)}",
                    "status": draft_gate.gate_status,
                    "violations": draft_gate.violations,
                }
            )
            if draft_gate.gate_status == "failed":
                blocked_reasons.append(f"draft_payload_gate_failed:{draft_path.name}")

        proposal_payload = json.loads(Path(stage["proposal_json_path"]).read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "proposal_manifest_fields",
                "status": "passed",
                "required": [
                    "proposal_id",
                    "source_task_id",
                    "source_result_hash",
                    "plan_id",
                    "proposal_count",
                ],
            }
        )
        manifest_payload = json.loads(Path(stage["manifest_path"]).read_text(encoding="utf-8"))
        scheduler_gate = _run_scheduler_dry_run_checks(stage_root, manifest_payload, draft_payloads)
        checks.append(
            {
                "name": scheduler_gate["check_name"],
                "status": scheduler_gate["status"],
                "violations": scheduler_gate["violations"],
            }
        )
        if scheduler_gate["status"] == "failed":
            blocked_reasons.append("scheduler_dry_run_blocked")

        stage = stage

    selected = (
        [
            step.kind.value if hasattr(step, "kind") else str(step)
            for step in plan.proposed_next_steps
        ]
        if not source_repair_blocked
        else []
    )

    pipeline_status = "selected" if not blocked_reasons and plan.proposed_next_steps and not source_repair_blocked else "no_action"
    if blocked_reasons:
        pipeline_status = "blocked"

    result = {
        "schema_version": 1,
        "pipeline_id": f"{source_task_id}__local_pipeline",
        "source_task_id": source_task_id,
        "source_result_hash": interpretation.source_result_hash,
        "source_task_category": interpretation.category,
        "idempotency_key": interpretation.idempotency_key,
        "status": pipeline_status,
        "selected": selected,
        "blocked_reasons": blocked_reasons,
        "proposal_count": proposal_count,
        "draft_count": draft_count,
        "stage": stage,
        "checks": checks,
        "written": bool(stage and stage.get("staged")),
        "manifest_path": stage.get("manifest_path") if stage else None,
        "proposal_json_path": stage.get("proposal_json_path") if stage else None,
        "proposal_md_path": stage.get("proposal_markdown_path") if stage else None,
        "manifest": manifest_payload,
        "proposal": proposal_payload,
        "next_step_readiness": next_step_readiness,
        "line_progression": line_progression,
        "bounded_validation_readiness": (
            {"status": "unavailable", "reasons": sorted(set(validation_readiness_reasons))}
            if validation_readiness_reasons
            else bounded_validation_readiness
        ),
        "source_repair_plan": source_repair_plan,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if write_report:
        report_root = continuation_root() / "reports"
        report_root.mkdir(parents=True, exist_ok=True)
        report_path = report_root / f"{source_task_id}_pipeline_report.json"
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report_path"] = str(report_path)

    return result


__all__ = [
    "run_local_continuation_pipeline",
    "_parse_front_matter",
]

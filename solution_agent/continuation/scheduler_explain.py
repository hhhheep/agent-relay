from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .knowledge_to_proposal import _coerce_nested_readiness_payload
from .paths import continuation_task_inbox_root
from .paths import (
    continuation_night_auto_pending_root,
    continuation_night_auto_scheduler_dry_run_report_path,
)


def _normalize_selector_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    text = str(value).strip()
    return text


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []


def _task_selector_value(front_matter: Dict[str, Any], key: str) -> str:
    aliases = {
        "line": ("line", "task_line"),
        "batch": ("batch", "batch_id", "task_batch"),
        "dag_root": ("dag_root", "dag-root", "root_task", "task_root", "root", "dag"),
        "prefix": ("prefix", "task_prefix", "task-id-prefix"),
    }
    for name in aliases.get(key, (key,)):
        value = _normalize_selector_value(front_matter.get(name))
        if value:
            return value
    tags = front_matter.get("tags")
    if isinstance(tags, dict):
        for name in aliases.get(key, (key,)):
            value = _normalize_selector_value(tags.get(name))
            if value:
                return value
    return ""


def _scope_mismatch_reason(value: str, expected: str, key: str) -> Optional[str]:
    if not expected:
        return None
    if value != expected:
        return f"{key}_scope_mismatch"
    return None


def parse_task_front_matter(text: str) -> Dict[str, Any]:
    """Parse YAML front matter from a markdown task safely.

    Returns an empty dictionary if front matter is missing or malformed.
    Supports BOM-prefixed files (`utf-8-sig`).
    """

    normalized = text.lstrip("\ufeff")
    if not normalized.startswith("---"):
        return {}
    if normalized.count("---") < 2:
        return {}
    parts = normalized.split("---", 2)
    if len(parts) < 3:
        return {}
    raw_front = parts[1]
    try:
        payload = yaml.safe_load(raw_front)
    except yaml.YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def _detect_dispatch_language(text: str) -> Optional[str]:
    lowered = (text or "").lower()
    if not lowered:
        return None
    if "dispatch" not in lowered:
        return None

    negated = [
        "do not dispatch",
        "don't dispatch",
        "do not attempt to dispatch",
        "without dispatching",
        "no dispatch",
        "do not auto-dispatch",
        "do not auto dispatch",
        "avoid dispatch",
        "avoid dispatching",
    ]
    if any(phrase in lowered for phrase in negated):
        return "dispatch_language_negated"

    request_markers = (
        "please dispatch",
        "request dispatch",
        "dispatch this",
        "dispatch the task",
        "dispatch requested",
        "manual dispatch",
    )
    if any(marker in lowered for marker in request_markers):
        return "dispatch_language_detected"

    # Generic fallback when dispatch-like language appears and we do not know intent.
    return "dispatch_language_detected"


def _scope_checks(
    front: Dict[str, Any],
    requested_scope: Dict[str, str],
    checks: List[Dict[str, Any]],
    reasons: List[str],
) -> None:
    line = _task_selector_value(front, "line")
    batch = _task_selector_value(front, "batch")
    dag_root = _task_selector_value(front, "dag_root")
    wanted_line = requested_scope.get("line", "")
    wanted_batch = requested_scope.get("batch", "")
    wanted_dag_root = requested_scope.get("dag_root", "")
    wanted_prefix = requested_scope.get("prefix", "")

    wanted_checks = {
        "line": wanted_line,
        "batch": wanted_batch,
        "dag_root": wanted_dag_root,
    }
    actuals = {
        "line": line,
        "batch": batch,
        "dag_root": dag_root,
    }

    for key, wanted in wanted_checks.items():
        reason = _scope_mismatch_reason(actuals[key], wanted, key)
        status = "passed" if reason is None else "failed"
        check = {"id": f"scope_{key}", "status": status, "requested": wanted, "actual": actuals[key]}
        if reason:
            check["reason"] = reason
            reasons.append(reason)
        checks.append(check)

    task_prefix = _task_selector_value(front, "prefix")
    task_task_id = _normalize_selector_value(front.get("task_id", ""))
    if wanted_prefix:
        actual_prefix = task_prefix or task_task_id[: len(wanted_prefix)]
        reason = None
        if not (task_task_id.startswith(wanted_prefix) or task_prefix == wanted_prefix):
            reason = "prefix_scope_mismatch"
        status = "passed" if reason is None else "failed"
        check = {"id": "scope_prefix", "status": status, "requested": wanted_prefix, "actual": task_prefix}
        if reason:
            check["reason"] = reason
            reasons.append(reason)
        checks.append(check)



def _append_check(checks: List[Dict[str, Any]], *, check_id: str, status: str, **payload: Any) -> None:
    check = {"id": check_id, "status": status}
    check.update(payload)
    checks.append(check)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json_payload(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = _normalize_selector_value(value).lower()
    if text in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _coerce_claim_limits(raw: Any) -> List[str]:
    if isinstance(raw, str):
        normalized = _normalize_selector_value(raw)
        return [normalized] if normalized else []
    if isinstance(raw, (list, tuple, set)):
        values = [_normalize_selector_value(item) for item in raw]
        return [item for item in values if item]
    return []


def _append_report_checks(
    checks: List[Dict[str, Any]],
    *,
    check_id: str,
    status: str,
    **payload: Any,
) -> None:
    _append_check(checks, check_id=check_id, status=status, **payload)


def _scheduler_blockers_from_claim_limits(claim_limits: List[str]) -> List[str]:
    normalized = {_normalize_selector_value(item) for item in claim_limits}
    blockers = []
    if "no_scheduler_dispatch" in normalized:
        blockers.append("claim_limit_no_scheduler_dispatch")
    if "no_active_inbox_write" in normalized:
        blockers.append("claim_limit_no_active_inbox_write")
    if "no_registry_mutation" in normalized:
        blockers.append("claim_limit_no_registry_mutation")
    if "no_remote_api_call" in normalized:
        blockers.append("claim_limit_no_remote_api_call")
    if "no_dataset_or_checkpoint_mutation" in normalized:
        blockers.append("claim_limit_no_dataset_or_checkpoint_mutation")
    if "claim_upgrade_via_human_only" in normalized:
        blockers.append("claim_limit_blocked_by_human_only_upgrade")
    return blockers


def _evaluate_pending_payload(
    payload: Dict[str, Any],
    requested_scope: Dict[str, str],
) -> Tuple[Dict[str, Any], bool]:
    reasons: List[str] = []
    candidate_id = _normalize_selector_value(payload.get("candidate_id"))
    proposal_run_id = _normalize_selector_value(payload.get("proposal_run_id"))
    mission_id = _normalize_selector_value(payload.get("mission_id"))
    scheduler_visible = _coerce_bool(payload.get("scheduler_visible"), default=False)
    side_effects = payload.get("side_effects") if isinstance(payload.get("side_effects"), dict) else {}
    claim_limits = _coerce_claim_limits(payload.get("claim_limits"))

    if not mission_id:
        reasons.append("pending_artifact_missing_mission_id")
    elif mission_id != requested_scope["mission_id"]:
        reasons.append("mission_id_mismatch")

    if candidate_id != requested_scope.get("candidate_id", "") and requested_scope.get("candidate_id"):
        reasons.append("candidate_id_scope_mismatch")

    if proposal_run_id != requested_scope.get("proposal_run_id", "") and requested_scope.get("proposal_run_id"):
        reasons.append("proposal_run_id_scope_mismatch")

    if scheduler_visible is not False:
        reasons.append("scheduler_visible_false_not_allowed")

    if not isinstance(side_effects, dict):
        reasons.append("pending_artifact_missing_side_effects")
    else:
        for key in (
            "scheduler_dispatch",
            "active_inbox_write",
            "registry_mutation",
            "remote_api_call",
            "dataset_or_checkpoint_mutation",
        ):
            if _coerce_bool(side_effects.get(key), default=False) is True:
                reasons.append(f"side_effects_blocker:{key}")

    if not claim_limits:
        reasons.append("pending_artifact_missing_claim_limits")
    reasons.extend(_scheduler_blockers_from_claim_limits(claim_limits))

    source_decision = payload.get("source_decision")
    if not isinstance(source_decision, dict):
        reasons.append("pending_artifact_missing_source_decision")
    next_step_readiness = (
        _coerce_nested_readiness_payload(source_decision.get("next_step_readiness"))
        if isinstance(source_decision, dict)
        else {}
    )
    bounded_validation_readiness = (
        dict(next_step_readiness.get("bounded_validation"))
        if isinstance(next_step_readiness.get("bounded_validation"), dict)
        else {}
    )

    report_entry = {
        "candidate_id": candidate_id,
        "proposal_run_id": proposal_run_id,
        "mission_id": mission_id,
        "status": "blocked" if reasons else "eligible",
        "reasons": sorted(set(reasons)),
        "scheduler_visible": bool(scheduler_visible),
        "claim_limits": claim_limits,
        "side_effects": {
            "scheduler_dispatch": bool(_normalize_selector_value(side_effects.get("scheduler_dispatch")).lower() == "true")
            if side_effects is not None and isinstance(side_effects, dict)
            else False,
            "registry_mutation": bool(_normalize_selector_value(side_effects.get("registry_mutation")).lower() == "true")
            if side_effects is not None and isinstance(side_effects, dict)
            else False,
            "remote_api_call": bool(_normalize_selector_value(side_effects.get("remote_api_call")).lower() == "true")
            if side_effects is not None and isinstance(side_effects, dict)
            else False,
            "dataset_or_checkpoint_mutation": bool(
                _normalize_selector_value(side_effects.get("dataset_or_checkpoint_mutation")).lower() == "true"
            )
            if side_effects is not None and isinstance(side_effects, dict)
            else False,
        },
    }
    if next_step_readiness:
        report_entry["next_step_readiness"] = next_step_readiness
    if bounded_validation_readiness:
        report_entry["bounded_validation_readiness"] = bounded_validation_readiness
    return report_entry, not bool(reasons)


def explain_night_auto_pending_scheduler_dry_run(
    *,
    mission_id: str | None = None,
    proposal_run_id: str | None = None,
    candidate_id: str | None = None,
) -> Dict[str, Any]:
    requested_scope = {
        "mission_id": _normalize_selector_value(mission_id),
        "proposal_run_id": _normalize_selector_value(proposal_run_id),
        "candidate_id": _normalize_selector_value(candidate_id),
    }

    payload: Dict[str, Any] = {
        "mission_id": requested_scope["mission_id"],
        "selector": {
            "mission_id": requested_scope["mission_id"],
            "proposal_run_id": requested_scope["proposal_run_id"],
            "candidate_id": requested_scope["candidate_id"],
        },
        "eligible": False,
        "counts": {
            "candidate_total": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "matched_count": 0,
        },
        "reasons": [],
        "candidate_reports": [],
        "side_effects": {
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "remote_api_call": False,
            "dataset_or_checkpoint_mutation": False,
            "active_inbox_write": False,
        },
        "checks": [],
    }

    reasons: List[str] = []
    checks = payload["checks"]
    candidate_reports: List[Dict[str, Any]] = []

    _append_report_checks(checks, check_id="night_scheduler_dry_run", status="passed")
    if not requested_scope["mission_id"]:
        reasons.append("missing_scope_mission_id")
        _append_report_checks(checks, check_id="mission_scope", status="failed", reason="missing_scope_mission_id")
        payload["reasons"] = reasons
        payload["status"] = "blocked"
        payload["checks"] = checks
        return payload

    _append_report_checks(checks, check_id="mission_scope", status="passed", mission_id=requested_scope["mission_id"])
    pending_root = continuation_night_auto_pending_root(requested_scope["mission_id"])
    if not pending_root.exists():
        reasons.append("no_pending_artifacts_found")
        payload["reasons"] = reasons
        payload["status"] = "blocked"
        _append_report_checks(checks, check_id="pending_root", status="failed", reason="missing_pending_root")
        payload["checks"] = checks
        return payload

    _append_report_checks(checks, check_id="pending_root", status="passed", path=str(pending_root))

    for artifact_path in sorted(pending_root.rglob("*.json")):
        artifact = _read_json_payload(artifact_path)
        if not artifact:
            continue
        payload_reasons: List[str] = []
        artifact_reason_scope: List[str] = []
        candidate_report, eligible = _evaluate_pending_payload(artifact, requested_scope)

        if requested_scope["proposal_run_id"] and _normalize_selector_value(artifact.get("proposal_run_id")) != requested_scope["proposal_run_id"]:
            artifact_reason_scope.append("proposal_run_scope_filtered_out")
        if requested_scope["candidate_id"] and _normalize_selector_value(artifact.get("candidate_id")) != requested_scope["candidate_id"]:
            artifact_reason_scope.append("candidate_scope_filtered_out")
        if artifact_reason_scope:
            continue

        if requested_scope["candidate_id"] and not candidate_report["candidate_id"]:
            payload_reasons.append("requested_candidate_id_missing_in_artifact")
        if requested_scope["proposal_run_id"] and not candidate_report["proposal_run_id"]:
            payload_reasons.append("requested_proposal_run_id_missing_in_artifact")

        if requested_scope["candidate_id"] and not candidate_report["candidate_id"]:
            payload_reasons.append("requested_candidate_id_missing_in_artifact")
        if requested_scope["proposal_run_id"] and not candidate_report["proposal_run_id"]:
            payload_reasons.append("requested_proposal_run_id_missing_in_artifact")

        if payload_reasons:
            candidate_report["reasons"] = sorted(set(candidate_report["reasons"] + payload_reasons))
            candidate_report["status"] = "blocked"
            eligible = False

        candidate_report["artifact_path"] = str(artifact_path.resolve())
        candidate_reports.append(candidate_report)

    payload["counts"]["candidate_total"] = len(candidate_reports)
    payload["counts"]["eligible_count"] = len([entry for entry in candidate_reports if entry.get("status") == "eligible"])
    payload["counts"]["blocked_count"] = len([entry for entry in candidate_reports if entry.get("status") != "eligible"])
    payload["counts"]["matched_count"] = payload["counts"]["candidate_total"]
    payload["candidate_reports"] = candidate_reports
    payload["eligible"] = payload["counts"]["eligible_count"] > 0
    payload["status"] = "eligible" if payload["eligible"] else "blocked"

    if requested_scope["proposal_run_id"] and payload["counts"]["matched_count"] == 0:
        reasons.append("requested_proposal_run_not_found")
        _append_report_checks(checks, check_id="proposal_run_scope", status="failed")
    if requested_scope["candidate_id"] and payload["counts"]["matched_count"] == 0:
        reasons.append("requested_candidate_not_found")
        _append_report_checks(checks, check_id="candidate_scope", status="failed")

    if payload["counts"]["candidate_total"] == 0:
        reasons.append("no_matching_candidates")

    payload["reasons"] = sorted(
        set(
            reasons
            + [reason for entry in candidate_reports for reason in entry["reasons"] if reason != "scheduler_visible_false_not_allowed"]
        )
    )
    payload["checks"] = checks
    report_payload = {
        "report_schema": "continuation_night_auto_scheduler_explain_v1",
        "generated_at": "1970-01-01T00:00:00Z",
        **payload,
    }
    report_path = continuation_night_auto_scheduler_dry_run_report_path(requested_scope["mission_id"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = _canonical_json(report_payload) + "\n"
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else None
    if existing == report_text:
        payload["replayed"] = True
        payload["created"] = False
        payload["report_path"] = str(report_path)
        payload["action"] = "replayed"
        return payload
    report_path.write_text(report_text, encoding="utf-8")
    payload["replayed"] = False
    payload["created"] = True
    payload["report_path"] = str(report_path)
    payload["action"] = "created"
    return payload


def explain_active_inbox_task(
    task_path: Path,
    *,
    requested_worker: str | None = None,
    scope: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Explain one active inbox task without any side effect.

    Returns a deterministic report describing visibility, eligibility, selectors, and
    known blocking reasons. This helper never dispatches, mutates registry, mutates
    files, or launches remote work.
    """

    path = Path(task_path)
    requested_scope = {str(key): _normalize_selector_value(value) for key, value in (scope or {}).items()}
    if requested_worker is not None:
        requested_scope["worker"] = _normalize_selector_value(requested_worker)

    payload: Dict[str, Any] = {
        "task_id": "",
        "visible_in_active_inbox": False,
        "eligible": False,
        "reasons": [],
        "selector": {
            "worker": requested_scope.get("worker", ""),
            "line": requested_scope.get("line", ""),
            "batch": requested_scope.get("batch", ""),
            "dag_root": requested_scope.get("dag_root", ""),
            "prefix": requested_scope.get("prefix", ""),
            "resource_lane": "",
            "resource_hint": "",
            "request": {
                "worker": requested_scope.get("worker", ""),
                "line": requested_scope.get("line", ""),
                "batch": requested_scope.get("batch", ""),
                "dag_root": requested_scope.get("dag_root", ""),
                "prefix": requested_scope.get("prefix", ""),
            },
            "task": {
                "worker": "",
                "line": "",
                "batch": "",
                "dag_root": "",
                "prefix": "",
            },
        },
        "continuation_metadata": {
            "continuation": {},
            "front_matter": {},
        },
        "side_effects": {
            "dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
        },
        "checks": [],
    }

    reasons: List[str] = payload["reasons"]
    checks: List[Dict[str, Any]] = payload["checks"]

    # No subprocess execution, no scheduler call, no registry mutation.
    _append_check(checks, check_id="side_effects", status="passed")

    try:
        if not path.exists():
            _append_check(checks, check_id="active_inbox_visibility", status="failed", reason="task_path_not_found")
            reasons.append("active_inbox_not_found")
            payload["reasons"] = sorted(set(reasons))
            return payload

        inbox_root = continuation_task_inbox_root().resolve()
        target_root = path.resolve().parent
        payload["visible_in_active_inbox"] = target_root == inbox_root
        if not payload["visible_in_active_inbox"]:
            _append_check(
                checks,
                check_id="active_inbox_visibility",
                status="failed",
                reason="not_in_active_inbox_root",
                path=str(path),
                active_inbox_root=str(inbox_root),
            )
            reasons.append("not_active_inbox_task")
            # Still parse for stable read-only diagnostics.
        else:
            _append_check(checks, check_id="active_inbox_visibility", status="passed", path=str(path))

        text = path.read_text(encoding="utf-8-sig")
        front = parse_task_front_matter(text)
        body = ""
        normalized = text.lstrip("\ufeff")
        if normalized.startswith("---") and normalized.count("---") >= 2:
            parts = normalized.split("---", 2)
            if len(parts) == 3:
                body = parts[2]

        if not front:
            _append_check(checks, check_id="front_matter", status="failed", reason="missing_or_malformed_front_matter")
            payload["task_id"] = _normalize_selector_value(path.stem)
            payload["selector"]["task"]["worker"] = ""
            payload["continuation_metadata"]["front_matter"] = {}
            if normalized.startswith("---"):
                reasons.append("front_matter_malformed")
            else:
                reasons.append("front_matter_missing")
            reasons.append("manual_review_required")
            payload["checks"] = checks
            payload["reasons"] = sorted(set(reasons))
            return payload

        payload["task_id"] = _normalize_selector_value(front.get("task_id", path.stem))

        continuation = front.get("continuation") if isinstance(front.get("continuation"), dict) else {}
        payload["continuation_metadata"] = {
            "front_matter": front,
            "continuation": continuation,
        }

        declared_worker = _normalize_selector_value(
            front.get("worker")
            or front.get("required_worker")
            or continuation.get("worker")
            or ""
        )

        allowed_workers = {
            worker
            for worker in _as_str_list(front.get("allowed_workers"))
            if worker
        } or {declared_worker or "codex"}

        payload["selector"]["task"]["worker"] = declared_worker
        payload["selector"]["task"]["line"] = _task_selector_value(front, "line")
        payload["selector"]["task"]["batch"] = _task_selector_value(front, "batch")
        payload["selector"]["task"]["dag_root"] = _task_selector_value(front, "dag_root")
        payload["selector"]["task"]["prefix"] = _task_selector_value(front, "prefix")

        resource_hint = _normalize_selector_value(front.get("resource_hint") or front.get("resource") or "")
        resource_hint = resource_hint.lower() if resource_hint else ""
        resource_lane = _normalize_selector_value(front.get("resource_lane") or ("gpu" if resource_hint == "gpu" else "cpu" if resource_hint else ""))
        payload["selector"]["resource_hint"] = resource_hint or "any"
        payload["selector"]["resource_lane"] = resource_lane or ("gpu" if resource_hint == "gpu" else "cpu")

        _append_check(
            checks,
            check_id="continuation_metadata_present",
            status="passed" if continuation else "failed",
            present=bool(continuation),
        )

        if continuation:
            continuation_state = _normalize_selector_value(
                continuation.get("activation_state")
                or continuation.get("state")
                or continuation.get("lifecycle_state")
            ).lower()
            if continuation_state in {"revoked", "superseded", "logically_revoked"}:
                reason = f"continuation_{continuation_state}"
                reasons.append(reason)
                _append_check(checks, check_id="continuation_state", status="failed", reason=reason, state=continuation_state)
            else:
                _append_check(checks, check_id="continuation_state", status="passed", state=continuation_state)

            dispatch_allowed = continuation.get("dispatch_allowed", False)
            if isinstance(dispatch_allowed, bool):
                if not dispatch_allowed:
                    reasons.append("dispatch_allowed_false")
                    _append_check(checks, check_id="dispatch_allowed", status="failed", dispatch_allowed=dispatch_allowed)
                else:
                    _append_check(checks, check_id="dispatch_allowed", status="passed", dispatch_allowed=True)
            else:
                reasons.append("dispatch_allowed_false")
                _append_check(
                    checks,
                    check_id="dispatch_allowed",
                    status="failed",
                    reason="dispatch_allowed_missing",
                    dispatch_allowed=dispatch_allowed,
                )

            scheduler_admission = continuation.get("scheduler_admission")
            scheduler_allowed = False
            if isinstance(scheduler_admission, bool):
                scheduler_allowed = bool(scheduler_admission)
                reason = "" if scheduler_allowed else "scheduler_admission_not_passed"
            elif isinstance(scheduler_admission, dict):
                status_value = _normalize_selector_value(scheduler_admission.get("status") or scheduler_admission.get("state") or "")
                scheduler_allowed = status_value in {"passed", "true", "approved", "success", "accepted"}
                reason = "" if scheduler_allowed else "scheduler_admission_not_passed"
            else:
                reason = "scheduler_admission_missing"
                scheduler_allowed = False

            _append_check(
                checks,
                check_id="scheduler_admission",
                status="passed" if scheduler_allowed else "failed",
                detail=str(scheduler_admission),
            )
            if reason:
                reasons.append("scheduler_admission_missing" if reason == "scheduler_admission_missing" else "scheduler_admission_not_passed")
        else:
            reasons.append("non_continuation_task")
            reasons.append("manual_review_required")
            _append_check(checks, check_id="continuation_metadata_present", status="failed", reason="continuation_metadata_missing")

        requested_worker = requested_scope.get("worker", "")
        if requested_worker:
            if declared_worker not in {"", "codex"} and declared_worker not in allowed_workers:
                allowed_workers.add(declared_worker or "codex")
            if requested_worker and allowed_workers and requested_worker not in allowed_workers:
                reasons.append("worker_scope_mismatch")
                _append_check(
                    checks,
                    check_id="scope_worker",
                    status="failed",
                    requested_worker=requested_worker,
                    declared_worker=declared_worker,
                    allowed_workers=sorted(allowed_workers),
                )
            else:
                _append_check(
                    checks,
                    check_id="scope_worker",
                    status="passed",
                    requested_worker=requested_worker,
                    declared_worker=declared_worker,
                    allowed_workers=sorted(allowed_workers),
                )
        else:
            _append_check(
                checks,
                check_id="scope_worker",
                status="passed",
                requested_worker="",
                declared_worker=declared_worker,
                allowed_workers=sorted(allowed_workers),
            )

        if resource_hint and resource_hint not in {"cpu", "gpu", "any"}:
            reasons.append("unsupported_resource_hint")
            _append_check(checks, check_id="resource_hint", status="failed", resource_hint=resource_hint)
        else:
            _append_check(
                checks,
                check_id="resource_hint",
                status="passed",
                resource_hint=resource_hint or "any",
            )

        _scope_checks(
            front,
            requested_scope,
            checks,
            reasons,
        )

        dispatch_reason = _detect_dispatch_language(body)
        if dispatch_reason:
            reasons.append(dispatch_reason)
            _append_check(checks, check_id="dispatch_language", status="passed", reason=dispatch_reason)

        checks.sort(key=lambda item: (str(item.get("id")), str(item.get("status"))) )

        payload["reasons"] = sorted(set(reasons))
        if not reasons:
            reasons.append("p1_explain_only")

        payload["reasons"] = sorted(set(reasons))
        # P1 is explanation-only by design. Visibility does not grant scheduler
        # authorization, so even cleanly parsed tasks are held for explicit phase-2
        # scheduling admission.
        payload["eligible"] = False
    except Exception:
        _append_check(
            checks,
            check_id="explain_task_exception",
            status="failed",
            reason="unexpected_error",
        )
        reasons.append("manual_review_required")
        payload["reasons"] = sorted(set(reasons))
        payload["eligible"] = False

    if not payload["eligible"] and not payload["reasons"]:
        reasons.append("manual_review_required")
        payload["reasons"] = sorted(set(reasons))

    payload["checks"] = checks
    return payload


__all__ = [
    "explain_active_inbox_task",
    "explain_night_auto_pending_scheduler_dry_run",
    "parse_task_front_matter",
]

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

import yaml

from .paths import solution_continuation_dir
from .scheduler_explain import explain_active_inbox_task


def _policy_root() -> Path:
    root = solution_continuation_dir()
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation"
    if root.exists():
        return root
    if fallback.exists():
        return fallback
    return root


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def _load_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_file("manual_dispatch_policy.yaml"))


def _load_execution_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_file("manual_dispatch_execution_policy.yaml"))


def _policy_file(filename: str) -> Path:
    configured_root = _policy_root()
    root_candidate = configured_root / filename
    if root_candidate.exists():
        return root_candidate

    fallback_root = Path(__file__).resolve().parents[2] / ".solution" / "continuation"
    fallback_candidate = fallback_root / filename
    if fallback_candidate.exists():
        return fallback_candidate

    package_root = Path(__file__).resolve().parents[1]
    package_candidate = package_root / "continuation" / filename
    if package_candidate.exists():
        return package_candidate
    return root_candidate


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_scope(raw: Dict[str, Any] | None) -> Dict[str, str]:
    if not raw:
        return {}
    return {str(key).strip(): _normalize(value) for key, value in raw.items()}


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _request_output_root(policy: Dict[str, Any]) -> Path:
    output_root = policy.get("output_root", "continuation/dispatch_requests/")
    root = solution_continuation_dir().parent.parent
    requested = Path(output_root)
    if requested.is_absolute():
        return requested
    return root / requested


def _request_scope(requested_worker: str, requested_scope: Dict[str, str]) -> Dict[str, str]:
    return {
        "worker": requested_worker,
        "line": _normalize(requested_scope.get("line", "")),
        "batch": _normalize(requested_scope.get("batch", "")),
        "dag_root": _normalize(requested_scope.get("dag_root", "")),
        "prefix": _normalize(requested_scope.get("prefix", "")),
    }


def _build_request_payload(
    *,
    task_path: Path,
    actor: str,
    requested_worker: str,
    declared_worker: str,
    scope: Dict[str, str],
    admission_report: Dict[str, Any],
    admission_report_hash: str,
    request_id: str,
) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "task_id": _normalize(task_path.stem),
        "active_inbox_path": str(task_path),
        "actor": actor,
        "requested_worker": requested_worker,
        "declared_worker": declared_worker,
        "scope": dict(scope),
        "admission_report_hash": admission_report_hash,
        "status": "request_created",
        "checks": list(admission_report.get("checks", [])),
        "reasons": sorted(set(admission_report.get("reasons", []))),
        "side_effects": {
            "execution": False,
            "registry_mutation": False,
            "remote_execution": False,
            "running_state": False,
            "subprocess": False,
        },
    }


def _append_check(checks: List[Dict[str, Any]], *, check_id: str, status: str, **payload: Any) -> None:
    check = {"id": check_id, "status": status}
    check.update(payload)
    checks.append(check)


def _coerce_status_values(values: Any) -> Sequence[str]:
    if isinstance(values, bool):
        return ("true" if values else "false",)
    if values is None:
        return ("passed",)
    if isinstance(values, (str, int)):
        return (str(values).strip().lower(),)
    if isinstance(values, list):
        out: List[str] = []
        for item in values:
            if isinstance(item, bool):
                out.append("true" if item else "false")
            else:
                item_text = _normalize(item).lower()
                if item_text:
                    out.append(item_text)
        return sorted(set(out)) or ("passed",)
    return ("passed",)


def _parse_scheduler_admission(value: Any, accepted_statuses: Sequence[str]) -> tuple[bool, str]:
    if isinstance(value, bool):
        return (bool(value) and "true" in accepted_statuses, "" if bool(value) else "scheduler_admission_not_passed")
    if isinstance(value, dict):
        status = _normalize(value.get("status") or value.get("state") or value.get("result") or value.get("outcome") or "").lower()
        if not status:
            return False, "scheduler_admission_missing"
        if status in accepted_statuses:
            return True, ""
        return False, "scheduler_admission_not_passed"
    if isinstance(value, str):
        status = _normalize(value).lower()
        if not status:
            return False, "scheduler_admission_missing"
        if status in accepted_statuses:
            return True, ""
        return False, "scheduler_admission_not_passed"
    if value is None:
        return False, "scheduler_admission_missing"
    return False, "scheduler_admission_not_passed"


def _collect_scope_checks(
    requested_scope: Dict[str, str],
    selector_task: Dict[str, str],
    checks: List[Dict[str, Any]],
    reasons: List[str],
) -> None:
    for field in ("line", "batch", "dag_root", "prefix"):
        requested = _normalize(requested_scope.get(field))
        actual = _normalize(selector_task.get(field))
        if not requested:
            _append_check(checks, check_id=f"scope_{field}", status="passed", requested=requested, actual=actual)
            continue
        if requested != actual:
            reason = f"{field}_scope_mismatch"
            reasons.append(reason)
            _append_check(
                checks,
                check_id=f"scope_{field}",
                status="failed",
                requested=requested,
                actual=actual,
                reason=reason,
            )
            continue
        _append_check(checks, check_id=f"scope_{field}", status="passed", requested=requested, actual=actual)


def _compose_report(
    task_path: Path,
    *,
    checks: List[Dict[str, Any]],
    reasons: List[str],
    admissible: bool,
    requested_worker: str,
    declared_worker: str,
    selector_task: Dict[str, str],
    selector_request: Dict[str, str],
    scheduler_explain: Dict[str, Any],
) -> Dict[str, Any]:
    next_step_readiness = (
        dict(scheduler_explain.get("next_step_readiness"))
        if isinstance(scheduler_explain.get("next_step_readiness"), dict)
        else {}
    )
    bounded_validation_readiness = (
        dict(scheduler_explain.get("bounded_validation_readiness"))
        if isinstance(scheduler_explain.get("bounded_validation_readiness"), dict)
        else {}
    )
    if not bounded_validation_readiness and isinstance(next_step_readiness.get("bounded_validation"), dict):
        bounded_validation_readiness = dict(next_step_readiness.get("bounded_validation"))

    normalized_reasons = sorted(set(reasons))
    if "disabled" in normalized_reasons:
        status = "disabled"
    elif "manual_review_required" in normalized_reasons or "non_continuation_task" in normalized_reasons:
        status = "manual_review"
    elif admissible:
        status = "admissible"
    else:
        status = "blocked"

    return {
        "task_id": _normalize(task_path.stem),
        "admissible": bool(admissible),
        "status": status,
        "reasons": normalized_reasons,
        "requested_worker": requested_worker,
        "declared_worker": declared_worker,
        "selector": {
            "task": selector_task,
            "request": selector_request,
        },
        "scheduler_explain_summary": {
            "visible_in_active_inbox": bool(scheduler_explain.get("visible_in_active_inbox")),
            "status": scheduler_explain.get("eligible"),
            "eligible": bool(scheduler_explain.get("eligible")),
            "reasons": scheduler_explain.get("reasons", []),
            "checks": scheduler_explain.get("checks", []),
            "next_step_readiness": next_step_readiness,
            "bounded_validation_readiness": bounded_validation_readiness,
            "raw": scheduler_explain,
        },
        "side_effects": {
            "dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
            "subprocess": False,
        },
        "checks": checks,
    }


def assess_manual_dispatch_admission(
    task_path: Path,
    *,
    actor: str,
    requested_worker: str | None = None,
    scope: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Assess whether an active-inbox continuation task is admissible for manual dispatch.

    This helper is decision-only: it reads the active task and returns a structured
    admission report without dispatch, subprocess, registry, or remote side effects.
    """

    task_path = Path(task_path)
    requested_worker_norm = _normalize(requested_worker)
    requested_scope = _normalize_scope(scope)
    checks: List[Dict[str, Any]] = []
    reasons: List[str] = []

    policy = _load_policy()
    required_state = _normalize(policy.get("require_activation_state", "activated_in_inbox"))
    accepted_scheduler_status = set(_coerce_status_values(policy.get("require_scheduler_admission")))
    blocked_states = {_normalize(item).lower() for item in policy.get("blocked_lifecycle_states", [])}
    execution_enabled = bool(policy.get("execution_enabled", False))

    _append_check(checks, check_id="manual_dispatch_execution", status="passed", execution_enabled=execution_enabled)

    explain = explain_active_inbox_task(
        task_path,
        requested_worker=requested_worker_norm or None,
        scope=requested_scope,
    )

    visible = bool(explain.get("visible_in_active_inbox"))
    continuation_meta = explain.get("continuation_metadata", {})
    continuation = continuation_meta.get("continuation") if isinstance(continuation_meta, dict) else {}
    if not isinstance(continuation, dict):
        continuation = {}
    front_matter = continuation_meta.get("front_matter") if isinstance(continuation_meta, dict) else {}
    if not isinstance(front_matter, dict):
        front_matter = {}

    selector = explain.get("selector", {})
    selector_task_raw = selector.get("task", {})
    if not isinstance(selector_task_raw, dict):
        selector_task_raw = {}
    selector_task = {
        "worker": _normalize(selector_task_raw.get("worker", front_matter.get("worker", ""))),
        "line": _normalize(selector_task_raw.get("line", front_matter.get("line", ""))),
        "batch": _normalize(selector_task_raw.get("batch", front_matter.get("batch", ""))),
        "dag_root": _normalize(selector_task_raw.get("dag_root", front_matter.get("dag_root", ""))),
        "prefix": _normalize(selector_task_raw.get("prefix", front_matter.get("prefix", ""))),
        "resource_lane": _normalize(selector_task_raw.get("resource_lane", front_matter.get("resource_lane", ""))),
        "resource_hint": _normalize(selector_task_raw.get("resource_hint", front_matter.get("resource_hint", ""))),
    }
    selector_request = {
        "worker": requested_worker_norm,
        "line": requested_scope.get("line", ""),
        "batch": requested_scope.get("batch", ""),
        "dag_root": requested_scope.get("dag_root", ""),
        "prefix": requested_scope.get("prefix", ""),
    }

    _append_check(checks, check_id="active_inbox_visibility", status="passed" if visible else "failed", visible=visible)
    _append_check(
        checks,
        check_id="task_is_continuation",
        status="passed" if isinstance(continuation, dict) and continuation else "failed",
    )

    if not task_path.exists() or not visible:
        reasons.append("task_not_in_active_inbox")
    if not continuation:
        reasons.extend(["non_continuation_task", "manual_review_required"])
    else:
        _append_check(checks, check_id="continuation_metadata_present", status="passed")

        activation_state = _normalize(
            continuation.get("activation_state")
            or front_matter.get("activation_state")
            or continuation.get("state")
            or front_matter.get("state")
            or continuation.get("lifecycle_state")
            or front_matter.get("lifecycle_state")
            or continuation.get("continuation_state")
            or front_matter.get("continuation_state")
        ).lower()

        if not activation_state:
            reasons.append("activation_state_not_activated_in_inbox")
        elif activation_state != required_state.lower():
            reasons.append("activation_state_not_activated_in_inbox")
        _append_check(
            checks,
            check_id="activation_state",
            status="passed" if activation_state == required_state.lower() else "failed",
            state=activation_state,
            required=required_state,
        )

        if activation_state in blocked_states:
            reason = f"continuation_{activation_state}"
            reasons.append(reason)
            _append_check(checks, check_id="lifecycle_state", status="failed", state=activation_state)
            _append_check(checks, check_id="lifecycle_blocked", status="failed", state=activation_state)
        else:
            _append_check(checks, check_id="lifecycle_state", status="passed", state=activation_state)
            _append_check(checks, check_id="lifecycle_blocked", status="passed", state=activation_state)

        dispatch_allowed = continuation.get("dispatch_allowed")
        if dispatch_allowed is True:
            _append_check(checks, check_id="dispatch_allowed", status="passed", value=True)
        else:
            reasons.append("dispatch_allowed_false")
            _append_check(checks, check_id="dispatch_allowed", status="failed", value=dispatch_allowed)

        scheduler_admission = continuation.get("scheduler_admission")
        scheduler_ok, scheduler_reason = _parse_scheduler_admission(scheduler_admission, accepted_scheduler_status)
        if scheduler_ok:
            _append_check(
                checks,
                check_id="scheduler_admission",
                status="passed",
                value=scheduler_admission,
                accepted_values=sorted(accepted_scheduler_status),
            )
        else:
            reasons.append(scheduler_reason)
            _append_check(
                checks,
                check_id="scheduler_admission",
                status="failed",
                value=scheduler_admission,
                reason=scheduler_reason,
            )

        declared_worker = selector_task["worker"]
        if requested_worker_norm:
            if requested_worker_norm != declared_worker:
                reasons.append("worker_scope_mismatch")
                _append_check(
                    checks,
                    check_id="worker_scope",
                    status="failed",
                    requested=requested_worker_norm,
                    declared=declared_worker,
                )
            else:
                _append_check(
                    checks,
                    check_id="worker_scope",
                    status="passed",
                    requested=requested_worker_norm,
                    declared=declared_worker,
                )
        else:
            _append_check(checks, check_id="worker_scope", status="passed", requested="", declared=declared_worker)

        _collect_scope_checks(requested_scope, selector_task, checks, reasons)

    _append_check(
        checks,
        check_id="no_side_effects",
        status="passed",
        dispatch=False,
        registry_mutation=False,
        remote_execution=False,
        subprocess=False,
    )

    if not execution_enabled and actor not in {"human", "operator", "cli"}:
        reasons.append("disabled")

    reasons_unique = sorted(set(reasons))
    admissible = (
        not reasons_unique
        and not any(
            item in {
                "task_not_in_active_inbox",
                "activation_state_not_activated_in_inbox",
                "dispatch_allowed_false",
                "scheduler_admission_missing",
                "scheduler_admission_not_passed",
                "worker_scope_mismatch",
                "line_scope_mismatch",
                "batch_scope_mismatch",
                "dag_root_scope_mismatch",
                "prefix_scope_mismatch",
            }
            for item in reasons_unique
        )
        and not any(item.startswith("continuation_") for item in reasons_unique)
    )

    return _compose_report(
        task_path,
        checks=checks,
        reasons=reasons_unique,
        admissible=admissible,
        requested_worker=requested_worker_norm,
        declared_worker=selector_task["worker"],
        selector_task=selector_task,
        selector_request=selector_request,
        scheduler_explain=explain,
    )


def create_manual_dispatch_request(
    task_path: Path,
    *,
    actor: str,
    requested_worker: str | None = None,
    scope: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Create a local manual-dispatch request artifact if and only if admission passes."""

    task_path = Path(task_path)
    requested_worker_norm = _normalize(requested_worker)
    requested_scope = _normalize_scope(scope)
    request_scope = _request_scope(requested_worker_norm, requested_scope)

    policy = _load_execution_policy()
    requires_admissible_report = bool(policy.get("requires_admissible_report", False))
    admission_report = assess_manual_dispatch_admission(
        task_path,
        actor=actor,
        requested_worker=requested_worker_norm or None,
        scope=requested_scope,
    )

    admission_report_hash = _sha256_hex(admission_report)

    request_id_input = {
        "task_id": _normalize(task_path.stem),
        "actor": actor,
        "requested_worker": requested_worker_norm,
        "scope": request_scope,
        "admission_report_hash": admission_report_hash,
    }
    request_id = f"{_normalize(task_path.stem)}__{_sha256_hex(request_id_input)[:16]}"
    request_output_root = _request_output_root(policy)
    request_path = request_output_root / f"{request_id}.json"

    if requires_admissible_report and not admission_report.get("admissible", False):
        return {
            "request_id": request_id,
            "task_id": _normalize(task_path.stem),
            "active_inbox_path": str(task_path),
            "actor": actor,
            "requested_worker": requested_worker_norm,
            "declared_worker": _normalize(admission_report.get("declared_worker", "")),
            "scope": request_scope,
            "admission_report_hash": admission_report_hash,
            "status": "blocked" if admission_report.get("status") in {"blocked", "manual_review"} else admission_report.get("status", "blocked"),
            "checks": list(admission_report.get("checks", [])),
            "reasons": sorted(set(admission_report.get("reasons", []))),
            "side_effects": {
                "execution": False,
                "registry_mutation": False,
                "remote_execution": False,
                "running_state": False,
                "subprocess": False,
            },
            "request_path": str(request_path),
            "created": False,
            "replayed": False,
        }

    payload = _build_request_payload(
        task_path=task_path,
        actor=actor,
        requested_worker=requested_worker_norm,
        declared_worker=_normalize(admission_report.get("declared_worker", "")),
        scope=request_scope,
        admission_report=admission_report,
        admission_report_hash=admission_report_hash,
        request_id=request_id,
    )
    if not request_output_root.exists():
        request_output_root.mkdir(parents=True, exist_ok=True)

    if request_path.exists():
        try:
            existing = json.loads(request_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            blocked = {
                "status": "blocked",
                "request_id": request_id,
                "task_id": _normalize(task_path.stem),
                "active_inbox_path": str(task_path),
                "actor": actor,
                "requested_worker": requested_worker_norm,
                "declared_worker": _normalize(admission_report.get("declared_worker", "")),
                "scope": request_scope,
                "admission_report_hash": admission_report_hash,
                "checks": list(admission_report.get("checks", [])),
                "reasons": sorted(set(admission_report.get("reasons", []))) + ["request_payload_corrupt"],
                "side_effects": {
                    "execution": False,
                    "registry_mutation": False,
                    "remote_execution": False,
                    "running_state": False,
                    "subprocess": False,
                },
                "request_path": str(request_path),
                "created": False,
                "replayed": False,
                "request_json_parse_error": str(exc),
            }
            return {
                **blocked,
            }

        expected = payload
        if existing == expected:
            payload["status"] = "request_created"
            return {
                **payload,
                "request_path": str(request_path),
                "created": False,
                "replayed": True,
            }

        return {
            **payload,
            "status": "blocked",
            "checks": list(admission_report.get("checks", [])),
            "reasons": sorted(set(admission_report.get("reasons", [])) | {"request_id_collision"}),
            "request_path": str(request_path),
            "created": False,
            "replayed": False,
        }

    request_path.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
    payload["status"] = "request_created"
    return {
        **payload,
        "request_path": str(request_path),
        "created": True,
        "replayed": False,
    }


__all__ = ["assess_manual_dispatch_admission", "create_manual_dispatch_request"]

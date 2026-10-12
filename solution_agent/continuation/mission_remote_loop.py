from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from .bounded_validation_bucket import (
    B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS,
    build_b14_l74_bounded_validation_bucket,
    write_b14_l74_artifact_readiness_report,
)
from .knowledge_curator import (
    build_knowledge_update_proposal_dry_run,
    write_knowledge_update_proposal_dry_run,
)
from .knowledge_to_proposal import (
    validate_proposal_candidate,
    write_proposal_candidates_dry_run,
)
from .memory_observer import append_continuation_observation
from .night_researcher import mission_checkpoint_skeleton, mission_state_skeleton
from .paths import root as continuation_root
from .research_prerequisites import assess_research_prerequisites
from .schema import StrictModel


ALLOWED_MISSION_TASK_CLASSES = {
    "artifact_inventory",
    "cache_inventory",
    "bounded_cpu_probe",
    "bounded_gpu_smoke",
    "bounded_validation_bucket",
    "failure_characterization",
    "metric_analysis",
    "visual_diagnostic",
    "rerun_after_verified_repair",
}

DISCUSSION_EVIDENCE_PACK_REQUIRED_FIELDS = (
    "canonical_formulation",
    "current_state_chronology",
    "materialized_results",
    "diagnostic_proxy_results",
    "negative_results",
    "artifact_inventory",
    "claim_limits",
    "open_uncertainties",
)

FORMULATION_REQUIRED_FIELDS = (
    "canonical_or_diagnostic",
    "canonical_formulation",
    "learnable_modules",
    "forbidden_substitutions",
)

MISSION_REMOTE_ALLOWED_COMMAND_PHASES = {"full", "prepare_code", "execute_run"}

FOLLOWUP_KIND_BY_NEXT_ACTION = {
    "continue": "bounded_probe",
    "repair_once_then_rerun": "metric_completion",
    "pivot": "decision_synthesis",
}


class MissionRemoteGateReport(StrictModel):
    schema_version: int = 1
    artifact_type: str = "mission_remote_gate_report"
    mission_id: str
    task_id: str
    status: str
    can_rehearse_locally: bool
    allow_remote_launch: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    reason_text: str = ""
    task_class: str = ""
    workspace_profile: dict[str, str] = Field(default_factory=dict)
    collect_judge_contract_present: bool = False
    discussion_evidence_pack_present: bool = False
    formulation_faithfulness_present: bool = False
    evidence_gap: dict[str, Any] = Field(default_factory=dict)
    budget_ledger_path: str = ""
    prior_budget_ledger_entries: int = 0
    prior_consecutive_proxy_or_diagnostic_tasks: int = 0
    prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks: int = 0
    prior_remote_collect_judge_pending: bool = False
    prior_pending_remote_iteration_id: str = ""
    proxy_or_diagnostic_budget_limit: int = 0
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "ssh": False,
            "remote_write": False,
            "remote_execution": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "raw_dataset_mutation": False,
            "checkpoint_mutation": False,
            "canonical_kb_mutation": False,
        }
    )


class MissionRemoteContinuationDecision(StrictModel):
    schema_version: int = 1
    artifact_type: str = "mission_remote_continuation_decision"
    mission_id: str
    task_id: str
    status: str
    next_action: str
    reason_codes: list[str] = Field(default_factory=list)
    reason_text: str = ""
    judge_status: str = ""
    claim_limits: list[str] = Field(default_factory=list)
    information_gain: dict[str, int] = Field(default_factory=dict)
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "ssh": False,
            "remote_write": False,
            "remote_execution": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "canonical_kb_mutation": False,
        }
    )


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_normalize(item) for item in value if _normalize(item)]
    normalized = _normalize(value)
    return [normalized] if normalized else []


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def _formulation_lane(task_payload: dict[str, Any]) -> str:
    formulation = task_payload.get("formulation_faithfulness")
    if not isinstance(formulation, dict):
        return ""
    return _normalize(formulation.get("canonical_or_diagnostic")).lower()


def _task_proxy_budget_used(task_payload: dict[str, Any]) -> int:
    formulation = task_payload.get("formulation_faithfulness")
    formulation_value = formulation.get("proxy_budget_used") if isinstance(formulation, dict) else None
    raw_value = task_payload.get("proxy_budget_used")
    try:
        normalized = int(raw_value or formulation_value or 0)
    except (TypeError, ValueError):
        normalized = 0
    return max(normalized, 0)


def _proxy_chain_budget_limit(mission_payload: dict[str, Any]) -> int:
    remote_budget = mission_payload.get("remote_budget")
    if isinstance(remote_budget, dict):
        limit = remote_budget.get("max_consecutive_proxy_or_diagnostic_tasks")
        if limit is not None:
            return int(limit)
    return int(mission_payload.get("proxy_budget_limit") or 2)


def _budget_entry_execution_scope(row: dict[str, Any]) -> str:
    scope = _normalize(row.get("execution_scope")).lower()
    if scope:
        return scope
    if bool(row.get("executed")) or int(row.get("remote_tasks_total_used") or 0) > 0:
        return "remote_task"
    if int(row.get("local_rehearsals_total_used") or 0) > 0:
        return "local_rehearsal"
    return ""


def _budget_row_counts_as_executed(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    scope = _budget_entry_execution_scope(row)
    if scope == "remote_collect_judge":
        return bool(row.get("collect_judge_completed"))
    if scope != "remote_task":
        return bool(row.get("executed"))
    launch_status = _normalize(row.get("launch_status")).lower()
    if launch_status in {"launch_failed", "launch_unknown", "unknown"}:
        return False
    return bool(row.get("executed"))


def _count_consecutive_proxy_or_diagnostic_tasks(
    budget_rows: list[dict[str, Any]],
    *,
    execution_scope: str | None = None,
) -> int:
    count = 0
    normalized_scope = _normalize(execution_scope).lower()
    for row in reversed(budget_rows):
        if not isinstance(row, dict):
            break
        row_scope = _budget_entry_execution_scope(row)
        if normalized_scope and row_scope != normalized_scope:
            continue
        if row_scope == "remote_task" and not _budget_row_counts_as_executed(row):
            continue
        lane = _normalize(row.get("canonical_or_diagnostic")).lower()
        if lane == "diagnostic":
            count += 1
            continue
        if lane == "canonical":
            break
        break
    return count


def _consecutive_proxy_or_diagnostic_tasks_used(
    run_root: Path | str | None,
) -> tuple[int, int, list[dict[str, Any]], Path]:
    budget_ledger_path = Path(run_root) / "budget_ledger.jsonl" if run_root else Path("")
    budget_rows = _read_jsonl(budget_ledger_path) if run_root else []
    if not budget_rows:
        return 0, 0, budget_rows, budget_ledger_path

    latest_row = budget_rows[-1] if isinstance(budget_rows[-1], dict) else {}
    latest_remote_value = latest_row.get("consecutive_proxy_or_diagnostic_tasks_used")
    latest_local_value = latest_row.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used")
    latest_scope = _budget_entry_execution_scope(latest_row)
    latest_remote_usable = latest_scope != "remote_task" or _budget_row_counts_as_executed(latest_row)
    if latest_remote_value is not None and latest_local_value is not None and latest_remote_usable:
        try:
            return (
                int(latest_remote_value),
                int(latest_local_value),
                budget_rows,
                budget_ledger_path,
            )
        except (TypeError, ValueError):
            pass

    remote_count = _count_consecutive_proxy_or_diagnostic_tasks(
        budget_rows,
        execution_scope="remote_task",
    )
    local_count = _count_consecutive_proxy_or_diagnostic_tasks(
        budget_rows,
        execution_scope="local_rehearsal",
    )
    return remote_count, local_count, budget_rows, budget_ledger_path


def _latest_remote_collect_judge_pending(
    budget_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    finalized_iterations: set[str] = set()
    for row in reversed(budget_rows):
        if not isinstance(row, dict):
            continue
        iteration_id = _normalize(row.get("iteration_id"))
        row_scope = _budget_entry_execution_scope(row)
        if row_scope == "remote_collect_judge" and bool(row.get("collect_judge_completed")):
            if iteration_id:
                finalized_iterations.add(iteration_id)
            continue
        if row_scope != "remote_task" or not _budget_row_counts_as_executed(row):
            continue
        if bool(row.get("collect_judge_completed")):
            return False, ""
        if iteration_id and iteration_id in finalized_iterations:
            return False, ""
        return True, iteration_id
    return False, ""


def _budget_totals(budget_rows: list[dict[str, Any]]) -> tuple[int, int]:
    remote_tasks_total = 0
    local_rehearsals_total = 0
    for row in budget_rows:
        if not isinstance(row, dict):
            continue
        try:
            remote_tasks_total = max(remote_tasks_total, int(row.get("remote_tasks_total_used") or 0))
        except (TypeError, ValueError):
            pass
        try:
            local_rehearsals_total = max(
                local_rehearsals_total,
                int(row.get("local_rehearsals_total_used") or 0),
            )
        except (TypeError, ValueError):
            pass
    return remote_tasks_total, local_rehearsals_total


def _budget_row_proxy_counts(row: dict[str, Any]) -> tuple[int, int]:
    if not isinstance(row, dict):
        return 0, 0

    scope = _budget_entry_execution_scope(row)
    remote_value = row.get("consecutive_proxy_or_diagnostic_tasks_used")
    local_value = row.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used")

    if scope == "local_rehearsal":
        if local_value is None:
            try:
                local_value = int(remote_value or 0)
            except (TypeError, ValueError):
                local_value = 0
        try:
            return 0, int(local_value or 0)
        except (TypeError, ValueError):
            return 0, 0
    if scope == "remote_task" and not _budget_row_counts_as_executed(row):
        try:
            local_count = int(local_value or 0)
        except (TypeError, ValueError):
            local_count = 0
        return 0, local_count

    try:
        remote_count = int(remote_value or 0)
    except (TypeError, ValueError):
        remote_count = 0
    try:
        local_count = int(local_value or 0)
    except (TypeError, ValueError):
        local_count = 0
    return remote_count, local_count


def _normalized_budget_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row) if isinstance(row, dict) else {}
    remote_count, local_count = _budget_row_proxy_counts(normalized)
    normalized["execution_scope"] = _budget_entry_execution_scope(normalized)
    normalized["consecutive_proxy_or_diagnostic_tasks_used"] = remote_count
    normalized["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] = local_count
    return normalized


def _latest_effective_budget_row(
    budget_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    if not budget_rows:
        return {}, {}, False

    latest_audit_budget = _normalized_budget_row(budget_rows[-1])
    for row in reversed(budget_rows):
        if not isinstance(row, dict):
            continue
        scope = _budget_entry_execution_scope(row)
        if scope == "remote_task" and not _budget_row_counts_as_executed(row):
            continue
        return _normalized_budget_row(row), latest_audit_budget, row is not budget_rows[-1]
    return latest_audit_budget, latest_audit_budget, False


def _read_iteration_artifacts(iteration_dir: Path) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {
        "decision": {},
        "judge": {},
        "gate": {},
        "information_gain": {},
        "kb_update": {},
    }
    for name, target in (
        ("decision", "continuation_decision.yaml"),
        ("judge", "judge_report.yaml"),
        ("gate", "mission_gate_report.yaml"),
        ("information_gain", "information_gain.yaml"),
        ("kb_update", "kb_update_proposal/proposal_pointer.yaml"),
    ):
        path = iteration_dir / target
        if not path.exists():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(payload, dict):
            payloads[name] = payload
    return payloads


def _latest_actionable_iteration(
    iterations: list[Path],
) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    for iteration_dir in reversed(iterations):
        payloads = _read_iteration_artifacts(iteration_dir)
        if payloads["decision"]:
            return iteration_dir, payloads
    return None, {
        "decision": {},
        "judge": {},
        "gate": {},
        "information_gain": {},
        "kb_update": {},
    }


def _assess_followup_candidate_state(
    *,
    mission_id: str,
    task_id: str,
    latest_iteration_id: str,
    gate_payload: dict[str, Any],
    decision_payload: dict[str, Any],
    latest_lane: str,
    latest_consecutive_proxy_tasks: int,
    latest_local_rehearsal_proxy_tasks: int,
    proxy_chain_limit: int,
) -> dict[str, Any]:
    next_action = _normalize(decision_payload.get("next_action")).lower()
    candidate_kind = FOLLOWUP_KIND_BY_NEXT_ACTION.get(next_action, "")
    reasons: list[str] = []
    if _normalize(gate_payload.get("status")).lower() != "allow":
        reasons.append("mission_gate_blocked")
    if _normalize(decision_payload.get("status")).lower() != "ready":
        reasons.append("continuation_decision_not_ready")
    if not candidate_kind:
        reasons.append("next_action_not_stageable")
    if (
        candidate_kind == "bounded_probe"
        and latest_lane == "diagnostic"
        and proxy_chain_limit > 0
        and latest_consecutive_proxy_tasks >= proxy_chain_limit
    ):
        reasons.append("proxy_chain_budget_exhausted")

    return {
        "mission_id": mission_id,
        "task_id": task_id,
        "latest_iteration_id": latest_iteration_id,
        "next_action": next_action,
        "candidate_kind": candidate_kind,
        "latest_canonical_or_diagnostic": latest_lane,
        "latest_consecutive_proxy_or_diagnostic_tasks_used": latest_consecutive_proxy_tasks,
        "latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": latest_local_rehearsal_proxy_tasks,
        "proxy_or_diagnostic_budget_limit": proxy_chain_limit,
        "status": "ready" if not reasons else "blocked",
        "can_write_proposal": not reasons,
        "candidate_count": 1 if not reasons else 0,
        "reasons": sorted(set(reasons)),
    }


def _compact_bounded_validation_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    artifact_statuses = summary.get("artifact_statuses") if isinstance(summary.get("artifact_statuses"), dict) else {}
    compact = {
        "bucket_id": _normalize(payload.get("bucket_id") or summary.get("bucket_id")),
        "status": _normalize(payload.get("status") or summary.get("status")).lower(),
        "next_recommendation": _normalize(
            payload.get("next_recommendation") or summary.get("next_recommendation")
        ).lower(),
        "present_valid_artifact_ids": _normalize_string_list(summary.get("present_valid_artifact_ids")),
        "missing_artifact_ids": _normalize_string_list(summary.get("missing_artifact_ids")),
        "invalid_artifact_ids": _normalize_string_list(summary.get("invalid_artifact_ids")),
        "route_reasons": _normalize_string_list(summary.get("route_reasons")),
        "artifact_statuses": {
            _normalize(key): _normalize(value).lower()
            for key, value in artifact_statuses.items()
            if _normalize(key) and _normalize(value)
        },
        "reason_count": int(summary.get("reason_count") or len(_normalize_string_list(payload.get("reasons")))),
    }
    return compact


def _as_dict(payload: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError(f"{name} must be a dict")
    return dict(payload)


def _workspace_profile(task_payload: dict[str, Any]) -> dict[str, str]:
    raw = task_payload.get("workplace_profile")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "profile": _normalize(raw.get("profile")),
        "host": _normalize(raw.get("host")),
        "remote_root": _normalize(raw.get("remote_root")),
        "remote_workdir": _normalize(raw.get("remote_workdir")),
    }


def _last_remote_preflight_path() -> Path:
    return continuation_root() / "state" / "last_remote_preflight.json"


def _remote_preflight_matches_workplace(preflight: dict[str, Any], workplace_profile: dict[str, Any]) -> bool:
    return (
        _normalize(preflight.get("host")) == _normalize(workplace_profile.get("host"))
        and _normalize(preflight.get("remote_root")) == _normalize(workplace_profile.get("remote_root"))
        and _normalize(preflight.get("remote_workdir")) == _normalize(workplace_profile.get("remote_workdir"))
    )


def _latest_remote_transport_snapshot(workplace_profile: dict[str, Any]) -> dict[str, Any]:
    path = _last_remote_preflight_path()
    if not path.exists():
        return {
            "present": False,
            "matched_workplace": False,
            "status": "unknown",
            "reason_code": "remote_preflight_not_run",
            "automation_retry_safe": False,
            "recovery_action": "run remote-preflight before remote launch",
            "source_path": str(path),
        }
    try:
        preflight = _read_json(path)
    except (ValueError, json.JSONDecodeError):
        return {
            "present": True,
            "matched_workplace": False,
            "status": "unknown",
            "reason_code": "remote_preflight_corrupt",
            "automation_retry_safe": False,
            "recovery_action": "rerun remote-preflight; cached transport report is corrupt",
            "source_path": str(path),
        }
    if not _remote_preflight_matches_workplace(preflight, workplace_profile):
        return {
            "present": True,
            "matched_workplace": False,
            "status": "ignored",
            "reason_code": "remote_preflight_workplace_mismatch",
            "automation_retry_safe": False,
            "recovery_action": "rerun remote-preflight for the exact mission workplace profile",
            "source_path": str(path),
            "checked_at": _normalize(preflight.get("checked_at")),
            "host": _normalize(preflight.get("host")),
            "remote_root": _normalize(preflight.get("remote_root")),
            "remote_workdir": _normalize(preflight.get("remote_workdir")),
        }
    transport = preflight.get("transport")
    if not isinstance(transport, dict):
        transport = {}
    return {
        "present": True,
        "matched_workplace": True,
        "ok": bool(preflight.get("ok")),
        "checked_at": _normalize(preflight.get("checked_at")),
        "host": _normalize(preflight.get("host")),
        "remote_root": _normalize(preflight.get("remote_root")),
        "remote_workdir": _normalize(preflight.get("remote_workdir")),
        "profile_id": _normalize(preflight.get("profile_id")),
        "status": _normalize(transport.get("status")) or ("ok" if preflight.get("ok") else "unknown"),
        "reason_code": _normalize(transport.get("reason_code")) or ("transport_ok" if preflight.get("ok") else "transport_unclassified"),
        "automation_retry_safe": bool(transport.get("automation_retry_safe")),
        "recovery_action": _normalize(transport.get("recovery_action")),
        "ssh_user": _normalize(transport.get("ssh_user")),
        "ssh_port": _normalize(transport.get("ssh_port")),
        "source_path": str(path),
    }


def _load_remote_execution_profiles() -> dict[str, dict[str, Any]]:
    profile_path = continuation_root() / "config" / "remote_execution_profiles.yaml"
    if not profile_path.exists():
        return {}
    payload = _read_yaml_dict(profile_path)
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {
        _normalize(name): dict(value)
        for name, value in profiles.items()
        if _normalize(name) and isinstance(value, dict)
    }


def _profile_remote_root(profile_payload: dict[str, Any]) -> str:
    return _normalize(profile_payload.get("remote_root") or profile_payload.get("remote_output_root"))


def _resolve_mission_workplace_profile(task_payload: dict[str, Any]) -> dict[str, Any]:
    workspace_profile = _workspace_profile(task_payload)
    configured_profiles = _load_remote_execution_profiles()
    explicit_profile = workspace_profile["profile"]
    explicit_host = workspace_profile["host"]
    explicit_remote_root = workspace_profile["remote_root"]
    explicit_remote_workdir = workspace_profile["remote_workdir"]
    reasons: list[str] = []

    def _resolved_payload(profile_name: str, configured: dict[str, Any], *, source: str) -> dict[str, Any]:
        configured_host = _normalize(configured.get("host"))
        configured_remote_workdir = _normalize(configured.get("remote_workdir"))
        configured_remote_root = _profile_remote_root(configured)
        mismatches: list[dict[str, str]] = []
        for field, explicit_value, configured_value in (
            ("host", explicit_host, configured_host),
            ("remote_root", explicit_remote_root, configured_remote_root),
            ("remote_workdir", explicit_remote_workdir, configured_remote_workdir),
        ):
            if explicit_value and configured_value and explicit_value != configured_value:
                mismatches.append(
                    {
                        "field": field,
                        "explicit": explicit_value,
                        "configured": configured_value,
                    }
                )
        return {
            "status": "resolved" if not mismatches else "blocked",
            "profile": profile_name,
            "host": configured_host,
            "remote_root": configured_remote_root,
            "remote_workdir": configured_remote_workdir,
            "source": source,
            "mismatches": mismatches,
            "reasons": [] if not mismatches else ["workplace_profile_mismatch"],
        }

    if explicit_profile:
        configured = configured_profiles.get(explicit_profile)
        if not isinstance(configured, dict):
            return {
                "status": "blocked",
                "profile": explicit_profile,
                "host": "",
                "remote_root": "",
                "remote_workdir": "",
                "source": "unresolved",
                "mismatches": [],
                "reasons": ["unknown_workplace_profile"],
            }
        return _resolved_payload(explicit_profile, configured, source="profile_name")

    if explicit_host and explicit_remote_root and explicit_remote_workdir:
        matches: list[tuple[str, dict[str, Any]]] = []
        for profile_name, configured in configured_profiles.items():
            if (
                explicit_host == _normalize(configured.get("host"))
                and explicit_remote_root == _profile_remote_root(configured)
                and explicit_remote_workdir == _normalize(configured.get("remote_workdir"))
            ):
                matches.append((profile_name, configured))
        if len(matches) == 1:
            profile_name, configured = matches[0]
            return _resolved_payload(profile_name, configured, source="exact_host_root_workdir_match")
        reasons.append("workplace_profile_resolution_required")
        if len(matches) > 1:
            reasons.append("workplace_profile_ambiguous")
    else:
        reasons.append("missing_workplace_profile")

    return {
        "status": "blocked",
        "profile": explicit_profile,
        "host": explicit_host,
        "remote_root": explicit_remote_root,
        "remote_workdir": explicit_remote_workdir,
        "source": "unresolved",
        "mismatches": [],
        "reasons": sorted(set(reasons)),
    }


def _missing_discussion_evidence_fields(payload: dict[str, Any]) -> list[str]:
    return [
        field
        for field in DISCUSSION_EVIDENCE_PACK_REQUIRED_FIELDS
        if field not in payload or not payload.get(field)
    ]


def _missing_formulation_fields(payload: dict[str, Any]) -> list[str]:
    missing = [
        field for field in FORMULATION_REQUIRED_FIELDS if field not in payload or not payload.get(field)
    ]
    lane = _normalize(payload.get("canonical_or_diagnostic")).lower()
    if lane == "diagnostic":
        if not payload.get("diagnostic_cannot_prove"):
            missing.append("diagnostic_cannot_prove")
        if not payload.get("route_back_to_canonical"):
            missing.append("route_back_to_canonical")
    return missing


def _mission_remote_launch_packet_payload(
    *,
    mission_id: str,
    task_id: str,
    task_class: str,
    workplace_profile: dict[str, Any],
    remote_dispatch_task_id: str,
    phase: str,
    detach: bool,
) -> dict[str, Any]:
    command_phase = _normalize(phase) or "execute_run"
    if command_phase not in MISSION_REMOTE_ALLOWED_COMMAND_PHASES:
        command_phase = "execute_run"
    command_task_id = remote_dispatch_task_id or "__REMOTE_TASK_ID_REQUIRED__"
    argv = [
        "python",
        "-m",
        "solution_agent.cli",
        "run-remote",
        command_task_id,
        "--host",
        _normalize(workplace_profile.get("host")),
        "--remote-root",
        _normalize(workplace_profile.get("remote_root")),
        "--remote-workdir",
        _normalize(workplace_profile.get("remote_workdir")),
        "--worker",
        "materialized_execution",
        "--phase",
        command_phase,
    ]
    if detach:
        argv.append("--detach")
    launch_packet_id = _decision_id(mission_id, task_id, 0, f"launch_{task_class or 'task'}")
    return {
        "schema_version": 1,
        "artifact_type": "mission_remote_launch_packet",
        "mission_id": mission_id,
        "task_id": task_id,
        "task_class": task_class,
        "launch_packet_id": launch_packet_id,
        "status": "ready",
        "remote_dispatch_task_id": remote_dispatch_task_id,
        "command_task_id": command_task_id,
        "execution_mode": "command_packet_dry_run",
        "worker": "materialized_execution",
        "workplace_profile": {
            "profile": _normalize(workplace_profile.get("profile")),
            "host": _normalize(workplace_profile.get("host")),
            "remote_root": _normalize(workplace_profile.get("remote_root")),
            "remote_workdir": _normalize(workplace_profile.get("remote_workdir")),
        },
        "phase": command_phase,
        "detach": bool(detach),
        "argv": argv,
        "display_command": " ".join(f'"{item}"' if " " in item else item for item in argv),
        "intended_command_side_effects_if_executed": {
            "ssh": True,
            "remote_write": True,
            "remote_execution": True,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "raw_dataset_mutation": False,
            "checkpoint_mutation": False,
            "canonical_kb_mutation": False,
        },
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "remote_execution": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "raw_dataset_mutation": False,
            "checkpoint_mutation": False,
            "canonical_kb_mutation": False,
        },
    }


def _mission_remote_required_outputs(task_payload: dict[str, Any], task_class: str) -> list[str]:
    expected = task_payload.get("expected_outputs")
    if isinstance(expected, dict):
        required = expected.get("required")
        if isinstance(required, list):
            outputs = [_normalize(item) for item in required if _normalize(item)]
            if outputs:
                return outputs
    if task_class == "bounded_validation_bucket":
        bucket = build_b14_l74_bounded_validation_bucket()
        return [_normalize(item) for item in bucket.expected_outputs if _normalize(item)]
    return []


def _mission_remote_task_markdown(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    dispatch_task_id: str,
    workplace_profile: dict[str, Any],
) -> str:
    mission_id = _normalize(mission_payload.get("mission_id") or mission_payload.get("id") or "unknown_mission")
    logical_task_id = _normalize(task_payload.get("task_id") or dispatch_task_id or "unknown_task")
    task_class = _normalize(task_payload.get("task_class"))
    resource_hint = _normalize(task_payload.get("resource_hint") or "cpu")
    objective = _normalize(
        mission_payload.get("objective", {}).get("short")
        if isinstance(mission_payload.get("objective"), dict)
        else mission_payload.get("objective")
    ) or f"Execute the mission-bounded remote task for {logical_task_id}."
    expected_outputs = _mission_remote_required_outputs(task_payload, task_class)
    claim_limits = _normalize_string_list(
        task_payload.get("discussion_evidence_pack", {}).get("claim_limits")
        if isinstance(task_payload.get("discussion_evidence_pack"), dict)
        else []
    )
    formulation = task_payload.get("formulation_faithfulness")
    if not isinstance(formulation, dict):
        formulation = {}
    bounded_scope = task_payload.get("bounded_scope")
    if not isinstance(bounded_scope, dict):
        bounded_scope = {}
    task_body_lines = [
        "# Mission Remote Task",
        "",
        f"Mission id: `{mission_id}`",
        f"Logical task id: `{logical_task_id}`",
        f"Dispatch task id: `{dispatch_task_id}`",
        f"Task class: `{task_class or 'unknown'}`",
        "",
        "Run one mission-bounded remote task only. Keep all outputs run-scoped under the target workplace remote root.",
        "",
        "## Mission Constraints",
        "",
        "- Use the exact workplace profile resolved for this mission task.",
        "- Do not mutate raw datasets, checkpoints, canonical knowledge, or scheduler state.",
        "- Treat all outputs as bounded evidence only; do not upgrade claims.",
        "- After launch, the controller must sync, collect, and judge before any second remote action.",
        "",
        "## Target Workplace",
        "",
        f"- profile: `{_normalize(workplace_profile.get('profile'))}`",
        f"- host: `{_normalize(workplace_profile.get('host'))}`",
        f"- remote_workdir: `{_normalize(workplace_profile.get('remote_workdir'))}`",
        f"- remote_root: `{_normalize(workplace_profile.get('remote_root'))}`",
        "",
        "## Bounded Scope",
        "",
        "```yaml",
        yaml.safe_dump(bounded_scope or {"scope": "mission_bounded_remote_task"}, sort_keys=False, allow_unicode=True).strip(),
        "```",
        "",
        "## Formulation Faithfulness",
        "",
        f"- canonical_or_diagnostic: `{_normalize(formulation.get('canonical_or_diagnostic')) or 'unknown'}`",
        f"- canonical formulation: {_normalize(formulation.get('canonical_formulation')) or 'unspecified'}",
        f"- learnable modules: {', '.join(_normalize_string_list(formulation.get('learnable_modules'))) or 'unspecified'}",
        f"- forbidden substitutions: {', '.join(_normalize_string_list(formulation.get('forbidden_substitutions'))) or 'unspecified'}",
        f"- route back to canonical: {_normalize(formulation.get('route_back_to_canonical')) or 'unspecified'}",
        "",
        "## Required Outputs",
        "",
    ]
    for output_name in expected_outputs:
        task_body_lines.append(f"- {output_name}")
    task_body_lines.extend(
        [
            "",
            "## Claim Limits",
            "",
        ]
    )
    for limit in claim_limits or ["bounded_proxy_evidence_only", "no_formal_claim_upgrade"]:
        task_body_lines.append(f"- {limit}")
    task_body_lines.extend(
        [
            "",
            "## Notes",
            "",
            "Use the mission payload and discussion evidence as the authoritative brief for this bounded validation run.",
        ]
    )

    front_matter = {
        "task_id": dispatch_task_id,
        "title": f"Mission remote {logical_task_id}",
        "priority": "high",
        "worker": "materialized_execution",
        "status": "pending",
        "created_at": _normalize(mission_payload.get("created_at")) or None,
        "objective": objective,
        "success_criteria": [
            "Produce the required bounded-validation artifacts.",
            "Keep outputs claim-limited and run-scoped.",
            "Do not modify forbidden remote state.",
        ],
        "expected_outputs": expected_outputs,
        "line": "mission_remote",
        "batch": mission_id,
        "dag_root": mission_id,
        "resource_hint": resource_hint or "cpu",
        "target_workplace": {
            "profile": _normalize(workplace_profile.get("profile")),
            "host": _normalize(workplace_profile.get("host")),
            "remote_root": _normalize(workplace_profile.get("remote_root")),
            "remote_workdir": _normalize(workplace_profile.get("remote_workdir")),
        },
        "mission_remote": {
            "mission_id": mission_id,
            "logical_task_id": logical_task_id,
            "task_class": task_class,
        },
    }
    if not front_matter["created_at"]:
        front_matter.pop("created_at")
    return (
        "---\n"
        + yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + "\n".join(task_body_lines)
        + "\n"
    )


def _ensure_mission_remote_task_registered(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    dispatch_task_id: str,
    workplace_profile: dict[str, Any],
) -> dict[str, Any]:
    from ..orchestrator import core as lab

    try:
        current_status, task_path = lab.find_task(dispatch_task_id)
        return {
            "status": "existing",
            "task_id": dispatch_task_id,
            "task_path": str(task_path),
            "task_status": current_status,
            "created": False,
        }
    except FileNotFoundError:
        pass

    task_path = lab.task_file("pending", dispatch_task_id)
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        _mission_remote_task_markdown(
            mission_payload,
            task_payload,
            dispatch_task_id=dispatch_task_id,
            workplace_profile=workplace_profile,
        ),
        encoding="utf-8",
    )
    task = lab.load_task(task_path)
    lab.upsert_task(task, status="pending")
    return {
        "status": "created",
        "task_id": dispatch_task_id,
        "task_path": str(task_path),
        "task_status": "pending",
        "created": True,
    }


def _mission_remote_launch_outcome(launch_result: dict[str, Any]) -> tuple[str, bool]:
    explicit_status = _normalize(
        launch_result.get("status")
        or launch_result.get("task_status")
        or launch_result.get("remote_state")
    ).lower()
    run_payload = launch_result.get("run")
    if not isinstance(run_payload, dict):
        run_payload = {}
    launch_bundle = launch_result.get("launch_bundle")
    if not isinstance(launch_bundle, dict):
        launch_bundle = {}
    copy_payload = launch_bundle.get("copy")
    if not isinstance(copy_payload, dict):
        copy_payload = {}

    run_timed_out = bool(run_payload.get("timed_out"))
    if run_timed_out or explicit_status == "launch_unknown":
        return "launch_unknown", False

    for payload in (copy_payload, run_payload):
        returncode = payload.get("returncode")
        if returncode is None:
            continue
        try:
            if int(returncode) != 0:
                return "launch_failed", False
        except (TypeError, ValueError):
            return "launch_failed", False

    if explicit_status in {"blocked", "launch_failed", "failed"}:
        return explicit_status, False
    if explicit_status in {"running", "launching", "launched"}:
        return explicit_status, True
    if run_payload or copy_payload:
        return "running", True
    if explicit_status:
        return explicit_status, False
    return "unknown", False


def assess_mission_remote_gate(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
    run_root: Path | str | None = None,
) -> MissionRemoteGateReport:
    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")

    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    task_class = _normalize(task.get("task_class"))
    formulation_lane = _formulation_lane(task)
    proxy_chain_limit = _proxy_chain_budget_limit(mission)
    (
        prior_consecutive_proxy_tasks,
        prior_local_rehearsal_proxy_tasks,
        budget_rows,
        budget_ledger_path,
    ) = _consecutive_proxy_or_diagnostic_tasks_used(run_root)
    pending_remote_collect_judge, pending_remote_iteration_id = _latest_remote_collect_judge_pending(
        budget_rows
    )
    reason_codes: list[str] = []

    if project:
        prerequisite_gate = assess_research_prerequisites(project, discussion)
        if not prerequisite_gate.can_continue_to_mission_gate:
            reason_codes.extend(prerequisite_gate.reason_codes)

    workspace_profile = _workspace_profile(task)
    if not all(workspace_profile.values()):
        reason_codes.append("missing_workplace_profile")

    if not task_class or task_class not in ALLOWED_MISSION_TASK_CLASSES:
        reason_codes.append("task_class_not_allowlisted")

    if task.get("full_dataset") or _normalize(task.get("scope")) in {"full_dataset", "unbounded"}:
        reason_codes.append("unbounded_or_full_dataset_scope")

    if task.get("formal_claim_requested") or _normalize(task.get("claim_intent")) == "formal_claim":
        reason_codes.append("formal_claim_upgrade_requested")
    if pending_remote_collect_judge:
        reason_codes.append("prior_remote_collect_judge_incomplete")

    discussion_evidence_pack = task.get("discussion_evidence_pack")
    if not isinstance(discussion_evidence_pack, dict):
        reason_codes.append("discussion_evidence_pack_missing")
        evidence_gap = {
            "type": "evidence_gap",
            "minimum_task": "materialize discussion_evidence_pack before mission remote rehearsal",
            "missing_fields": list(DISCUSSION_EVIDENCE_PACK_REQUIRED_FIELDS),
        }
    else:
        missing_evidence_fields = _missing_discussion_evidence_fields(discussion_evidence_pack)
        evidence_gap = {
            "type": "evidence_gap",
            "minimum_task": "fill missing discussion_evidence_pack fields before mission remote rehearsal",
            "missing_fields": missing_evidence_fields,
        }
        if missing_evidence_fields:
            reason_codes.append("discussion_evidence_pack_incomplete")

    formulation = task.get("formulation_faithfulness")
    if not isinstance(formulation, dict):
        reason_codes.append("formulation_faithfulness_missing")
    else:
        missing_formulation_fields = _missing_formulation_fields(formulation)
        if missing_formulation_fields:
            reason_codes.append("formulation_faithfulness_incomplete")
        lane = _normalize(formulation.get("canonical_or_diagnostic")).lower()
        proxy_budget_used = _task_proxy_budget_used(task)
        proxy_budget_limit = int(task.get("proxy_budget_limit") or mission.get("proxy_budget_limit") or 2)
        if lane == "diagnostic" and proxy_budget_used >= proxy_budget_limit:
            reason_codes.append("proxy_budget_exhausted")
        if lane == "diagnostic" and prior_consecutive_proxy_tasks >= proxy_chain_limit:
            reason_codes.append("proxy_chain_budget_exhausted")
        if lane not in {"canonical", "diagnostic"}:
            reason_codes.append("canonical_or_diagnostic_invalid")

    bucket = build_b14_l74_bounded_validation_bucket()
    collect_judge_contract_present = task_class == "bounded_validation_bucket" and bool(bucket.expected_outputs)
    if not collect_judge_contract_present:
        reason_codes.append("collect_judge_contract_missing")

    status = "allow" if not reason_codes else "block"
    reason_text = (
        "mission remote local rehearsal is allowed"
        if status == "allow"
        else "mission remote local rehearsal is blocked by conservative gate checks"
    )
    return MissionRemoteGateReport(
        mission_id=mission_id,
        task_id=task_id,
        status=status,
        can_rehearse_locally=status == "allow",
        reason_codes=sorted(set(reason_codes)),
        reason_text=reason_text,
        task_class=task_class,
        workspace_profile=workspace_profile,
        collect_judge_contract_present=collect_judge_contract_present,
        discussion_evidence_pack_present=isinstance(discussion_evidence_pack, dict)
        and not _missing_discussion_evidence_fields(discussion_evidence_pack),
        formulation_faithfulness_present=isinstance(formulation, dict)
        and not _missing_formulation_fields(formulation),
        evidence_gap=evidence_gap,
        budget_ledger_path=str(budget_ledger_path) if run_root and budget_ledger_path.exists() else "",
        prior_budget_ledger_entries=len(budget_rows),
        prior_consecutive_proxy_or_diagnostic_tasks=prior_consecutive_proxy_tasks
        if formulation_lane == "diagnostic"
        else 0,
        prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks=prior_local_rehearsal_proxy_tasks
        if formulation_lane == "diagnostic"
        else 0,
        prior_remote_collect_judge_pending=pending_remote_collect_judge,
        prior_pending_remote_iteration_id=pending_remote_iteration_id,
        proxy_or_diagnostic_budget_limit=proxy_chain_limit,
    )


def _ensure_fake_collect_bundle(target_dir: Path, mode: str) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.joinpath("stdout.log").write_text("dry-run stdout\n", encoding="utf-8")
    target_dir.joinpath("stderr.log").write_text("", encoding="utf-8")

    if mode in {"artifact_complete", "repair_once", "pivot"}:
        target_dir.joinpath("bbox_predictions.jsonl").write_text(
            json.dumps({"image_id": "img-001", "bbox": [1, 2, 3, 4]}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if mode in {"artifact_complete", "pivot"}:
        target_dir.joinpath("metrics.json").write_text(
            json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if mode == "artifact_complete":
        target_dir.joinpath("evidence_payload.jsonl").write_text(
            json.dumps({"image_id": "img-001", "evidence": "sidecar-present"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        target_dir.joinpath("decision_summary.json").write_text(
            json.dumps({"status": "claim_limited_complete"}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        target_dir.joinpath("action_requests.json").write_text("[]\n", encoding="utf-8")
    elif mode == "repair_once":
        target_dir.joinpath("evidence_payload.jsonl").write_text(
            json.dumps({"image_id": "img-001", "evidence": "sidecar-present"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    elif mode == "pivot":
        target_dir.joinpath("decision_summary.json").write_text(
            json.dumps({"status": "missing_sidecar_evidence"}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        target_dir.joinpath("action_requests.json").write_text("[]\n", encoding="utf-8")


def _iteration_dir(run_root: Path, iteration_index: int) -> Path:
    return run_root / f"iteration_{iteration_index:03d}"


def _next_iteration_index(run_root: Path) -> int:
    existing = []
    if run_root.exists():
        for child in run_root.iterdir():
            if child.is_dir() and child.name.startswith("iteration_"):
                suffix = child.name.split("_", 1)[-1]
                if suffix.isdigit():
                    existing.append(int(suffix))
    return (max(existing) + 1) if existing else 1


def _continuation_decision_from_judge(
    mission_id: str,
    task_id: str,
    judge_report: dict[str, Any],
    claim_limits: list[str],
) -> MissionRemoteContinuationDecision:
    judge_status = _normalize(judge_report.get("status"))
    if judge_status == "artifact_complete":
        next_action = "continue"
        reason_codes = ["collect_judge_artifact_complete"]
        reason_text = "bounded collect/judge bundle is complete; continue with bounded evidence only"
        information_gain = {
            "execution_path": 2,
            "evidence_quality": 2,
            "uncertainty_reduction": 2,
            "direction_decision": 2,
            "failure_explanation": 1,
        }
    elif judge_status == "repair_once":
        next_action = "repair_once_then_rerun"
        reason_codes = ["collect_judge_repair_once"]
        reason_text = "collect/judge found recoverable artifact gaps; one deterministic repair may rerun"
        information_gain = {
            "execution_path": 1,
            "evidence_quality": 1,
            "uncertainty_reduction": 1,
            "direction_decision": 1,
            "failure_explanation": 2,
        }
    elif judge_status == "pivot":
        next_action = "pivot"
        reason_codes = ["collect_judge_pivot"]
        reason_text = "sidecar mechanism evidence is missing or invalid; pivot without claim upgrade"
        information_gain = {
            "execution_path": 1,
            "evidence_quality": 1,
            "uncertainty_reduction": 2,
            "direction_decision": 2,
            "failure_explanation": 2,
        }
    else:
        next_action = "stop"
        reason_codes = ["collect_judge_stop"]
        reason_text = "no collectable bounded validation evidence was produced; stop and redesign locally"
        information_gain = {
            "execution_path": 0,
            "evidence_quality": 0,
            "uncertainty_reduction": 1,
            "direction_decision": 1,
            "failure_explanation": 2,
        }
    return MissionRemoteContinuationDecision(
        mission_id=mission_id,
        task_id=task_id,
        status="ready",
        next_action=next_action,
        reason_codes=reason_codes,
        reason_text=reason_text,
        judge_status=judge_status,
        claim_limits=claim_limits,
        information_gain=information_gain,
    )


def _information_gain_summary(
    mission_id: str,
    task_id: str,
    decision: MissionRemoteContinuationDecision,
) -> dict[str, Any]:
    components = {
        key: int(value)
        for key, value in decision.information_gain.items()
        if isinstance(value, int)
    }
    total_score = sum(components.values())
    if total_score <= 3:
        routing = "stop_or_redesign"
    elif total_score <= 7:
        routing = "weak_but_useful_followup_or_failure_characterization"
    elif total_score <= 11:
        routing = "useful_bounded_evidence_continue_or_expand"
    else:
        routing = "strong_bounded_evidence_no_formal_claim_upgrade"
    return {
        "schema_version": 1,
        "artifact_type": "mission_remote_information_gain",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": decision.status,
        "judge_status": decision.judge_status,
        "next_action": decision.next_action,
        "components": components,
        "total_score": total_score,
        "routing": routing,
        "claim_limits": list(decision.claim_limits),
    }


def _factual_memory_append_entry(
    *,
    mission_id: str,
    task_id: str,
    iteration_id: str,
    decision_id: str,
    task_payload: dict[str, Any],
    project_knowledge: dict[str, Any],
    gate: MissionRemoteGateReport,
    decision: MissionRemoteContinuationDecision,
    information_gain_payload: dict[str, Any],
) -> dict[str, Any]:
    evidence_refs = project_knowledge.get("evidence_refs") if isinstance(project_knowledge, dict) else []
    return {
        "schema_version": 1,
        "artifact_type": "mission_remote_factual_memory_append",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration_id": iteration_id,
        "decision_id": decision_id,
        "task_class": _normalize(task_payload.get("task_class")),
        "canonical_or_diagnostic": _formulation_lane(task_payload),
        "gate_status": gate.status,
        "judge_status": decision.judge_status,
        "continuation_status": decision.status,
        "next_action": decision.next_action,
        "claim_limits": list(decision.claim_limits),
        "information_gain": information_gain_payload,
        "summary": _normalize(decision.reason_text)
        or f"{decision.judge_status or 'unknown_judge'} -> {decision.next_action or 'unknown_action'}",
        "source_evidence_refs": [
            {
                "ref": _normalize(item.get("ref")),
                "authority": _normalize(item.get("authority") or "unknown"),
            }
            for item in evidence_refs
            if isinstance(item, dict) and _normalize(item.get("ref"))
        ],
        "side_effects": gate.side_effects,
    }


def _mission_remote_kb_update_payload(
    *,
    mission_id: str,
    task_id: str,
    iteration_id: str,
    decision_id: str,
    task_payload: dict[str, Any],
    discussion_payload: dict[str, Any],
    project_knowledge: dict[str, Any],
    gate: MissionRemoteGateReport,
    judge_report: dict[str, Any],
    decision: MissionRemoteContinuationDecision,
) -> dict[str, Any]:
    evidence_refs = project_knowledge.get("evidence_refs") if isinstance(project_knowledge, dict) else []
    source_evidence = [
        {
            "evidence_id": f"{iteration_id}_{index + 1}",
            "evidence_type": "project_evidence_ref",
            "summary": _normalize(item.get("ref")),
            "authority": _normalize(item.get("authority") or "unknown"),
        }
        for index, item in enumerate(evidence_refs)
        if isinstance(item, dict) and _normalize(item.get("ref"))
    ]
    if not source_evidence:
        source_evidence = [
            {
                "evidence_id": f"{iteration_id}_local_rehearsal",
                "evidence_type": "local_rehearsal",
                "summary": f"{task_id} {decision.judge_status or 'judge'} -> {decision.next_action or 'next_action'}",
                "authority": "local_iteration_artifact",
            }
        ]

    current_state = project_knowledge.get("current_state") if isinstance(project_knowledge, dict) else {}
    discussion_pack = task_payload.get("discussion_evidence_pack")
    if not isinstance(discussion_pack, dict):
        discussion_pack = {}
    decision_fingerprint_payload = {
        "decision_id": decision_id,
        "judge_status": decision.judge_status,
        "next_action": decision.next_action,
        "claim_limits": list(decision.claim_limits),
        "reason_text": decision.reason_text,
    }
    decision_fingerprint = hashlib.sha256(
        json.dumps(decision_fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "proposal_id": f"{mission_id}__{iteration_id}__{decision_fingerprint}",
        "source_kind": "mission_remote_rehearsal",
        "source_id": decision_id,
        "source_evidence": source_evidence,
        "observations": {
            "current_mainline": {
                "mainline": _normalize(current_state.get("mainline"))
                or _normalize(project_knowledge.get("current_mainline"))
                or _normalize(discussion_pack.get("canonical_formulation"))
                or "mission_remote_rehearsal_current_mainline",
                "status": "active_direction" if gate.status == "allow" else "blocked_direction",
                "known_bottleneck": _normalize(discussion_payload.get("key_uncertainty"))
                or _normalize(decision.reason_text)
                or _normalize(judge_report.get("status")),
                "next_safe_action": (
                    f"Use local mission continuation decision: {decision.next_action}."
                    if decision.next_action
                    else "Resolve mission gate blockers before another mission iteration."
                ),
                "forbidden_escalations": [
                    "formal_claim_upgrade",
                    "real_remote_launch_without_m3_approval",
                    "scheduler_dispatch",
                ],
            },
            "smoke_result": {
                "subject": f"{task_id}_{iteration_id}",
                "can_claim": [
                    "local_rehearsal_path_materialized",
                    "claim_limited_collect_judge_routing_recorded",
                ],
                "cannot_claim": list(dict.fromkeys(list(decision.claim_limits) + ["final_method_success"])),
            },
            "open_questions": _normalize_string_list(
                discussion_pack.get("open_uncertainties") or discussion_payload.get("open_uncertainties")
            ),
        },
    }


def _decision_id(mission_id: str, task_id: str, iteration_index: int, next_action: str) -> str:
    safe_task_id = _normalize(task_id).replace(" ", "_").replace("/", "_").replace("\\", "_")
    safe_action = _normalize(next_action or "unknown_action").replace(" ", "_")
    return f"{mission_id}:{safe_task_id}:iteration_{iteration_index:03d}:{safe_action}"


def _mission_remote_preflight_payload(
    mission_id: str,
    task_id: str,
    gate: MissionRemoteGateReport,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "mission_remote_launch_preflight",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "pass" if gate.can_rehearse_locally else "blocked",
        "reason_codes": gate.reason_codes,
        "workplace_profile": gate.workspace_profile,
        "collect_judge_contract_present": gate.collect_judge_contract_present,
        "discussion_evidence_pack_present": gate.discussion_evidence_pack_present,
        "formulation_faithfulness_present": gate.formulation_faithfulness_present,
        "evidence_gap": gate.evidence_gap,
        "side_effects": gate.side_effects,
        "declared_only": True,
    }


def assess_local_mission_remote_preflight(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
    run_root: Path | str | None = None,
) -> dict[str, Any]:
    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")

    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    gate = assess_mission_remote_gate(
        mission,
        task,
        project_knowledge=project,
        discussion_payload=discussion,
        run_root=run_root,
    )
    return {
        "status": "ready",
        "mission_id": mission_id,
        "task_id": task_id,
        "gate": gate.model_dump(mode="json"),
        "preflight": _mission_remote_preflight_payload(mission_id, task_id, gate),
        "side_effects": gate.side_effects,
    }


def write_local_mission_remote_preflight(
    mission_path: Path | str,
    task_path: Path | str,
    *,
    output_dir: Path | str,
    project_knowledge_path: Path | str | None = None,
    discussion_payload_path: Path | str | None = None,
    run_root: Path | str | None = None,
) -> dict[str, Any]:
    mission_payload = _read_json(Path(mission_path))
    task_payload = _read_json(Path(task_path))
    project_knowledge = _read_json(Path(project_knowledge_path)) if project_knowledge_path else {}
    discussion_payload = _read_json(Path(discussion_payload_path)) if discussion_payload_path else {}
    mission_id = _normalize(mission_payload.get("mission_id") or mission_payload.get("id") or "unknown_mission")
    result = assess_local_mission_remote_preflight(
        mission_payload,
        task_payload,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
        run_root=run_root
        or continuation_root() / "outputs" / "night_runs" / mission_id,
    )
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    gate_path = output_path / "mission_gate_report.yaml"
    preflight_path = output_path / "remote_launch_preflight.yaml"
    _write_yaml(gate_path, dict(result["gate"]))
    _write_yaml(preflight_path, dict(result["preflight"]))
    return {
        **result,
        "status": "written",
        "output_dir": str(output_path),
        "mission_gate_report_path": str(gate_path),
        "remote_launch_preflight_path": str(preflight_path),
    }


def run_local_mission_remote_rehearsal(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    run_root: Path | str,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")

    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    root_dir = Path(run_root)
    root_dir.mkdir(parents=True, exist_ok=True)
    iteration_index = _next_iteration_index(root_dir)
    iteration_dir = _iteration_dir(root_dir, iteration_index)
    iteration_dir.mkdir(parents=True, exist_ok=True)

    gate = assess_mission_remote_gate(
        mission,
        task,
        project_knowledge=project,
        discussion_payload=discussion,
        run_root=root_dir,
    )
    decision_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_decision",
        "mission_id": mission_id,
        "task_id": task_id,
        "task_class": _normalize(task.get("task_class")),
        "status": gate.status,
        "allowed_to_preflight": gate.can_rehearse_locally,
        "claim_limits": _normalize_string_list(task.get("discussion_evidence_pack", {}).get("claim_limits")),
        "side_effects": gate.side_effects,
    }
    _write_yaml(iteration_dir / "decision.yaml", decision_payload)
    _write_yaml(iteration_dir / "mission_gate_report.yaml", gate.model_dump(mode="json"))

    preflight_payload = _mission_remote_preflight_payload(mission_id, task_id, gate)
    _write_yaml(iteration_dir / "remote_launch_preflight.yaml", preflight_payload)

    launch_record_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_launch_record",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "dry_run_declared_only",
        "executed": False,
        "task_class": _normalize(task.get("task_class")),
        "side_effects_declared": gate.side_effects,
        "notes": [
            "Local rehearsal only.",
            "No SSH, scheduler dispatch, or registry mutation executed.",
        ],
    }
    _write_yaml(iteration_dir / "launch_record.yaml", launch_record_payload)
    wait_report_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_wait_report",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "bounded_wait_skipped_local_dry_run",
        "executed": False,
        "reason_codes": ["local_rehearsal_declared_only"],
        "side_effects": gate.side_effects,
    }
    _write_yaml(iteration_dir / "wait_report.yaml", wait_report_payload)

    collect_dir = iteration_dir / "fake_collect_output"
    _ensure_fake_collect_bundle(
        collect_dir,
        _normalize(task.get("fake_collect_mode") or "artifact_complete"),
    )
    collect_report_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_collect_report",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "collect_ready" if gate.can_rehearse_locally else "collect_skipped",
        "collect_output_dir": str(collect_dir),
        "side_effects": gate.side_effects,
    }
    _write_yaml(iteration_dir / "collect_report.yaml", collect_report_payload)

    judge_report = write_b14_l74_artifact_readiness_report(collect_dir)
    _write_yaml(iteration_dir / "judge_report.yaml", judge_report)

    claim_limits = _normalize_string_list(task.get("discussion_evidence_pack", {}).get("claim_limits"))
    if not claim_limits:
        claim_limits = ["proxy_evidence_only", "no_formal_claim_upgrade"]
    continuation_decision = _continuation_decision_from_judge(
        mission_id,
        task_id,
        judge_report,
        claim_limits,
    )
    if gate.status != "allow":
        continuation_decision = MissionRemoteContinuationDecision(
            mission_id=mission_id,
            task_id=task_id,
            status="blocked",
            next_action="ask_human",
            reason_codes=gate.reason_codes,
            reason_text=gate.reason_text,
            judge_status="skipped",
            claim_limits=claim_limits,
            information_gain={
                "execution_path": 0,
                "evidence_quality": 0,
                "uncertainty_reduction": 0,
                "direction_decision": 0,
                "failure_explanation": 1,
            },
        )
    _write_yaml(iteration_dir / "continuation_decision.yaml", continuation_decision.model_dump(mode="json"))
    information_gain_payload = _information_gain_summary(
        mission_id,
        task_id,
        continuation_decision,
    )
    _write_yaml(iteration_dir / "information_gain.yaml", information_gain_payload)

    decision_id = _decision_id(
        mission_id,
        task_id,
        iteration_index,
        continuation_decision.next_action,
    )
    mission_state_payload = mission_state_skeleton(
        mission_id=mission_id,
        current_iteration=iteration_index,
        total_evidence_runs=iteration_index,
        active_decision_count=iteration_index,
        parent_decision_id=decision_id,
    )
    mission_state_payload["last_decision_id"] = decision_id
    mission_state_payload["claim_limits"] = claim_limits
    mission_checkpoint_payload = mission_checkpoint_skeleton(
        mission_id=mission_id,
        decision_id=decision_id,
    )
    mission_checkpoint_payload["claim_limits"] = claim_limits
    mission_state_path = root_dir / "mission_state" / "state.json"
    checkpoint_path = root_dir / "checkpoint" / f"iteration_{iteration_index:03d}.json"
    _write_json(mission_state_path, mission_state_payload)
    _write_json(checkpoint_path, mission_checkpoint_payload)
    factual_memory_entry = _factual_memory_append_entry(
        mission_id=mission_id,
        task_id=task_id,
        iteration_id=iteration_dir.name,
        decision_id=decision_id,
        task_payload=task,
        project_knowledge=project,
        gate=gate,
        decision=continuation_decision,
        information_gain_payload=information_gain_payload,
    )
    factual_memory_path = iteration_dir / "factual_memory_append.jsonl"
    _append_jsonl(factual_memory_path, factual_memory_entry)
    kb_update_payload = build_knowledge_update_proposal_dry_run(
        _mission_remote_kb_update_payload(
            mission_id=mission_id,
            task_id=task_id,
            iteration_id=iteration_dir.name,
            decision_id=decision_id,
            task_payload=task,
            discussion_payload=discussion,
            project_knowledge=project,
            gate=gate,
            judge_report=judge_report,
            decision=continuation_decision,
        )
    )
    kb_update_written = write_knowledge_update_proposal_dry_run(kb_update_payload)
    local_kb_update_dir = iteration_dir / "kb_update_proposal"
    local_kb_update_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(local_kb_update_dir / "kb_update_manifest.yaml", kb_update_payload)
    _write_yaml(
        local_kb_update_dir / "proposal_pointer.yaml",
        {
            "schema_version": 1,
            "artifact_type": "mission_remote_kb_update_pointer",
            "proposal_id": _normalize(kb_update_written.get("proposal_id") or kb_update_payload.get("proposal_id")),
            "status": _normalize(kb_update_written.get("status") or kb_update_payload.get("status")),
            "created": bool(kb_update_written.get("created")),
            "replayed": bool(kb_update_written.get("replayed")),
            "proposal_dir": _normalize(kb_update_written.get("proposal_dir")),
        },
    )

    budget_update_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_budget_update",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration": iteration_index,
        "execution_scope": "local_rehearsal",
        "executed": False,
        "local_rehearsals_total_used": iteration_index,
        "remote_tasks_total_used": 0,
        "proxy_or_diagnostic_tasks_used": _task_proxy_budget_used(task),
        "canonical_or_diagnostic": _formulation_lane(task),
        "consecutive_proxy_or_diagnostic_tasks_used": (
            gate.prior_consecutive_proxy_or_diagnostic_tasks
            if _formulation_lane(task) == "diagnostic"
            else 0
            if _formulation_lane(task) == "canonical"
            else gate.prior_consecutive_proxy_or_diagnostic_tasks
        ),
        "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": (
            gate.prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks + 1
            if gate.status == "allow" and _formulation_lane(task) == "diagnostic"
            else 0
            if gate.status == "allow" and _formulation_lane(task) == "canonical"
            else gate.prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks
        ),
        "next_action": continuation_decision.next_action,
        "judge_status": continuation_decision.judge_status,
    }
    _write_yaml(iteration_dir / "budget_update.yaml", budget_update_payload)
    _append_jsonl(root_dir / "budget_ledger.jsonl", budget_update_payload)
    observation_result = append_continuation_observation(
        run_id=mission_id,
        stage="mission_remote_rehearsal",
        actor="mission_remote_loop",
        status=continuation_decision.status,
        idempotency_key=f"mission_remote_rehearsal:{decision_id}",
        source_task_id=task_id,
        artifact_path=str(iteration_dir / "continuation_decision.yaml"),
        reasons=continuation_decision.reason_codes or gate.reason_codes,
        payload={
            "mission_id": mission_id,
            "task_id": task_id,
            "iteration_id": iteration_dir.name,
            "gate_status": gate.status,
            "judge_status": continuation_decision.judge_status,
            "next_action": continuation_decision.next_action,
            "claim_limits": claim_limits,
            "checkpoint_path": str(checkpoint_path),
            "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
            "information_gain": information_gain_payload,
            "factual_memory_path": str(factual_memory_path),
            "kb_update_proposal_dir": str(local_kb_update_dir),
        },
    )

    return {
        "status": "written",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration_dir": str(iteration_dir),
        "gate": gate.model_dump(mode="json"),
        "judge_report": judge_report,
        "continuation_decision": continuation_decision.model_dump(mode="json"),
        "wait_report": wait_report_payload,
        "information_gain": information_gain_payload,
        "mission_state_path": str(mission_state_path),
        "checkpoint_path": str(checkpoint_path),
        "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
        "factual_memory_path": str(factual_memory_path),
        "kb_update_proposal_dir": str(local_kb_update_dir),
        "kb_update_proposal": kb_update_written,
        "continuation_observation": observation_result,
        "side_effects": gate.side_effects,
    }


def write_local_mission_remote_rehearsal(
    mission_path: Path | str,
    task_path: Path | str,
    *,
    run_root: Path | str,
    project_knowledge_path: Path | str | None = None,
    discussion_payload_path: Path | str | None = None,
) -> dict[str, Any]:
    mission_payload = _read_json(Path(mission_path))
    task_payload = _read_json(Path(task_path))
    project_knowledge = _read_json(Path(project_knowledge_path)) if project_knowledge_path else {}
    discussion_payload = _read_json(Path(discussion_payload_path)) if discussion_payload_path else {}
    return run_local_mission_remote_rehearsal(
        mission_payload,
        task_payload,
        run_root=run_root,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
    )


def assess_mission_remote_launch_packet(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    run_root: Path | str | None = None,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
    remote_task_id: str | None = None,
    phase: str = "execute_run",
    detach: bool = True,
) -> dict[str, Any]:
    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")
    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    task_class = _normalize(task.get("task_class"))
    gate = assess_mission_remote_gate(
        mission,
        task,
        project_knowledge=project,
        discussion_payload=discussion,
        run_root=run_root,
    )
    resolved_profile = _resolve_mission_workplace_profile(task)
    reason_codes = list(gate.reason_codes)
    reason_codes.extend(_normalize_string_list(resolved_profile.get("reasons")))
    transport_snapshot = _latest_remote_transport_snapshot(resolved_profile)
    remote_dispatch_task_id = _remote_dispatch_task_id(task, override_task_id=remote_task_id)
    live_launch_blockers: list[str] = []
    if not remote_dispatch_task_id:
        live_launch_blockers.append("remote_task_id_missing")
    if task_class == "bounded_validation_bucket":
        bucket = build_b14_l74_bounded_validation_bucket()
        expected_outputs = list(bucket.expected_outputs)
    else:
        expected_outputs = []
    launch_allowed = gate.status == "allow" and _normalize(resolved_profile.get("status")) == "resolved"
    packet = (
        _mission_remote_launch_packet_payload(
            mission_id=mission_id,
            task_id=task_id,
            task_class=task_class,
            workplace_profile=resolved_profile,
            remote_dispatch_task_id=remote_dispatch_task_id,
            phase=phase,
            detach=detach,
        )
        if launch_allowed
        else {}
    )
    live_launch_ready = launch_allowed and not live_launch_blockers
    preflight = _mission_remote_preflight_payload(mission_id, task_id, gate)
    preflight["status"] = "pass" if launch_allowed else "blocked"
    preflight["declared_only"] = True
    preflight["allow_remote_launch"] = launch_allowed
    preflight["remote_dispatch_task_id"] = remote_dispatch_task_id
    preflight["live_launch_ready"] = live_launch_ready
    preflight["live_launch_blockers"] = list(live_launch_blockers)
    preflight["latest_remote_transport"] = transport_snapshot
    preflight["resolved_workplace_profile"] = {
        "status": _normalize(resolved_profile.get("status")),
        "profile": _normalize(resolved_profile.get("profile")),
        "host": _normalize(resolved_profile.get("host")),
        "remote_root": _normalize(resolved_profile.get("remote_root")),
        "remote_workdir": _normalize(resolved_profile.get("remote_workdir")),
        "source": _normalize(resolved_profile.get("source")),
        "mismatches": resolved_profile.get("mismatches", []),
        "reasons": _normalize_string_list(resolved_profile.get("reasons")),
    }
    launch_record = {
        "schema_version": 1,
        "artifact_type": "mission_remote_launch_record",
        "mission_id": mission_id,
        "task_id": task_id,
        "task_class": task_class,
        "status": "command_packet_ready" if launch_allowed else "blocked",
        "executed": False,
        "execution_scope": "remote_task",
        "dry_run": True,
        "launch_packet_ready": bool(packet),
        "remote_dispatch_task_id": remote_dispatch_task_id,
        "live_launch_ready": live_launch_ready,
        "live_launch_blockers": list(live_launch_blockers),
        "latest_remote_transport": transport_snapshot,
        "command_packet_only": True,
        "required_collect_judge_contract": expected_outputs,
        "reasons": sorted(set(reason_codes)),
        "side_effects": packet.get("side_effects") if packet else gate.side_effects,
        "intended_command_side_effects_if_executed": (
            packet.get("intended_command_side_effects_if_executed") if packet else {}
        ),
    }
    return {
        "status": "ready" if launch_allowed else "blocked",
        "mission_id": mission_id,
        "task_id": task_id,
        "task_class": task_class,
        "gate": gate.model_dump(mode="json"),
        "preflight": preflight,
        "resolved_workplace_profile": resolved_profile,
        "launch_packet": packet,
        "launch_record": launch_record,
        "required_collect_judge_contract": expected_outputs,
        "remote_dispatch_task_id": remote_dispatch_task_id,
        "live_launch_ready": live_launch_ready,
        "live_launch_blockers": list(live_launch_blockers),
        "latest_remote_transport": transport_snapshot,
        "reason_codes": sorted(set(reason_codes)),
    }


def write_mission_remote_launch_packet(
    mission_path: Path | str,
    task_path: Path | str,
    *,
    output_dir: Path | str,
    run_root: Path | str | None = None,
    project_knowledge_path: Path | str | None = None,
    discussion_payload_path: Path | str | None = None,
    remote_task_id: str | None = None,
    phase: str = "execute_run",
    detach: bool = True,
) -> dict[str, Any]:
    mission_payload = _read_json(Path(mission_path))
    task_payload = _read_json(Path(task_path))
    project_knowledge = _read_json(Path(project_knowledge_path)) if project_knowledge_path else {}
    discussion_payload = _read_json(Path(discussion_payload_path)) if discussion_payload_path else {}
    mission_id = _normalize(mission_payload.get("mission_id") or mission_payload.get("id") or "unknown_mission")
    packet_result = assess_mission_remote_launch_packet(
        mission_payload,
        task_payload,
        run_root=run_root or continuation_root() / "outputs" / "night_runs" / mission_id,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
        remote_task_id=remote_task_id,
        phase=phase,
        detach=detach,
    )
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    gate_path = output_path / "mission_gate_report.yaml"
    preflight_path = output_path / "remote_launch_preflight.yaml"
    launch_record_path = output_path / "launch_record.yaml"
    _write_yaml(gate_path, dict(packet_result["gate"]))
    _write_yaml(preflight_path, dict(packet_result["preflight"]))
    _write_yaml(launch_record_path, dict(packet_result["launch_record"]))

    launch_packet_path = output_path / "launch_packet.json"
    launch_packet_markdown_path = output_path / "launch_packet.md"
    if packet_result["launch_packet"]:
        _write_json(launch_packet_path, dict(packet_result["launch_packet"]))
        launch_packet_markdown_path.write_text(
            "\n".join(
                [
                    "# Mission Remote Launch Packet",
                    "",
                    f"- mission_id: {packet_result['mission_id']}",
                    f"- task_id: {packet_result['task_id']}",
                    f"- task_class: {packet_result['task_class']}",
                    f"- workplace_profile: {packet_result['resolved_workplace_profile'].get('profile', '')}",
                    f"- status: {packet_result['launch_record']['status']}",
                    f"- live_launch_ready: {str(bool(packet_result['live_launch_ready'])).lower()}",
                    f"- remote_dispatch_task_id: {packet_result['remote_dispatch_task_id'] or '(missing)'}",
                    f"- command_task_id: {packet_result['launch_packet']['command_task_id']}",
                    (
                        f"- live_launch_blockers: {', '.join(packet_result['live_launch_blockers'])}"
                        if packet_result["live_launch_blockers"]
                        else "- live_launch_blockers: none"
                    ),
                    "",
                    "```powershell",
                    packet_result["launch_packet"]["display_command"],
                    "```",
                    "",
                    "This is a dry-run command packet. It does not execute SSH or remote work.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        launch_packet_path.write_text("{}\n", encoding="utf-8")
        launch_packet_markdown_path.write_text(
            "# Mission Remote Launch Packet\n\nBlocked before command packet generation.\n",
            encoding="utf-8",
        )

    return {
        **packet_result,
        "status": "written",
        "output_dir": str(output_path),
        "mission_gate_report_path": str(gate_path),
        "remote_launch_preflight_path": str(preflight_path),
        "launch_record_path": str(launch_record_path),
        "launch_packet_path": str(launch_packet_path),
        "launch_packet_markdown_path": str(launch_packet_markdown_path),
    }


def _remote_dispatch_task_id(task_payload: dict[str, Any], override_task_id: str | None = None) -> str:
    return _normalize(
        override_task_id
        or task_payload.get("remote_task_id")
        or task_payload.get("orchestrator_task_id")
        or task_payload.get("dispatch_task_id")
    )


def execute_mission_remote_launch_once(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    run_root: Path | str,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
    remote_task_id: str | None = None,
    phase: str = "execute_run",
    detach: bool = True,
) -> dict[str, Any]:
    from ..orchestrator import core as lab

    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")
    root_dir = Path(run_root)
    root_dir.mkdir(parents=True, exist_ok=True)
    iteration_index = _next_iteration_index(root_dir)
    iteration_dir = _iteration_dir(root_dir, iteration_index)
    iteration_dir.mkdir(parents=True, exist_ok=True)

    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    packet_result = assess_mission_remote_launch_packet(
        mission,
        task,
        run_root=root_dir,
        project_knowledge=project,
        discussion_payload=discussion,
        remote_task_id=remote_task_id,
        phase=phase,
        detach=detach,
    )
    _write_yaml(iteration_dir / "decision.yaml", {
        "schema_version": 1,
        "artifact_type": "mission_remote_decision",
        "mission_id": mission_id,
        "task_id": task_id,
        "task_class": _normalize(task.get("task_class")),
        "status": packet_result["gate"]["status"],
        "allowed_to_preflight": packet_result["gate"]["can_rehearse_locally"],
        "claim_limits": _normalize_string_list(task.get("discussion_evidence_pack", {}).get("claim_limits")),
        "side_effects": packet_result["gate"]["side_effects"],
    })
    _write_yaml(iteration_dir / "mission_gate_report.yaml", dict(packet_result["gate"]))
    _write_yaml(iteration_dir / "remote_launch_preflight.yaml", dict(packet_result["preflight"]))
    if packet_result["launch_packet"]:
        _write_json(iteration_dir / "launch_packet.json", dict(packet_result["launch_packet"]))

    dispatch_task_id = _remote_dispatch_task_id(task, override_task_id=remote_task_id)
    reason_codes = list(_normalize_string_list(packet_result.get("reason_codes")))
    if not dispatch_task_id:
        reason_codes.append("remote_task_id_missing")
    if packet_result["status"] != "ready" or not dispatch_task_id:
        launch_record = {
            **dict(packet_result["launch_record"]),
            "status": "blocked",
            "executed": False,
            "dry_run": False,
            "command_packet_only": False,
            "reasons": sorted(set(reason_codes)),
        }
        _write_yaml(iteration_dir / "launch_record.yaml", launch_record)
        return {
            **packet_result,
            "status": "blocked",
            "iteration_dir": str(iteration_dir),
            "launch_record": launch_record,
            "launch_record_path": str(iteration_dir / "launch_record.yaml"),
        }

    resolved = packet_result["resolved_workplace_profile"]
    task_registration = _ensure_mission_remote_task_registered(
        mission,
        task,
        dispatch_task_id=dispatch_task_id,
        workplace_profile=resolved,
    )
    launch_result = lab.run_remote(
        dispatch_task_id,
        host=_normalize(resolved.get("host")),
        remote_root=_normalize(resolved.get("remote_root")),
        worker="materialized_execution",
        remote_workdir=_normalize(resolved.get("remote_workdir")),
        detach=detach,
        execution_phase=phase,
    )
    launch_status, executed = _mission_remote_launch_outcome(launch_result)
    remote_tasks_total, local_rehearsals_total = _budget_totals(_read_jsonl(root_dir / "budget_ledger.jsonl"))
    gate_payload = dict(packet_result["gate"])
    lane = _formulation_lane(task)
    budget_update_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_budget_update",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration": iteration_index,
        "iteration_id": iteration_dir.name,
        "execution_scope": "remote_task",
        "executed": executed,
        "collect_judge_completed": False,
        "local_rehearsals_total_used": local_rehearsals_total,
        "remote_tasks_total_used": remote_tasks_total + (1 if executed else 0),
        "proxy_or_diagnostic_tasks_used": _task_proxy_budget_used(task),
        "canonical_or_diagnostic": lane,
        "consecutive_proxy_or_diagnostic_tasks_used": (
            int(gate_payload.get("prior_consecutive_proxy_or_diagnostic_tasks") or 0) + 1
            if executed and lane == "diagnostic"
            else 0
            if executed and lane == "canonical"
            else int(gate_payload.get("prior_consecutive_proxy_or_diagnostic_tasks") or 0)
        ),
        "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": int(
            gate_payload.get("prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks") or 0
        ),
        "launch_status": launch_status,
        "next_action": "collect_and_judge_pending" if executed else "launch_failed",
        "judge_status": "",
        "remote_dispatch_task_id": dispatch_task_id,
        "task_registration": task_registration,
    }
    _write_yaml(iteration_dir / "budget_update.yaml", budget_update_payload)
    _append_jsonl(root_dir / "budget_ledger.jsonl", budget_update_payload)
    launch_record_status = "launched" if executed else launch_status or "launch_failed"
    launch_record = {
        **dict(packet_result["launch_record"]),
        "status": launch_record_status,
        "executed": executed,
        "dry_run": False,
        "command_packet_only": False,
        "remote_dispatch_task_id": dispatch_task_id,
        "launch_status": launch_status,
        "task_registration": task_registration,
        "launch_result": launch_result,
        "reasons": sorted(set(reason_codes)),
    }
    _write_yaml(iteration_dir / "launch_record.yaml", launch_record)
    observation_result = append_continuation_observation(
        run_id=mission_id,
        stage="mission_remote_launch",
        actor="mission_remote_loop",
        status="running" if executed else "blocked",
        idempotency_key=f"mission_remote_launch:{mission_id}:{iteration_dir.name}",
        source_task_id=task_id,
        artifact_path=str(iteration_dir / "launch_record.yaml"),
        reasons=sorted(set(reason_codes)),
        payload={
            "mission_id": mission_id,
            "task_id": task_id,
            "iteration_id": iteration_dir.name,
            "remote_dispatch_task_id": dispatch_task_id,
            "launch_status": launch_status,
            "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
        },
    )
    return {
        **packet_result,
        "status": launch_record_status,
        "iteration_dir": str(iteration_dir),
        "launch_record": launch_record,
        "launch_record_path": str(iteration_dir / "launch_record.yaml"),
        "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
        "budget_update": budget_update_payload,
        "task_registration": task_registration,
        "launch_result": launch_result,
        "continuation_observation": observation_result,
    }


def finalize_mission_remote_collect_judge(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    run_root: Path | str,
    iteration_id: str,
    collect_output_dir: Path | str,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
    sync_pull_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")
    root_dir = Path(run_root)
    iteration_dir = root_dir / iteration_id
    if not iteration_dir.exists():
        raise ValueError(f"iteration_dir missing: {iteration_dir}")
    gate = _read_yaml_dict(iteration_dir / "mission_gate_report.yaml")
    launch_record = _read_yaml_dict(iteration_dir / "launch_record.yaml")
    if not bool(launch_record.get("executed")):
        raise ValueError("cannot finalize collect/judge for a non-executed remote launch")

    mission_id = _normalize(mission.get("mission_id") or mission.get("id") or "unknown_mission")
    task_id = _normalize(task.get("task_id") or "unknown_task")
    collect_dir = Path(collect_output_dir)
    claim_limits = _normalize_string_list(task.get("discussion_evidence_pack", {}).get("claim_limits"))
    if not claim_limits:
        claim_limits = ["proxy_evidence_only", "no_formal_claim_upgrade"]
    wait_report_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_wait_report",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "bounded_wait_complete",
        "executed": True,
        "reason_codes": [],
        "side_effects": launch_record.get("side_effects") or gate.get("side_effects") or {},
    }
    _write_yaml(iteration_dir / "wait_report.yaml", wait_report_payload)
    collect_report_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_collect_report",
        "mission_id": mission_id,
        "task_id": task_id,
        "status": "collect_ready",
        "collect_output_dir": str(collect_dir),
        "sync_pull_result": dict(sync_pull_result or {}),
        "side_effects": launch_record.get("side_effects") or gate.get("side_effects") or {},
    }
    _write_yaml(iteration_dir / "collect_report.yaml", collect_report_payload)
    judge_report = write_b14_l74_artifact_readiness_report(collect_dir)
    _write_yaml(iteration_dir / "judge_report.yaml", judge_report)

    continuation_decision = _continuation_decision_from_judge(
        mission_id,
        task_id,
        judge_report,
        claim_limits,
    )
    _write_yaml(iteration_dir / "continuation_decision.yaml", continuation_decision.model_dump(mode="json"))
    information_gain_payload = _information_gain_summary(
        mission_id,
        task_id,
        continuation_decision,
    )
    _write_yaml(iteration_dir / "information_gain.yaml", information_gain_payload)

    try:
        iteration_index = int(iteration_id.split("_", 1)[-1])
    except (TypeError, ValueError):
        iteration_index = _next_iteration_index(root_dir)
    decision_id = _decision_id(
        mission_id,
        task_id,
        iteration_index,
        continuation_decision.next_action,
    )
    mission_state_payload = mission_state_skeleton(
        mission_id=mission_id,
        current_iteration=iteration_index,
        total_evidence_runs=iteration_index,
        active_decision_count=iteration_index,
        parent_decision_id=decision_id,
    )
    mission_state_payload["last_decision_id"] = decision_id
    mission_state_payload["claim_limits"] = claim_limits
    mission_checkpoint_payload = mission_checkpoint_skeleton(
        mission_id=mission_id,
        decision_id=decision_id,
    )
    mission_checkpoint_payload["claim_limits"] = claim_limits
    mission_state_path = root_dir / "mission_state" / "state.json"
    checkpoint_path = root_dir / "checkpoint" / f"{iteration_id}.json"
    _write_json(mission_state_path, mission_state_payload)
    _write_json(checkpoint_path, mission_checkpoint_payload)
    factual_memory_entry = _factual_memory_append_entry(
        mission_id=mission_id,
        task_id=task_id,
        iteration_id=iteration_dir.name,
        decision_id=decision_id,
        task_payload=task,
        project_knowledge=project,
        gate=MissionRemoteGateReport.model_validate(gate),
        decision=continuation_decision,
        information_gain_payload=information_gain_payload,
    )
    factual_memory_path = iteration_dir / "factual_memory_append.jsonl"
    _append_jsonl(factual_memory_path, factual_memory_entry)
    kb_update_payload = build_knowledge_update_proposal_dry_run(
        _mission_remote_kb_update_payload(
            mission_id=mission_id,
            task_id=task_id,
            iteration_id=iteration_dir.name,
            decision_id=decision_id,
            task_payload=task,
            discussion_payload=discussion,
            project_knowledge=project,
            gate=MissionRemoteGateReport.model_validate(gate),
            judge_report=judge_report,
            decision=continuation_decision,
        )
    )
    kb_update_written = write_knowledge_update_proposal_dry_run(kb_update_payload)
    local_kb_update_dir = iteration_dir / "kb_update_proposal"
    local_kb_update_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(local_kb_update_dir / "kb_update_manifest.yaml", kb_update_payload)
    _write_yaml(
        local_kb_update_dir / "proposal_pointer.yaml",
        {
            "schema_version": 1,
            "artifact_type": "mission_remote_kb_update_pointer",
            "proposal_id": _normalize(kb_update_written.get("proposal_id") or kb_update_payload.get("proposal_id")),
            "status": _normalize(kb_update_written.get("status") or kb_update_payload.get("status")),
            "created": bool(kb_update_written.get("created")),
            "replayed": bool(kb_update_written.get("replayed")),
            "proposal_dir": _normalize(kb_update_written.get("proposal_dir")),
        },
    )
    budget_rows = _read_jsonl(root_dir / "budget_ledger.jsonl")
    remote_tasks_total, local_rehearsals_total = _budget_totals(budget_rows)
    lane = _formulation_lane(task)
    consecutive_proxy_tasks, local_proxy_tasks, _, _ = _consecutive_proxy_or_diagnostic_tasks_used(root_dir)
    budget_finalize_payload = {
        "schema_version": 1,
        "artifact_type": "mission_remote_budget_update",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration": iteration_index,
        "iteration_id": iteration_dir.name,
        "execution_scope": "remote_collect_judge",
        "executed": True,
        "collect_judge_completed": True,
        "local_rehearsals_total_used": local_rehearsals_total,
        "remote_tasks_total_used": remote_tasks_total,
        "proxy_or_diagnostic_tasks_used": _task_proxy_budget_used(task),
        "canonical_or_diagnostic": lane,
        "consecutive_proxy_or_diagnostic_tasks_used": consecutive_proxy_tasks,
        "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": local_proxy_tasks,
        "next_action": continuation_decision.next_action,
        "judge_status": continuation_decision.judge_status,
    }
    _write_yaml(iteration_dir / "budget_update.yaml", budget_finalize_payload)
    _append_jsonl(root_dir / "budget_ledger.jsonl", budget_finalize_payload)
    observation_result = append_continuation_observation(
        run_id=mission_id,
        stage="mission_remote_execution",
        actor="mission_remote_loop",
        status=continuation_decision.status,
        idempotency_key=f"mission_remote_execution:{decision_id}",
        source_task_id=task_id,
        artifact_path=str(iteration_dir / "continuation_decision.yaml"),
        reasons=continuation_decision.reason_codes,
        payload={
            "mission_id": mission_id,
            "task_id": task_id,
            "iteration_id": iteration_dir.name,
            "judge_status": continuation_decision.judge_status,
            "next_action": continuation_decision.next_action,
            "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
            "factual_memory_path": str(factual_memory_path),
            "kb_update_proposal_dir": str(local_kb_update_dir),
            "sync_pull_result": dict(sync_pull_result or {}),
        },
    )
    return {
        "status": "written",
        "mission_id": mission_id,
        "task_id": task_id,
        "iteration_dir": str(iteration_dir),
        "wait_report": wait_report_payload,
        "collect_report": collect_report_payload,
        "judge_report": judge_report,
        "continuation_decision": continuation_decision.model_dump(mode="json"),
        "information_gain": information_gain_payload,
        "mission_state_path": str(mission_state_path),
        "checkpoint_path": str(checkpoint_path),
        "budget_ledger_path": str(root_dir / "budget_ledger.jsonl"),
        "factual_memory_path": str(factual_memory_path),
        "kb_update_proposal_dir": str(local_kb_update_dir),
        "kb_update_proposal": kb_update_written,
        "continuation_observation": observation_result,
    }


def _collect_contract_filenames(task_class: str) -> list[str]:
    if _normalize(task_class) == "bounded_validation_bucket":
        return list(build_b14_l74_bounded_validation_bucket().expected_outputs)
    return []


def _has_required_collect_contract(output_dir: Path, expected_outputs: list[str], *, task_class: str = "") -> bool:
    if not output_dir.exists() or not expected_outputs:
        return False
    if _normalize(task_class) == "bounded_validation_bucket":
        return all(
            any((output_dir / filename).exists() for filename in filenames)
            for filenames in B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS.values()
        )
    return all((output_dir / filename).exists() for filename in expected_outputs)


def _resolve_collect_output_dir_from_sync_result(
    *,
    task_class: str,
    sync_pull_result: dict[str, Any],
) -> dict[str, Any]:
    local_root = Path(_normalize(sync_pull_result.get("local")))
    expected_outputs = _collect_contract_filenames(task_class)
    candidates = [local_root, local_root / "artifacts"] if local_root else []
    checked_candidates: list[str] = []
    for candidate in candidates:
        checked_candidates.append(str(candidate))
        if _has_required_collect_contract(candidate, expected_outputs, task_class=task_class):
            return {
                "status": "resolved",
                "collect_output_dir": str(candidate),
                "expected_outputs": expected_outputs,
                "checked_candidates": checked_candidates,
                "source": "worker_out" if candidate == local_root else "worker_out_artifacts",
            }
    return {
        "status": "unresolved",
        "collect_output_dir": str(local_root) if local_root else "",
        "expected_outputs": expected_outputs,
        "checked_candidates": checked_candidates,
        "source": "default_local_root",
    }


def sync_finalize_mission_remote_iteration(
    mission_payload: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    run_root: Path | str,
    iteration_id: str,
    project_knowledge: dict[str, Any] | None = None,
    discussion_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ..orchestrator import core as lab

    mission = _as_dict(mission_payload, name="mission_payload")
    task = _as_dict(task_payload, name="task_payload")
    project = _as_dict(project_knowledge or {}, name="project_knowledge")
    discussion = _as_dict(discussion_payload or {}, name="discussion_payload")
    root_dir = Path(run_root)
    iteration_dir = root_dir / iteration_id
    if not iteration_dir.exists():
        raise ValueError(f"iteration_dir missing: {iteration_dir}")

    gate_payload = _read_yaml_dict(iteration_dir / "mission_gate_report.yaml")
    launch_record = _read_yaml_dict(iteration_dir / "launch_record.yaml")
    if not bool(launch_record.get("executed")):
        raise ValueError("cannot sync/finalize a non-executed remote launch")

    workspace_profile = gate_payload.get("workspace_profile") if isinstance(gate_payload, dict) else {}
    if not isinstance(workspace_profile, dict):
        workspace_profile = {}
    host = _normalize(workspace_profile.get("host"))
    remote_root = _normalize(workspace_profile.get("remote_root"))
    if not host or not remote_root:
        resolved = _resolve_mission_workplace_profile(task)
        host = host or _normalize(resolved.get("host"))
        remote_root = remote_root or _normalize(resolved.get("remote_root"))
    if not host or not remote_root:
        raise ValueError("cannot sync/finalize without a resolved workplace host and remote_root")

    dispatch_task_id = _normalize(launch_record.get("remote_dispatch_task_id")) or _remote_dispatch_task_id(task)
    if not dispatch_task_id:
        raise ValueError("cannot sync/finalize without a remote dispatch task id")

    sync_pull_result = lab.sync_pull_remote(dispatch_task_id, host=host, remote_root=remote_root)
    collect_dir_resolution = _resolve_collect_output_dir_from_sync_result(
        task_class=_normalize(task.get("task_class")),
        sync_pull_result=sync_pull_result,
    )
    collect_output_dir = _normalize(collect_dir_resolution.get("collect_output_dir"))
    if not collect_output_dir:
        raise ValueError("sync-pull did not return a local collect output directory")

    finalized = finalize_mission_remote_collect_judge(
        mission,
        task,
        run_root=root_dir,
        iteration_id=iteration_id,
        collect_output_dir=collect_output_dir,
        project_knowledge=project,
        discussion_payload=discussion,
        sync_pull_result=sync_pull_result,
    )
    return {
        "status": finalized["status"],
        "mission_id": finalized["mission_id"],
        "task_id": finalized["task_id"],
        "iteration_dir": finalized["iteration_dir"],
        "iteration_id": iteration_id,
        "sync_pull_result": sync_pull_result,
        "collect_output_dir_resolution": collect_dir_resolution,
        "collect_output_dir": collect_output_dir,
        "finalize_result": finalized,
    }


def summarize_local_mission_remote_rehearsal(run_root: Path | str) -> dict[str, Any]:
    root_dir = Path(run_root)
    if not root_dir.exists():
        return {
            "present": False,
            "run_root": str(root_dir),
            "iteration_count": 0,
            "budget_ledger_entries": 0,
        }

    iterations = sorted(
        [
            child
            for child in root_dir.iterdir()
            if child.is_dir() and child.name.startswith("iteration_")
        ],
        key=lambda item: item.name,
    )
    latest_iteration = iterations[-1] if iterations else None
    actionable_iteration, actionable_payloads = _latest_actionable_iteration(iterations)
    latest_decision = actionable_payloads["decision"]
    latest_judge = actionable_payloads["judge"]
    latest_gate = actionable_payloads["gate"]
    latest_information_gain = actionable_payloads["information_gain"]
    latest_kb_update_pointer = actionable_payloads["kb_update"]
    mission_state: dict[str, Any] = {}

    mission_state_path = root_dir / "mission_state" / "state.json"
    if mission_state_path.exists():
        try:
            mission_state = _read_json(mission_state_path)
        except (ValueError, json.JSONDecodeError):
            mission_state = {}

    checkpoint_dir = root_dir / "checkpoint"
    checkpoint_files = sorted(checkpoint_dir.glob("*.json")) if checkpoint_dir.exists() else []
    latest_checkpoint_path = checkpoint_files[-1] if checkpoint_files else None
    latest_checkpoint: dict[str, Any] = {}
    if latest_checkpoint_path is not None:
        try:
            latest_checkpoint = _read_json(latest_checkpoint_path)
        except (ValueError, json.JSONDecodeError):
            latest_checkpoint = {}

    budget_ledger_path = root_dir / "budget_ledger.jsonl"
    budget_rows = _read_jsonl(budget_ledger_path)
    latest_budget, latest_budget_audit, latest_budget_uses_fallback = _latest_effective_budget_row(
        budget_rows
    )
    pending_remote_collect_judge, pending_remote_iteration_id = _latest_remote_collect_judge_pending(
        budget_rows
    )
    followup_state: dict[str, Any] = {
        "status": "",
        "can_write_proposal": False,
        "candidate_count": 0,
        "reasons": [],
        "next_action": "",
        "latest_canonical_or_diagnostic": "",
        "latest_consecutive_proxy_or_diagnostic_tasks_used": 0,
        "latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
        "proxy_or_diagnostic_budget_limit": 0,
    }
    if pending_remote_collect_judge:
        followup_state = {
            **followup_state,
            "status": "blocked",
            "reasons": ["prior_remote_collect_judge_incomplete"],
        }
    elif actionable_iteration is not None and (latest_gate or latest_decision):
        followup_state = _assess_followup_candidate_state(
            mission_id=_normalize(latest_decision.get("mission_id")),
            task_id=_normalize(latest_decision.get("task_id")),
            latest_iteration_id=actionable_iteration.name,
            gate_payload=latest_gate,
            decision_payload=latest_decision,
            latest_lane=_normalize(latest_budget.get("canonical_or_diagnostic")).lower(),
            latest_consecutive_proxy_tasks=int(
                latest_budget.get("consecutive_proxy_or_diagnostic_tasks_used") or 0
            ),
            latest_local_rehearsal_proxy_tasks=int(
                latest_budget.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
            ),
            proxy_chain_limit=int(latest_gate.get("proxy_or_diagnostic_budget_limit") or 0),
        )
    latest_transport_snapshot = _latest_remote_transport_snapshot(
        latest_gate.get("workspace_profile") if isinstance(latest_gate.get("workspace_profile"), dict) else {}
    )

    return {
        "present": bool(iterations or budget_rows),
        "run_root": str(root_dir),
        "iteration_count": len(iterations),
        "latest_iteration_dir": str(latest_iteration) if latest_iteration else "",
        "latest_iteration_id": latest_iteration.name if latest_iteration else "",
        "latest_actionable_iteration_dir": str(actionable_iteration) if actionable_iteration else "",
        "latest_actionable_iteration_id": actionable_iteration.name if actionable_iteration else "",
        "budget_ledger_path": str(budget_ledger_path) if budget_ledger_path.exists() else "",
        "budget_ledger_entries": len(budget_rows),
        "latest_remote_collect_judge_pending": pending_remote_collect_judge,
        "latest_pending_remote_iteration_id": pending_remote_iteration_id,
        "latest_canonical_or_diagnostic": _normalize(latest_budget.get("canonical_or_diagnostic")),
        "latest_consecutive_proxy_or_diagnostic_tasks_used": int(
            latest_budget.get("consecutive_proxy_or_diagnostic_tasks_used") or 0
        ),
        "latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": int(
            latest_budget.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
        ),
        "mission_state_path": str(mission_state_path) if mission_state_path.exists() else "",
        "mission_state_status": _normalize(mission_state.get("status")),
        "mission_state_current_iteration": int(mission_state.get("current_iteration") or 0)
        if isinstance(mission_state, dict)
        else 0,
        "mission_state_last_decision_id": _normalize(mission_state.get("last_decision_id")),
        "checkpoint_count": len(checkpoint_files),
        "latest_checkpoint_path": str(latest_checkpoint_path) if latest_checkpoint_path else "",
        "latest_checkpoint_id": _normalize(latest_checkpoint.get("checkpoint_id")),
        "latest_checkpoint_parent_decision_id": _normalize(latest_checkpoint.get("parent_decision_id")),
        "latest_gate_status": _normalize(latest_gate.get("status")),
        "latest_gate_reason_codes": _normalize_string_list(latest_gate.get("reason_codes")),
        "latest_gate_evidence_gap": latest_gate.get("evidence_gap") if isinstance(latest_gate, dict) else {},
        "latest_collect_judge_contract_present": bool(latest_gate.get("collect_judge_contract_present")),
        "latest_discussion_evidence_pack_present": bool(latest_gate.get("discussion_evidence_pack_present")),
        "latest_formulation_faithfulness_present": bool(latest_gate.get("formulation_faithfulness_present")),
        "latest_side_effects": latest_gate.get("side_effects") if isinstance(latest_gate, dict) else {},
        "latest_judge_status": _normalize(latest_judge.get("status")),
        "latest_continuation_status": _normalize(latest_decision.get("status")),
        "latest_next_action": _normalize(latest_decision.get("next_action")),
        "latest_claim_limits": _normalize_string_list(latest_decision.get("claim_limits")),
        "latest_budget_update": latest_budget,
        "latest_budget_audit_update": latest_budget_audit,
        "latest_budget_update_uses_fallback": latest_budget_uses_fallback,
        "latest_information_gain": latest_information_gain,
        "latest_information_gain_total_score": int(latest_information_gain.get("total_score") or 0)
        if isinstance(latest_information_gain, dict)
        else 0,
        "latest_information_gain_routing": _normalize(latest_information_gain.get("routing")),
        "latest_factual_memory_path": str(actionable_iteration / "factual_memory_append.jsonl")
        if actionable_iteration is not None and (actionable_iteration / "factual_memory_append.jsonl").exists()
        else "",
        "latest_kb_update_proposal_dir": str(actionable_iteration / "kb_update_proposal")
        if actionable_iteration is not None and (actionable_iteration / "kb_update_proposal").exists()
        else "",
        "latest_kb_update_proposal_id": _normalize(latest_kb_update_pointer.get("proposal_id")),
        "latest_kb_update_status": _normalize(latest_kb_update_pointer.get("status")),
        "latest_kb_update_global_proposal_dir": _normalize(latest_kb_update_pointer.get("proposal_dir")),
        "latest_followup_proposal_status": _normalize(followup_state.get("status")),
        "latest_followup_can_write_proposal": bool(followup_state.get("can_write_proposal")),
        "latest_followup_candidate_count": int(followup_state.get("candidate_count") or 0),
        "latest_followup_reasons": _normalize_string_list(followup_state.get("reasons")),
        "latest_remote_transport": latest_transport_snapshot,
        "latest_remote_transport_status": _normalize(latest_transport_snapshot.get("status")),
        "latest_remote_transport_reason_code": _normalize(latest_transport_snapshot.get("reason_code")),
        "latest_remote_transport_automation_retry_safe": bool(latest_transport_snapshot.get("automation_retry_safe")),
    }


def read_local_mission_remote_history(
    run_root: Path | str,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    root_dir = Path(run_root)
    summary = summarize_local_mission_remote_rehearsal(root_dir)
    if not root_dir.exists():
        return {
            "present": False,
            "run_root": str(root_dir),
            "checkpoint_count": 0,
            "budget_ledger_entries": 0,
            "history": [],
        }

    iterations = sorted(
        [
            child
            for child in root_dir.iterdir()
            if child.is_dir() and child.name.startswith("iteration_")
        ],
        key=lambda item: item.name,
    )
    if limit is not None and limit > 0:
        iterations = iterations[-limit:]

    history: list[dict[str, Any]] = []
    for iteration_dir in iterations:
        gate_payload: dict[str, Any] = {}
        judge_payload: dict[str, Any] = {}
        decision_payload: dict[str, Any] = {}
        budget_payload: dict[str, Any] = {}
        information_gain_payload: dict[str, Any] = {}
        kb_update_pointer: dict[str, Any] = {}
        for target_name, filename in (
            ("gate", "mission_gate_report.yaml"),
            ("judge", "judge_report.yaml"),
            ("decision", "continuation_decision.yaml"),
            ("budget", "budget_update.yaml"),
            ("information_gain", "information_gain.yaml"),
            ("kb_update", "kb_update_proposal/proposal_pointer.yaml"),
        ):
            path = iteration_dir / filename
            if not path.exists():
                continue
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(payload, dict):
                continue
            if target_name == "gate":
                gate_payload = payload
            elif target_name == "judge":
                judge_payload = payload
            elif target_name == "decision":
                decision_payload = payload
            elif target_name == "information_gain":
                information_gain_payload = payload
            elif target_name == "kb_update":
                kb_update_pointer = payload
            else:
                budget_payload = payload

        normalized_budget_payload = _normalized_budget_row(budget_payload)
        history.append(
            {
                "iteration_id": iteration_dir.name,
                "iteration_dir": str(iteration_dir),
                "gate_status": _normalize(gate_payload.get("status")),
                "gate_reason_codes": _normalize_string_list(gate_payload.get("reason_codes")),
                "gate_evidence_gap": gate_payload.get("evidence_gap") if isinstance(gate_payload, dict) else {},
                "collect_judge_contract_present": bool(gate_payload.get("collect_judge_contract_present")),
                "discussion_evidence_pack_present": bool(gate_payload.get("discussion_evidence_pack_present")),
                "formulation_faithfulness_present": bool(gate_payload.get("formulation_faithfulness_present")),
                "side_effects": gate_payload.get("side_effects") if isinstance(gate_payload, dict) else {},
                "judge_status": _normalize(judge_payload.get("status")),
                "continuation_status": _normalize(decision_payload.get("status")),
                "next_action": _normalize(decision_payload.get("next_action")),
                "claim_limits": _normalize_string_list(decision_payload.get("claim_limits")),
                "canonical_or_diagnostic": _normalize(normalized_budget_payload.get("canonical_or_diagnostic")),
                "execution_scope": _normalize(normalized_budget_payload.get("execution_scope")),
                "collect_judge_completed": bool(normalized_budget_payload.get("collect_judge_completed")),
                "consecutive_proxy_or_diagnostic_tasks_used": int(
                    normalized_budget_payload.get("consecutive_proxy_or_diagnostic_tasks_used") or 0
                ),
                "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": int(
                    normalized_budget_payload.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
                ),
                "information_gain_total_score": int(information_gain_payload.get("total_score") or 0)
                if isinstance(information_gain_payload, dict)
                else 0,
                "information_gain_routing": _normalize(information_gain_payload.get("routing")),
                "information_gain": information_gain_payload if isinstance(information_gain_payload, dict) else {},
                "factual_memory_path": str(iteration_dir / "factual_memory_append.jsonl")
                if (iteration_dir / "factual_memory_append.jsonl").exists()
                else "",
                "kb_update_proposal_dir": str(iteration_dir / "kb_update_proposal")
                if (iteration_dir / "kb_update_proposal").exists()
                else "",
                "kb_update_status": _normalize(kb_update_pointer.get("status")),
                "kb_update_global_proposal_dir": _normalize(kb_update_pointer.get("proposal_dir")),
                "budget_update": normalized_budget_payload,
            }
        )

    return {
        **summary,
        "history": history,
    }


def build_local_mission_remote_followup_candidates(
    run_root: Path | str,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    root_dir = Path(run_root)
    summary = summarize_local_mission_remote_rehearsal(root_dir)
    result: dict[str, Any] = {
        "status": "blocked",
        "present": bool(summary.get("present")),
        "run_root": str(root_dir),
        "actor": _normalize(actor) or "system",
        "can_write_proposal": False,
        "proposal_run_id": "",
        "candidate_count": 0,
        "reasons": [],
        "candidates": [],
        "latest_iteration_id": _normalize(summary.get("latest_iteration_id")),
        "latest_actionable_iteration_id": _normalize(summary.get("latest_actionable_iteration_id")),
        "latest_consecutive_proxy_or_diagnostic_tasks_used": int(
            summary.get("latest_consecutive_proxy_or_diagnostic_tasks_used") or 0
        ),
        "latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": int(
            summary.get("latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
        ),
        "latest_pending_remote_iteration_id": _normalize(summary.get("latest_pending_remote_iteration_id")),
    }
    if not root_dir.exists() or not summary.get("present"):
        result["reasons"] = ["mission_remote_rehearsal_missing"]
        return result

    latest_iteration_id = _normalize(summary.get("latest_iteration_id"))
    if not latest_iteration_id:
        result["reasons"] = ["latest_iteration_missing"]
        return result

    if bool(summary.get("latest_remote_collect_judge_pending")):
        result["reasons"] = ["prior_remote_collect_judge_incomplete"]
        result["latest_iteration_id"] = latest_iteration_id
        result["latest_pending_remote_iteration_id"] = _normalize(
            summary.get("latest_pending_remote_iteration_id")
        )
        return result

    actionable_iteration_id = _normalize(summary.get("latest_actionable_iteration_id"))
    if not actionable_iteration_id:
        result["reasons"] = ["latest_iteration_artifacts_invalid"]
        result["latest_iteration_id"] = latest_iteration_id
        return result

    iteration_dir = root_dir / actionable_iteration_id
    try:
        decision_payload = _read_yaml_dict(iteration_dir / "continuation_decision.yaml")
        gate_payload = _read_yaml_dict(iteration_dir / "mission_gate_report.yaml")
        judge_payload = _read_yaml_dict(iteration_dir / "judge_report.yaml")
    except (OSError, ValueError, yaml.YAMLError):
        result["reasons"] = ["latest_iteration_artifacts_invalid"]
        return result

    mission_id = _normalize(decision_payload.get("mission_id")) or _normalize(summary.get("mission_state_last_decision_id")).split(":")[0]
    task_id = _normalize(decision_payload.get("task_id")) or "unknown_task"
    latest_lane = _normalize(summary.get("latest_canonical_or_diagnostic")).lower()
    latest_consecutive_proxy_tasks = int(summary.get("latest_consecutive_proxy_or_diagnostic_tasks_used") or 0)
    latest_local_rehearsal_proxy_tasks = int(
        summary.get("latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
    )
    proxy_chain_limit = int(gate_payload.get("proxy_or_diagnostic_budget_limit") or 0)
    followup_state = _assess_followup_candidate_state(
        mission_id=mission_id,
        task_id=task_id,
        latest_iteration_id=actionable_iteration_id,
        gate_payload=gate_payload,
        decision_payload=decision_payload,
        latest_lane=latest_lane,
        latest_consecutive_proxy_tasks=latest_consecutive_proxy_tasks,
        latest_local_rehearsal_proxy_tasks=latest_local_rehearsal_proxy_tasks,
        proxy_chain_limit=proxy_chain_limit,
    )
    result.update(followup_state)
    if not followup_state["can_write_proposal"]:
        return result

    next_action = followup_state["next_action"]
    candidate_kind = followup_state["candidate_kind"]
    claim_limits = _normalize_string_list(decision_payload.get("claim_limits"))
    claim_limits.extend(
        [
            "mission_remote_local_rehearsal_only",
            "declared_side_effects_only",
        ]
    )
    candidate = validate_proposal_candidate(
        {
            "schema_version": 1,
            "candidate_id": f"{task_id}__{actionable_iteration_id}__{candidate_kind}",
            "source_council_id": f"mission_remote::{mission_id}::{actionable_iteration_id}",
            "source_task_id": task_id,
            "kind": candidate_kind,
            "line": "mission_remote",
            "workplace_profile": _normalize((gate_payload.get("workspace_profile") or {}).get("profile")),
            "reason": (
                "Mission-remote rehearsal produced bounded artifact-complete evidence; stage one bounded follow-up."
                if candidate_kind == "bounded_probe"
                else (
                    "Mission-remote rehearsal found recoverable artifact gaps; stage one bounded metric/artifact completion follow-up."
                    if candidate_kind == "metric_completion"
                    else "Mission-remote rehearsal found missing sidecar mechanism evidence; stage a bounded decision synthesis pivot."
                )
            ),
            "risk_flags": _normalize_string_list(gate_payload.get("reason_codes"))
            + _normalize_string_list(decision_payload.get("reason_codes"))
            + _normalize_string_list(judge_payload.get("reasons")),
            "claim_limitations": list(dict.fromkeys(claim_limits)),
            "continuation_lane": "mission_remote",
            "mission_id": mission_id,
            "parent_decision_id": _normalize(summary.get("mission_state_last_decision_id"))
            or _normalize(summary.get("latest_checkpoint_parent_decision_id")),
            "mission_gate_pass": True,
            "next_step_readiness": {
                "can_stage": True,
                "bounded_validation": _compact_bounded_validation_readiness(judge_payload),
            },
        }
    )
    proposal_run_id = f"{mission_id}__mission_remote__{actionable_iteration_id}"
    result.update(
        {
            "status": "ready",
            "can_write_proposal": True,
            "mission_id": mission_id,
            "task_id": task_id,
            "latest_iteration_id": latest_iteration_id,
            "latest_actionable_iteration_id": actionable_iteration_id,
            "next_action": next_action,
            "proposal_run_id": proposal_run_id,
            "candidate_count": 1,
            "latest_canonical_or_diagnostic": latest_lane,
            "latest_consecutive_proxy_or_diagnostic_tasks_used": latest_consecutive_proxy_tasks,
            "proxy_or_diagnostic_budget_limit": proxy_chain_limit,
            "reasons": [],
            "candidates": [candidate],
        }
    )
    return result


def write_local_mission_remote_followup_candidates(
    run_root: Path | str,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    built = build_local_mission_remote_followup_candidates(run_root, actor=actor)
    if not built.get("can_write_proposal"):
        return {
            **built,
            "written": False,
            "created": False,
            "replayed": False,
        }

    written = write_proposal_candidates_dry_run(
        {
            "schema_version": 1,
            "proposal_run_id": built["proposal_run_id"],
            "actor": built["actor"],
            "source_council_id": (
                f"mission_remote::{built['mission_id']}::{built['latest_actionable_iteration_id']}"
            ),
            "source_task_id": built["task_id"],
            "line": "mission_remote",
            "workplace_profile": built["candidates"][0].get("workplace_profile"),
            "candidates": built["candidates"],
        },
        proposal_run_id=built["proposal_run_id"],
    )
    return {
        **built,
        "status": "written",
        "written": bool(written.get("created") or written.get("replayed")),
        "created": bool(written.get("created")),
        "replayed": bool(written.get("replayed")),
        "proposal_write": written,
        "candidates_path": written.get("candidates_path", ""),
        "summary_path": written.get("summary_path", ""),
    }


__all__ = [
    "MissionRemoteContinuationDecision",
    "MissionRemoteGateReport",
    "assess_local_mission_remote_preflight",
    "assess_mission_remote_launch_packet",
    "assess_mission_remote_gate",
    "build_local_mission_remote_followup_candidates",
    "execute_mission_remote_launch_once",
    "finalize_mission_remote_collect_judge",
    "read_local_mission_remote_history",
    "run_local_mission_remote_rehearsal",
    "sync_finalize_mission_remote_iteration",
    "summarize_local_mission_remote_rehearsal",
    "write_mission_remote_launch_packet",
    "write_local_mission_remote_followup_candidates",
    "write_local_mission_remote_preflight",
    "write_local_mission_remote_rehearsal",
]

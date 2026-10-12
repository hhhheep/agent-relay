from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .paths import continuation_launch_preflight_dir, continuation_manual_launch_records_dir
from .launch_intent import LAUNCH_INTENT_SCHEMA_ID, LAUNCH_INTENT_SCHEMA_VERSION as LAUNCH_INTENT_SCHEMA_VERSION
from .remote_boundary_contract import (
    REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS,
    assess_disabled_side_effects,
    collect_required_text_fields,
)
from solution_agent.orchestrator.paths import state_dir


MANUAL_LAUNCH_SCHEMA_VERSION = 1
MANUAL_LAUNCH_STAGE = "manual_bounded_remote_launch_preflight"
MANUAL_LAUNCH_READINESS_REPORT_STAGE = "manual_bounded_remote_launch_readiness_report"
MANUAL_LAUNCH_READINESS_REPORT_NAME = "manual_launch_readiness_report.json"

DISABLED_SIDE_EFFECTS: Dict[str, bool] = dict(REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS)

REMOTE_TRANSPORT_LOG_KINDS = {"ssh", "scp_to", "scp_from", "ssh_stream"}

DESTRUCTIVE_COMMAND_PATTERNS = {
    "rm ",
    "rm -",
    "remove-item",
    "del ",
    "rmdir",
    "rd ",
    "git reset --hard",
    "git clean",
    "mkfs",
    "format ",
    "truncate ",
    "shred ",
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    text = _normalize(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _remote_call_log_path() -> Path:
    return state_dir() / "remote_call_log.jsonl"


def _recent_remote_transport_calls(
    *,
    host: str,
    window_seconds: int,
    now: datetime | None = None,
    log_path: Path | None = None,
) -> List[Dict[str, Any]]:
    normalized_host = _normalize(host)
    if not normalized_host:
        return []
    path = log_path or _remote_call_log_path()
    if not path.exists():
        return []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = max(0, int(window_seconds))
    recent: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if _normalize(item.get("host")) != normalized_host:
            continue
        if _normalize(item.get("kind")) not in REMOTE_TRANSPORT_LOG_KINDS:
            continue
        ts = _parse_timestamp(item.get("ts"))
        if ts is None:
            continue
        age = (current - ts).total_seconds()
        if 0 <= age <= window:
            recent.append(item)
    return recent


def _remote_workplace_paths_compatible(packet: dict) -> bool:
    root = _normalize(packet.get("remote_root")).rstrip("/")
    workdir = _normalize(packet.get("remote_workdir")).rstrip("/")
    if not root or not workdir:
        return False
    return (
        workdir == root
        or workdir.startswith(root + "/")
        or root.startswith(workdir + "/")
    )


def _destructive_command_reasons(packet: dict) -> List[str]:
    argv = packet.get("argv")
    argv_text = " ".join(_normalize(part) for part in argv) if isinstance(argv, list) else ""
    display = _normalize(packet.get("display_command"))
    haystack = f"{argv_text}\n{display}".lower()
    matched = sorted(pattern for pattern in DESTRUCTIVE_COMMAND_PATTERNS if pattern in haystack)
    return [f"destructive_command_pattern:{pattern}" for pattern in matched]


def _load_launch_intent(launch_intent: dict | Path | str) -> Dict[str, Any]:
    if isinstance(launch_intent, (Path, str)):
        payload = json.loads(Path(launch_intent).read_text(encoding="utf-8"))
    elif isinstance(launch_intent, dict):
        payload = dict(launch_intent)
    else:
        raise TypeError("launch_intent must be a dict or path")
    if not isinstance(payload, dict):
        raise ValueError("launch intent payload must be a JSON object")
    return payload


def _is_schema_supported(launch_intent: Dict[str, Any]) -> bool:
    return _normalize(launch_intent.get("execution_mode")) == LAUNCH_INTENT_SCHEMA_ID


def _is_schema_version_valid(launch_intent: Dict[str, Any]) -> bool:
    try:
        schema_version = int(launch_intent.get("schema_version", 0))
    except (TypeError, ValueError):
        return False
    return schema_version == LAUNCH_INTENT_SCHEMA_VERSION


def _load_json_object(payload_or_path: dict | Path | str, *, label: str) -> Dict[str, Any]:
    if isinstance(payload_or_path, (Path, str)):
        payload = json.loads(Path(payload_or_path).read_text(encoding="utf-8"))
    elif isinstance(payload_or_path, dict):
        payload = dict(payload_or_path)
    else:
        raise TypeError(f"{label} must be a dict or path")
    if not isinstance(payload, dict):
        raise ValueError(f"{label} payload must be a JSON object")
    return payload


def _intent_source_path(launch_intent: dict | Path | str) -> str:
    if isinstance(launch_intent, (Path, str)):
        return str(Path(launch_intent))
    return _normalize(launch_intent.get("launch_intent_path") if isinstance(launch_intent, dict) else "")


def _manual_launch_id(launch_intent_id: str, launch_intent_hash: str) -> str:
    safe_id = _normalize(launch_intent_id).replace("..", "_").replace("/", "_").replace("\\", "_")
    if not safe_id:
        safe_id = "launch_intent"
    return f"ml_{safe_id}_{launch_intent_hash[:12]}"


def _manual_launch_idempotency_key(
    *,
    launch_intent_id: str,
    launch_intent_hash: str,
    actor: str,
    operator_confirmed: bool,
) -> str:
    payload = {
        "launch_intent_id": launch_intent_id,
        "launch_intent_hash": launch_intent_hash,
        "actor": actor,
        "operator_confirmed": bool(operator_confirmed),
        "stage": MANUAL_LAUNCH_STAGE,
    }
    return f"ml_{_sha256_hex(payload)[:20]}"


def _manual_launch_readiness_report_payload_id(
    *,
    manual_launch_id: str,
    command_packet_id: str,
    actor: str,
) -> str:
    payload = {
        "manual_launch_id": _normalize(manual_launch_id),
        "command_packet_id": _normalize(command_packet_id),
        "actor": _normalize(actor),
    }
    return f"mlrpt_{_sha256_hex(payload)[:20]}"


def assess_manual_launch_preflight(
    launch_intent: dict | Path | str,
    *,
    actor: str,
    operator_confirmed: bool = False,
) -> Dict[str, Any]:
    """Assess whether a launch intent is ready for an explicit human-run SSH step.

    This is still local-only. It deliberately does not perform SSH, subprocess,
    scheduler dispatch, remote writes, registry mutation, or remote execution.
    """

    normalized_actor = _normalize(actor)
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []

    if not normalized_actor:
        reasons.append("actor_missing")
        checks.append({"id": "actor_present", "status": "failed"})
    else:
        checks.append({"id": "actor_present", "status": "passed", "actor": normalized_actor})

    try:
        intent = _load_launch_intent(launch_intent)
        checks.append({"id": "launch_intent_load", "status": "passed"})
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        checks.append({"id": "launch_intent_load", "status": "failed", "reason": str(exc)})
        return {
            "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
            "stage": MANUAL_LAUNCH_STAGE,
            "manual_launch_id": "",
            "idempotency_key": "",
            "launch_intent_id": "",
            "launch_intent_hash": "",
            "launch_intent_path": _intent_source_path(launch_intent),
            "actor": normalized_actor,
            "status": "blocked",
            "ready_for_manual_remote_launch": False,
            "launch_enabled": False,
            "reasons": ["launch_intent_load_failed"],
            "checks": checks,
            "side_effects": dict(DISABLED_SIDE_EFFECTS),
            "next_required_approval": "fix_launch_intent_payload",
        }

    launch_intent_hash = _sha256_hex(intent)
    if not _is_schema_version_valid(intent):
        reasons.append("launch_intent_schema_version_invalid")
    if not _is_schema_supported(intent):
        reasons.append("launch_intent_schema_id_mismatch")
    checks.append(
        {
            "id": "launch_intent_schema",
            "status": "passed" if not reasons else "failed",
            "schema_id": _normalize(intent.get("execution_mode")),
            "schema_version": _normalize(intent.get("schema_version")),
        },
    )
    launch_intent_id = _normalize(intent.get("launch_intent_id"))
    manual_launch_id = _manual_launch_id(launch_intent_id, launch_intent_hash)
    idempotency_key = _manual_launch_idempotency_key(
        launch_intent_id=launch_intent_id,
        launch_intent_hash=launch_intent_hash,
        actor=normalized_actor,
        operator_confirmed=operator_confirmed,
    )

    if not launch_intent_id:
        reasons.append("launch_intent_id_missing")
    if _normalize(intent.get("status")) != "ready_for_launch_preflight":
        reasons.append("launch_intent_not_ready_for_preflight")
    if intent.get("launch_enabled") is not False:
        reasons.append("launch_intent_launch_enabled_must_be_false")
    if _normalize(intent.get("worker")) != "materialized_execution":
        reasons.append("non_materialized_execution_worker")

    side_effect_reasons, side_effect_check = assess_disabled_side_effects(
        intent,
        reason_code="launch_intent_side_effects_enabled",
        expected=DISABLED_SIDE_EFFECTS,
        check_id="launch_intent_side_effects_disabled",
    )
    reasons.extend(side_effect_reasons)
    checks.append(side_effect_check)

    required_fields, missing_fields = collect_required_text_fields(
        intent,
        (
            "task_id",
            "source_work_order_id",
            "dispatch_request_id",
            "workplace_profile",
            "host",
            "remote_root",
            "remote_workdir",
        ),
    )
    if missing_fields:
        reasons.append("required_launch_field_missing")
    checks.append(
        {
            "id": "required_launch_fields",
            "status": "passed" if not missing_fields else "failed",
            "missing": missing_fields,
        },
    )

    if not operator_confirmed:
        reasons.append("explicit_manual_launch_confirmation_required")
    checks.append(
        {
            "id": "explicit_manual_launch_confirmation",
            "status": "passed" if operator_confirmed else "failed",
            "operator_confirmed": bool(operator_confirmed),
        },
    )

    status = "ready_for_operator_remote_launch" if not reasons else "blocked"
    return {
        "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
        "stage": MANUAL_LAUNCH_STAGE,
        "manual_launch_id": manual_launch_id,
        "idempotency_key": idempotency_key,
        "launch_intent_id": launch_intent_id,
        "launch_intent_hash": launch_intent_hash,
        "launch_intent_path": _intent_source_path(launch_intent),
        "actor": normalized_actor,
        "status": status,
        "ready_for_manual_remote_launch": status == "ready_for_operator_remote_launch",
        "launch_enabled": False,
        "task_id": required_fields["task_id"],
        "source_work_order_id": required_fields["source_work_order_id"],
        "dispatch_request_id": required_fields["dispatch_request_id"],
        "workplace_profile": required_fields["workplace_profile"],
        "host": required_fields["host"],
        "remote_root": required_fields["remote_root"],
        "remote_workdir": required_fields["remote_workdir"],
        "worker": _normalize(intent.get("worker")),
        "resource_hint": _normalize(intent.get("resource_hint")),
        "resource_lane": _normalize(intent.get("resource_lane")),
        "reasons": sorted(set(reasons)),
        "checks": checks,
        "side_effects": dict(DISABLED_SIDE_EFFECTS),
        "next_required_approval": "operator_runs_explicit_ssh_command_outside_p3f" if status == "ready_for_operator_remote_launch" else "resolve_manual_launch_preflight_blockers",
    }


def write_manual_launch_preflight(
    launch_intent: dict | Path | str,
    *,
    actor: str,
    operator_confirmed: bool = False,
) -> Dict[str, Any]:
    report = assess_manual_launch_preflight(
        launch_intent,
        actor=actor,
        operator_confirmed=operator_confirmed,
    )
    manual_launch_id = _normalize(report.get("manual_launch_id"))
    if not manual_launch_id:
        return {
            **report,
            "created": False,
            "replayed": False,
            "preflight_path": "",
            "record_path": "",
        }

    preflight_dir = continuation_launch_preflight_dir()
    records_dir = continuation_manual_launch_records_dir()
    preflight_path = preflight_dir / f"{manual_launch_id}.json"
    record_path = records_dir / f"{manual_launch_id}.json"
    payload = {
        **report,
        "preflight_path": str(preflight_path),
        "record_path": str(record_path),
    }
    canonical = _canonical_json(payload) + "\n"

    preflight_dir.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=True)

    created = False
    replayed = False
    for path in (preflight_path, record_path):
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {
                    **payload,
                    "status": "blocked",
                    "reasons": ["manual_launch_record_corrupt"],
                    "created": False,
                    "replayed": False,
                }
            if _canonical_json(existing) != _canonical_json(payload):
                if _normalize(existing.get("idempotency_key")) == _normalize(payload.get("idempotency_key")):
                    return {
                        **payload,
                        "status": "blocked",
                        "reasons": ["idempotency_collision"],
                        "created": False,
                        "replayed": False,
                    }
                return {
                    **payload,
                    "status": "blocked",
                    "reasons": ["manual_launch_id_collision"],
                    "created": False,
                    "replayed": False,
                }
            replayed = True
            continue
        path.write_text(canonical, encoding="utf-8")
        created = True

    return {
        **payload,
        "created": created,
        "replayed": replayed and not created,
    }


def build_manual_launch_command_packet(
    manual_launch_preflight: dict | Path | str,
    *,
    detach: bool = True,
    execution_phase: str = "execute_run",
) -> Dict[str, Any]:
    """Build a printable local CLI command packet for an explicitly approved launch.

    The packet is a review artifact only. It returns argv/display command text for
    a future operator-approved `run-remote` invocation and does not execute it.
    """

    try:
        preflight = _load_json_object(manual_launch_preflight, label="manual launch preflight")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
            "stage": "manual_bounded_remote_launch_command_packet",
            "status": "blocked",
            "reasons": ["manual_launch_preflight_load_failed"],
            "error": str(exc),
            "argv": [],
            "display_command": "",
            "side_effects": dict(DISABLED_SIDE_EFFECTS),
        }

    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []
    if _normalize(preflight.get("status")) != "ready_for_operator_remote_launch":
        reasons.append("manual_launch_preflight_not_ready")
    if preflight.get("ready_for_manual_remote_launch") is not True:
        reasons.append("manual_launch_ready_flag_false")
    if preflight.get("launch_enabled") is not False:
        reasons.append("preflight_launch_enabled_must_be_false")
    side_effect_reasons, side_effect_check = assess_disabled_side_effects(
        preflight,
        reason_code="manual_launch_preflight_side_effects_enabled",
        expected=DISABLED_SIDE_EFFECTS,
        check_id="manual_launch_preflight_side_effects_disabled",
    )
    reasons.extend(side_effect_reasons)
    checks.append(side_effect_check)

    required = {
        "manual_launch_id": _normalize(preflight.get("manual_launch_id")),
        "task_id": _normalize(preflight.get("task_id")),
        "host": _normalize(preflight.get("host")),
        "remote_root": _normalize(preflight.get("remote_root")),
        "remote_workdir": _normalize(preflight.get("remote_workdir")),
        "worker": _normalize(preflight.get("worker")),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        reasons.append("required_command_field_missing")
    checks.append({"id": "required_command_fields", "status": "passed" if not missing else "failed", "missing": missing})

    phase = _normalize(execution_phase) or "execute_run"
    if phase not in {"full", "prepare_code", "execute_run"}:
        reasons.append("unsupported_execution_phase")

    argv = [
        "python",
        "-m",
        "solution_agent.cli",
        "run-remote",
        required["task_id"],
        "--host",
        required["host"],
        "--remote-root",
        required["remote_root"],
        "--remote-workdir",
        required["remote_workdir"],
        "--worker",
        required["worker"],
        "--phase",
        phase,
    ]
    if detach:
        argv.append("--detach")

    command_packet_id = ""
    idempotency_key = ""
    if required["manual_launch_id"]:
        command_packet_id = f"mlcmd_{required['manual_launch_id']}_{_sha256_hex({'argv': argv, 'preflight': preflight})[:12]}"
        idempotency_key = f"mlcmd_{_sha256_hex({'manual_launch_id': required['manual_launch_id'], 'argv': argv})[:20]}"

    status = "ready_for_operator_execution" if not reasons else "blocked"
    return {
        "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
        "stage": "manual_bounded_remote_launch_command_packet",
        "command_packet_id": command_packet_id,
        "idempotency_key": idempotency_key,
        "manual_launch_id": required["manual_launch_id"],
        "manual_launch_preflight_hash": _sha256_hex(preflight),
        "status": status,
        "reasons": sorted(set(reasons)),
        "ready_for_operator_execution": status == "ready_for_operator_execution",
        "task_id": required["task_id"],
        "host": required["host"],
        "remote_root": required["remote_root"],
        "remote_workdir": required["remote_workdir"],
        "worker": required["worker"],
        "argv": argv if status == "ready_for_operator_execution" else [],
        "display_command": subprocess.list2cmdline(argv) if status == "ready_for_operator_execution" else "",
        "intended_command_side_effects_if_operator_runs_it": {
            "ssh": True,
            "remote_write": True,
            "remote_execution": True,
            "subprocess": True,
            "scheduler_dispatch": False,
            "registry_mutation": False,
        },
        "side_effects": dict(DISABLED_SIDE_EFFECTS),
        "checks": checks,
        "next_required_approval": "explicit_user_approval_to_execute_command_packet" if status == "ready_for_operator_execution" else "resolve_command_packet_blockers",
    }


def write_manual_launch_command_packet(
    manual_launch_preflight: dict | Path | str,
    *,
    detach: bool = True,
    execution_phase: str = "execute_run",
) -> Dict[str, Any]:
    packet = build_manual_launch_command_packet(
        manual_launch_preflight,
        detach=detach,
        execution_phase=execution_phase,
    )
    packet_id = _normalize(packet.get("command_packet_id"))
    if not packet_id:
        return {
            **packet,
            "created": False,
            "replayed": False,
            "packet_path": "",
            "packet_markdown_path": "",
        }

    records_dir = continuation_manual_launch_records_dir()
    packet_path = records_dir / f"{packet_id}.json"
    packet_markdown_path = records_dir / f"{packet_id}.md"
    payload = {
        **packet,
        "packet_path": str(packet_path),
        "packet_markdown_path": str(packet_markdown_path),
    }
    markdown = "\n".join(
        [
            "# Manual Launch Command Packet",
            "",
            f"- status: {payload['status']}",
            f"- manual_launch_id: {payload['manual_launch_id']}",
            f"- ready_for_operator_execution: {str(payload['ready_for_operator_execution']).lower()}",
            f"- next_required_approval: {payload['next_required_approval']}",
            "",
            "```powershell",
            payload["display_command"],
            "```",
            "",
            "This packet does not execute SSH or remote work. It is a review artifact for a later explicit operator action.",
            "",
        ],
    )
    canonical = _canonical_json(payload) + "\n"
    records_dir.mkdir(parents=True, exist_ok=True)

    created = False
    replayed = False
    for path, expected_text in ((packet_path, canonical), (packet_markdown_path, markdown)):
        if path.exists():
            existing_text = path.read_text(encoding="utf-8")
            if existing_text != expected_text:
                return {
                    **payload,
                    "status": "blocked",
                    "reasons": ["manual_launch_command_packet_collision"],
                    "created": False,
                    "replayed": False,
                }
            replayed = True
            continue
        path.write_text(expected_text, encoding="utf-8")
        created = True

    return {
        **payload,
        "created": created,
        "replayed": replayed and not created,
    }


def assess_live_launch_executor_guard(
    command_packet: dict | Path | str,
    *,
    actor: str,
    execute_requested: bool = False,
    approval_phrase: str = "",
    mission_bounded: bool = False,
    min_interval_seconds: int = 300,
    rate_limit_window_seconds: int = 600,
    max_transport_calls_per_window: int = 2,
    remote_call_log_path: Path | None = None,
) -> Dict[str, Any]:
    """Final local guard before any live remote launch executor is allowed.

    This guard never executes the packet. It reports whether the packet is ready
    for dry-run review, or whether a requested live execution has reached the
    explicit approval/sandbox boundary.
    """

    normalized_actor = _normalize(actor)
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []
    try:
        packet = _load_json_object(command_packet, label="manual launch command packet")
        checks.append({"id": "command_packet_load", "status": "passed"})
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
            "stage": "manual_bounded_remote_launch_live_executor_guard",
            "status": "blocked",
            "reasons": ["command_packet_load_failed"],
            "error": str(exc),
            "actor": normalized_actor,
            "execute_requested": bool(execute_requested),
            "would_execute": False,
            "executed": False,
            "side_effects": dict(DISABLED_SIDE_EFFECTS),
            "checks": checks,
        }

    if not normalized_actor:
        reasons.append("actor_missing")
    checks.append({"id": "actor_present", "status": "passed" if normalized_actor else "failed", "actor": normalized_actor})

    if _normalize(packet.get("status")) != "ready_for_operator_execution":
        reasons.append("command_packet_not_ready")
    if packet.get("ready_for_operator_execution") is not True:
        reasons.append("command_packet_ready_flag_false")
    if not isinstance(packet.get("argv"), list) or not packet.get("argv"):
        reasons.append("command_packet_argv_missing")
    if not _normalize(packet.get("display_command")):
        reasons.append("command_packet_display_command_missing")
    side_effect_reasons, side_effect_check = assess_disabled_side_effects(
        packet,
        reason_code="command_packet_side_effects_enabled",
        expected=DISABLED_SIDE_EFFECTS,
        check_id="command_packet_side_effects_disabled",
    )
    reasons.extend(side_effect_reasons)
    checks.append(side_effect_check)

    packet_intended = packet.get("intended_command_side_effects_if_operator_runs_it")
    if not isinstance(packet_intended, dict) or packet_intended.get("ssh") is not True or packet_intended.get("remote_execution") is not True:
        reasons.append("intended_remote_side_effects_not_declared")

    packet_host = _normalize(packet.get("host"))
    recent_calls = _recent_remote_transport_calls(
        host=packet_host,
        window_seconds=rate_limit_window_seconds,
        log_path=remote_call_log_path,
    )
    recent_count = len(recent_calls)
    ssh_rate_limit = {
        "host": packet_host,
        "window_seconds": int(rate_limit_window_seconds),
        "max_transport_calls_per_window": int(max_transport_calls_per_window),
        "recent_transport_call_count": recent_count,
        "min_interval_seconds": int(min_interval_seconds),
        "log_path": str(remote_call_log_path or _remote_call_log_path()),
        "status": "passed",
    }
    if recent_count >= int(max_transport_calls_per_window):
        ssh_rate_limit["status"] = "blocked"
        if execute_requested:
            reasons.append("ssh_rate_limit_active")
    checks.append({"id": "ssh_rate_limit", **ssh_rate_limit})

    workplace_ok = _remote_workplace_paths_compatible(packet)
    checks.append({
        "id": "mission_workplace_scope",
        "status": "passed" if workplace_ok else "failed",
        "remote_root": _normalize(packet.get("remote_root")),
        "remote_workdir": _normalize(packet.get("remote_workdir")),
    })
    destructive_reasons = _destructive_command_reasons(packet)
    checks.append({
        "id": "destructive_command_guard",
        "status": "passed" if not destructive_reasons else "failed",
        "matched": destructive_reasons,
    })
    if mission_bounded:
        if not workplace_ok:
            reasons.append("remote_workplace_paths_incompatible")
        reasons.extend(destructive_reasons)

    if execute_requested:
        if approval_phrase != "RUN_REMOTE_MANUAL_LAUNCH":
            reasons.append("explicit_live_execution_phrase_required")
        if not reasons:
            status = "ready_for_mission_bounded_execution" if mission_bounded else "requires_sandbox_escalation"
        else:
            status = "blocked"
    else:
        status = "dry_run_ready" if not reasons else "blocked"

    guard_id = ""
    idempotency_key = ""
    packet_id = _normalize(packet.get("command_packet_id"))
    if packet_id:
        guard_payload = {
            "command_packet_id": packet_id,
            "packet_hash": _sha256_hex(packet),
            "actor": normalized_actor,
            "execute_requested": bool(execute_requested),
            "approval_phrase_present": bool(approval_phrase),
            "mission_bounded": bool(mission_bounded),
        }
        guard_id = f"mlguard_{packet_id}_{_sha256_hex(guard_payload)[:12]}"
        idempotency_key = f"mlguard_{_sha256_hex(guard_payload)[:20]}"

    return {
        "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
        "stage": "manual_bounded_remote_launch_live_executor_guard",
        "live_guard_id": guard_id,
        "idempotency_key": idempotency_key,
        "command_packet_id": packet_id,
        "command_packet_hash": _sha256_hex(packet),
        "actor": normalized_actor,
        "status": status,
        "reasons": sorted(set(reasons)),
        "execute_requested": bool(execute_requested),
        "mission_bounded": bool(mission_bounded),
        "agent_policy_allows_remote_execution": status == "ready_for_mission_bounded_execution",
        "would_execute": status in {"requires_sandbox_escalation", "ready_for_mission_bounded_execution"},
        "executed": False,
        "argv": packet.get("argv", []) if isinstance(packet.get("argv"), list) else [],
        "display_command": _normalize(packet.get("display_command")),
        "ssh_rate_limit": ssh_rate_limit,
        "side_effects": dict(DISABLED_SIDE_EFFECTS),
        "intended_command_side_effects_if_approved": packet_intended if isinstance(packet_intended, dict) else {},
        "checks": checks,
        "next_required_approval": "execute_command_packet_via_host_shell_with_sandbox_approval_if_needed" if status == "ready_for_mission_bounded_execution" else (
            "sandbox_escalation_and_user_approval" if status == "requires_sandbox_escalation" else (
            "rerun_with_execute_and_approval_phrase_to_request_live_boundary" if status == "dry_run_ready" else "resolve_live_guard_blockers"
            )
        ),
    }


def write_live_launch_executor_guard(
    command_packet: dict | Path | str,
    *,
    actor: str,
    execute_requested: bool = False,
    approval_phrase: str = "",
    mission_bounded: bool = False,
    min_interval_seconds: int = 300,
    rate_limit_window_seconds: int = 600,
    max_transport_calls_per_window: int = 2,
) -> Dict[str, Any]:
    guard = assess_live_launch_executor_guard(
        command_packet,
        actor=actor,
        execute_requested=execute_requested,
        approval_phrase=approval_phrase,
        mission_bounded=mission_bounded,
        min_interval_seconds=min_interval_seconds,
        rate_limit_window_seconds=rate_limit_window_seconds,
        max_transport_calls_per_window=max_transport_calls_per_window,
    )
    guard_id = _normalize(guard.get("live_guard_id"))
    if not guard_id:
        return {**guard, "created": False, "replayed": False, "guard_path": ""}

    records_dir = continuation_manual_launch_records_dir()
    guard_path = records_dir / f"{guard_id}.json"
    payload = {**guard, "guard_path": str(guard_path)}
    canonical = _canonical_json(payload) + "\n"
    records_dir.mkdir(parents=True, exist_ok=True)

    if guard_path.exists():
        try:
            existing = guard_path.read_text(encoding="utf-8")
        except OSError:
            return {**payload, "status": "blocked", "reasons": ["live_guard_unreadable"], "created": False, "replayed": False}
        if existing == canonical:
            return {**payload, "created": False, "replayed": True}
        return {**payload, "status": "blocked", "reasons": ["live_guard_collision"], "created": False, "replayed": False}

    guard_path.write_text(canonical, encoding="utf-8")
    return {**payload, "created": True, "replayed": False}


def assess_manual_launch_readiness_report(
    launch_intent: dict | Path | str,
    *,
    actor: str,
    operator_confirmed: bool = False,
) -> Dict[str, Any]:
    """Assess whether a local launch intent is ready to request a human-approved remote launch."""

    preflight = assess_manual_launch_preflight(
        launch_intent,
        actor=actor,
        operator_confirmed=operator_confirmed,
    )
    packet = build_manual_launch_command_packet(
        preflight,
        detach=True,
        execution_phase="execute_run",
    )
    guard = assess_live_launch_executor_guard(
        packet,
        actor=actor,
        execute_requested=False,
    )

    preflight_reasons = [item for item in preflight.get("reasons", []) if _normalize(item)]
    packet_reasons = [item for item in packet.get("reasons", []) if _normalize(item)]
    guard_reasons = [item for item in guard.get("reasons", []) if _normalize(item)]
    all_reasons = sorted(set(preflight_reasons + packet_reasons + guard_reasons))
    blocker_count = len(all_reasons)

    readiness_ready = (
        preflight.get("status") == "ready_for_operator_remote_launch"
        and packet.get("status") == "ready_for_operator_execution"
        and guard.get("status") == "dry_run_ready"
        and not all_reasons
    )
    ready_for_human_approved_remote_launch = bool(readiness_ready)

    launch_intent_id = _normalize(preflight.get("launch_intent_id"))
    preflight_path = _normalize(preflight.get("preflight_path"))
    packet_path = _normalize(packet.get("packet_path"))
    command_packet_id = _normalize(packet.get("command_packet_id"))
    manual_launch_id = _normalize(preflight.get("manual_launch_id"))
    report_id = _manual_launch_readiness_report_payload_id(
        manual_launch_id=manual_launch_id,
        command_packet_id=command_packet_id,
        actor=actor,
    )

    if not preflight.get("ready_for_manual_remote_launch"):
        next_required_approval = preflight.get("next_required_approval") or "resolve_manual_launch_preflight_blockers"
    elif not packet.get("ready_for_operator_execution"):
        next_required_approval = packet.get("next_required_approval") or "resolve_command_packet_blockers"
    else:
        next_required_approval = guard.get("next_required_approval") or "operator_runs_live_launch_guard_with_approval_phrase"

    preflight_status = _normalize(preflight.get("status"))
    packet_status = _normalize(packet.get("status"))
    guard_status = _normalize(guard.get("status"))
    status = "ready_for_human_approved_remote_launch" if readiness_ready else "blocked"
    remote_boundary_reached = guard_status == "requires_sandbox_escalation"
    return {
        "schema_version": MANUAL_LAUNCH_SCHEMA_VERSION,
        "artifact_type": "manual_launch_readiness_report",
        "stage": MANUAL_LAUNCH_READINESS_REPORT_STAGE,
        "readiness_report_id": report_id,
        "manual_launch_id": manual_launch_id,
        "command_packet_id": command_packet_id,
        "launch_intent_id": launch_intent_id,
        "launch_intent_path": _intent_source_path(launch_intent),
        "actor": _normalize(actor),
        "status": status,
        "ready_for_human_approved_remote_launch": ready_for_human_approved_remote_launch,
        "manual_launch_ready_for_remote_launch": bool(preflight.get("ready_for_manual_remote_launch")),
        "preflight_status": preflight_status,
        "command_packet_status": packet_status,
        "live_guard_status": guard_status,
        "live_guard_would_execute": bool(guard.get("would_execute")),
        "remote_boundary": "reached" if remote_boundary_reached else "not_reached",
        "remote_boundary_reached": remote_boundary_reached,
        "preflight_path": preflight_path,
        "packet_path": packet_path,
        "preflight_ready": bool(preflight.get("ready_for_manual_remote_launch")),
        "command_packet_ready": bool(packet.get("ready_for_operator_execution")),
        "reasons": all_reasons,
        "blocker_count": blocker_count,
        "next_required_approval": next_required_approval,
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
        "preflight": preflight,
        "command_packet": packet,
        "live_guard": guard,
        "checks": [
            {"id": "manual_launch_preflight", "status": preflight.get("status"), "manual_launch_id": manual_launch_id},
            {"id": "manual_launch_command_packet", "status": packet_status, "command_packet_id": command_packet_id},
            {"id": "manual_launch_live_guard_dry_run", "status": guard_status, "execute_requested": False},
        ],
    }


def write_manual_launch_readiness_report(
    launch_intent: dict | Path | str,
    *,
    actor: str,
    operator_confirmed: bool = False,
) -> Dict[str, Any]:
    report = assess_manual_launch_readiness_report(
        launch_intent,
        actor=actor,
        operator_confirmed=operator_confirmed,
    )
    report_id = _normalize(report.get("readiness_report_id"))
    if not report_id:
        return {
            **report,
            "status": "blocked",
            "reasons": ["manual_launch_readiness_report_id_missing"],
            "created": False,
            "replayed": False,
            "report_path": "",
        }

    records_dir = continuation_manual_launch_records_dir()
    report_path = records_dir / f"{report_id}_{MANUAL_LAUNCH_READINESS_REPORT_NAME}"
    payload = {**report, "report_path": str(report_path)}
    canonical = _canonical_json(payload) + "\n"

    records_dir.mkdir(parents=True, exist_ok=True)
    if report_path.exists():
        try:
            existing = report_path.read_text(encoding="utf-8")
        except OSError:
            return {
                **payload,
                "status": "blocked",
                "reasons": ["manual_launch_readiness_report_unreadable"],
                "created": False,
                "replayed": False,
            }
        if existing == canonical:
            return {**payload, "created": False, "replayed": True}
        return {
            **payload,
            "status": "blocked",
            "reasons": ["manual_launch_readiness_report_collision"],
            "created": False,
            "replayed": False,
        }

    report_path.write_text(canonical, encoding="utf-8")
    return {**payload, "created": True, "replayed": False}


__all__ = [
    "assess_manual_launch_preflight",
    "build_manual_launch_command_packet",
    "assess_live_launch_executor_guard",
    "write_live_launch_executor_guard",
    "write_manual_launch_command_packet",
    "write_manual_launch_preflight",
    "assess_manual_launch_readiness_report",
    "write_manual_launch_readiness_report",
]

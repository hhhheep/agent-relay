from __future__ import annotations

import json
import hashlib
import getpass
import hmac
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from ..contracts import CONTRACT_SCHEMA_VERSION, REMOTE_RUNTIME_VERSION, V1_BASELINE_VERSION
from ..failure_taxonomy import failure_contract, recommended_action_for_failure
from ..harness.context_budget import conservative_token_estimate
from ..harness.model_dispatch_guard import (
    ModelDispatchDenied,
    admit_model_dispatch,
    complete_model_dispatch,
    model_request_fingerprint,
)
from ..harness.telemetry_contracts import PhaseBudget, UsageEvent
from ..harness.usage_ledger import UsageLedger


def _fs_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        return "\\\\?\\" + resolved
    return resolved


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_fs_path(src), _fs_path(dst))


def _copy_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(_fs_path(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_fs_path(src), _fs_path(dst))


def _write_tar_gz(archive_path: Path, files: Dict[str, Path]) -> Path:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        for arcname, path in files.items():
            archive.add(path, arcname=arcname)
    return archive_path


def _remote_runtime_files() -> Dict[str, Path]:
    repo = root()
    files: Dict[str, Path] = {}
    for rel in (
        "scripts/probes/run_probe.py",
        "scripts/probes/run_eval_probe.py",
        "scripts/eval/run_recipe_ablation.py",
        "scripts/reports/run_contract_artifact_writer.py",
        "scripts/reports/run_b14_workplace_sanity.py",
        "scripts/reports/run_b14_large_image_coordinate_protocol_audit.py",
        "scripts/reports/run_b14_large_image_bbox_sidecar_smoke.py",
        "scripts/reports/run_b14_large_image_feasibility_decision_synthesis.py",
        "scripts/reports/run_feature_leakage_inventory.py",
        "scripts/reports/run_feature_leakage_followup_packet.py",
        "scripts/reports/run_feature_leakage_maskout_probe.py",
        "scripts/reports/run_feature_leakage_synthesis.py",
        "scripts/reports/run_a_residual_metrics_synthesis.py",
        "scripts/loupe/run_metadata_audit.py",
        "scripts/loupe/run_manifest_metadata_audit.py",
        "scripts/loupe/run_metadata_only_classifier.py",
        "scripts/loupe/run_matched_group_split.py",
        "scripts/loupe/run_format_normalized_eval.py",
        "scripts/loupe/run_seg_overfit.py",
        "scripts/loupe/run_protocol_report_writer.py",
    ):
        path = repo / rel
        if path.exists():
            files[f"runtime/{rel}"] = path
    return files


def _remote_runtime_signature(files: Dict[str, Path]) -> str:
    digest = hashlib.sha1()
    digest.update(REMOTE_RUNTIME_VERSION.encode("utf-8"))
    digest.update(b"\0")
    for arcname, path in sorted(files.items()):
        digest.update(arcname.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


from .adapters import (
    git_diff,
    git_diff_stat,
    open_ssh_stream,
    run_codex,
    run_remote_materializer,
    run_ssh,
    scp_from_remote,
    scp_to_remote,
    submit_to_pueue,
    transport_command,
    write_materializer_worker_out,
    write_status_json,
)
from .memory import ensure_local_memory_files, remote_context_paths, update_after_collect
from .paths import (
    codex_worker_profile_path,
    dispatch_dir,
    ensure_orchestrator_dirs,
    registry_path,
    remote_execution_profiles_path,
    result_dir,
    result_latest_dir,
    result_run_dir,
    root,
    state_dir,
    task_dir,
    task_file,
    workflow_profile_path,
    worker_out_dir,
    worktree_dir,
)
from .prompt import build_worker_prompt
from .registry import ensure_registry, load_registry, mark_from_result, mutate_registry, registry_journal_path, update_task_record, upsert_task
from .reporting import write_index, write_latest_summary, write_task_report
from .schema import TaskBook, now_iso, worker_result_from_dict
from .supervisor import DEFAULT_OLLAMA_MODEL, SupervisionDecision, fallback_decision, supervise_with_llm
from .task_loader import ensure_task_front_matter, load_task, render_new_task
from ..utils.subprocess_utils import decode_output, hidden_subprocess_kwargs, run_subprocess
from ..utils.process_utils import process_is_alive as _pid_alive


def _timestamp_id() -> str:
    return now_iso().replace("-", "").replace(":", "").replace("+", "_").replace("T", "_")[:15]


def _iso_age_seconds(value: object) -> Optional[int]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
        return max(0, int((now - stamp).total_seconds()))
    except Exception:
        return None


def _next_attempt(task_id: str) -> int:
    record = load_registry().get("tasks", {}).get(task_id, {})
    return len(record.get("runs", []) or []) + 1


def _make_run_id(task_id: str, attempt: int) -> str:
    return f"{task_id}__{_timestamp_id()}__a{attempt:02d}"


def _append_run_record(task_id: str, run_record: Dict[str, object]) -> Dict[str, object]:
    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        runs = list(record.get("runs", []) or [])
        runs.append(run_record)
        record["runs"] = runs
        record["latest_run_id"] = run_record.get("run_id")
        return record

    return mutate_registry(_mutate)


def _upsert_run_record(task_id: str, run_record: Dict[str, object]) -> Dict[str, object]:
    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        runs = list(record.get("runs", []) or [])
        run_id = run_record.get("run_id")
        replaced = False
        for index, existing in enumerate(runs):
            if isinstance(existing, dict) and existing.get("run_id") == run_id:
                merged = dict(existing)
                merged.update(run_record)
                runs[index] = merged
                replaced = True
                break
        if not replaced:
            runs.append(run_record)
        record["runs"] = runs
        record["latest_run_id"] = run_id
        return record

    return mutate_registry(_mutate)


def _update_run_record(task_id: str, run_id: str, **updates: object) -> Dict[str, object]:
    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        runs = list(record.get("runs", []) or [])
        for index, existing in enumerate(runs):
            if isinstance(existing, dict) and existing.get("run_id") == run_id:
                merged = dict(existing)
                merged.update(updates)
                runs[index] = merged
                break
        record["runs"] = runs
        return record

    return mutate_registry(_mutate)


def _move_task_folder(task_id: str, destination_status: str) -> Optional[Path]:
    current_status, current_task = find_task(task_id)
    destination = task_dir(destination_status, task_id)
    if current_status == destination_status:
        return current_task.parent
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    current_task.parent.rename(destination)
    return destination


def _current_remote_task_dir(task_id: str, task_remote_root: str) -> str:
    record = load_registry().get("tasks", {}).get(task_id, {})
    remote = record.get("remote") or {}
    if isinstance(remote, dict) and remote.get("remote_task_dir"):
        return str(remote["remote_task_dir"])
    latest_run_id = record.get("latest_run_id")
    name = str(latest_run_id or task_id)
    return task_remote_root.rstrip("/\\") + f"/{name}"


def _scheduler_lock_file() -> Path:
    return state_dir() / "scheduler.lock"


def _remote_watch_lock_file() -> Path:
    return state_dir() / "remote_watch.lock"


def maintenance_mode_path() -> Path:
    return state_dir() / "maintenance_mode.json"


def default_maintenance_mode() -> Dict[str, object]:
    return {
        "enabled": False,
        "reason": "",
        "allow_dashboard_read": True,
        "allow_action_requests": True,
        "allow_scheduler_launch": True,
        "created_at": None,
    }


def load_maintenance_mode() -> Dict[str, object]:
    path = maintenance_mode_path()
    payload = default_maintenance_mode()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            payload.update(
                {
                    "enabled": True,
                    "reason": f"maintenance_mode unreadable: {exc}",
                    "allow_dashboard_read": True,
                    "allow_action_requests": False,
                    "allow_scheduler_launch": False,
                    "read_error": str(exc),
                }
            )
            return payload
        if isinstance(data, dict):
            payload.update(data)
    payload["enabled"] = bool(payload.get("enabled"))
    if payload["enabled"]:
        payload["allow_dashboard_read"] = bool(payload.get("allow_dashboard_read", True))
        payload["allow_action_requests"] = bool(payload.get("allow_action_requests", False))
        payload["allow_scheduler_launch"] = bool(payload.get("allow_scheduler_launch", False))
    else:
        payload["allow_dashboard_read"] = bool(payload.get("allow_dashboard_read", True))
        payload["allow_action_requests"] = bool(payload.get("allow_action_requests", True))
        payload["allow_scheduler_launch"] = bool(payload.get("allow_scheduler_launch", True))
    payload["path"] = str(path)
    return payload


def set_maintenance_mode(
    enabled: bool,
    *,
    reason: str = "",
    allow_dashboard_read: bool = True,
    allow_action_requests: bool = False,
    allow_scheduler_launch: bool = False,
) -> Dict[str, object]:
    path = maintenance_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = now_iso()
    payload = {
        "enabled": bool(enabled),
        "reason": reason,
        "allow_dashboard_read": bool(allow_dashboard_read),
        "allow_action_requests": bool(allow_action_requests) if enabled else True,
        "allow_scheduler_launch": bool(allow_scheduler_launch) if enabled else True,
        "created_at": now,
        "updated_at": now,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload["path"] = str(path)
    return payload


def clear_maintenance_mode() -> Dict[str, object]:
    return set_maintenance_mode(
        False,
        reason="maintenance cleared",
        allow_dashboard_read=True,
        allow_action_requests=True,
        allow_scheduler_launch=True,
    )


def drain_mode_path() -> Path:
    return state_dir() / "drain_mode.json"


def load_drain_mode() -> Dict[str, object]:
    path = drain_mode_path()
    payload: Dict[str, object] = {
        "enabled": False,
        "reason": "",
        "allow_collect": True,
        "allow_reconcile": True,
        "allow_reports": True,
        "allow_launch": True,
        "path": str(path),
    }
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            payload.update({"enabled": True, "allow_launch": False, "read_error": str(exc), "reason": f"drain_mode unreadable: {exc}"})
            return payload
        if isinstance(data, dict):
            payload.update(data)
    payload["enabled"] = bool(payload.get("enabled"))
    payload["allow_launch"] = bool(payload.get("allow_launch", not payload["enabled"]))
    payload["path"] = str(path)
    return payload


def set_drain_mode(enabled: bool, *, reason: str = "") -> Dict[str, object]:
    path = drain_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = now_iso()
    payload = {
        "enabled": bool(enabled),
        "reason": reason,
        "allow_collect": True,
        "allow_reconcile": True,
        "allow_reports": True,
        "allow_launch": not bool(enabled),
        "created_at": now,
        "updated_at": now,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload["path"] = str(path)
    return payload


def _remote_watch_state_file() -> Path:
    return state_dir() / "remote_watch_state.json"


def _safe_profile_part(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("_") or "default"


def remote_profile_id(host: str, remote_workdir: str, ssh_port: str = "22", uid: object = "") -> str:
    parts = [
        _safe_profile_part(host),
        f"p{_safe_profile_part(ssh_port)}",
        _safe_profile_part(remote_workdir),
    ]
    if uid not in ("", None):
        parts.append(f"uid{_safe_profile_part(uid)}")
    return "__".join(parts)


def load_remote_execution_profiles() -> Dict[str, object]:
    path = remote_execution_profiles_path()
    if not path.exists():
        return {"schema_version": 1, "profiles": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return data if isinstance(data, dict) else {"schema_version": 1, "profiles": {}}


def _remote_execution_profile_for(host: str, remote_workdir: str) -> Dict[str, object]:
    data = load_remote_execution_profiles()
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    candidates: List[Dict[str, object]] = []
    for key, value in profiles.items():
        if not isinstance(value, dict):
            continue
        profile = dict(value)
        profile.setdefault("profile", key)
        if str(key) == host or str(profile.get("host") or "") == host:
            candidates.append(profile)
    for profile in candidates:
        configured_workdir = str(profile.get("remote_workdir") or "").rstrip("/")
        if configured_workdir and configured_workdir == str(remote_workdir).rstrip("/"):
            return profile
    return candidates[0] if candidates else {}


def _resolve_remote_execution_env(task: TaskBook, host: str, remote_workdir: str) -> Dict[str, object]:
    profile = _remote_execution_profile_for(host, remote_workdir)
    conda = profile.get("conda") if isinstance(profile.get("conda"), dict) else {}
    isolation = profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {}
    raw = task.raw_front_matter
    conda_env = raw.get("remote_conda_env") or conda.get("env") or profile.get("conda_env")
    conda_prefix = raw.get("remote_conda_prefix") or conda.get("prefix") or profile.get("conda_prefix")
    conda_executable = (
        raw.get("remote_conda_executable")
        or conda.get("executable")
        or profile.get("conda_executable")
    )
    python_executable = raw.get("remote_python") or profile.get("python") or "python3"
    if conda_prefix:
        exe = str(conda_executable or "conda")
        command_prefix = [exe, "run", "-p", str(conda_prefix), str(profile.get("conda_python") or "python")]
        mode = "conda"
    elif conda_env:
        exe = str(conda_executable or "conda")
        command_prefix = [exe, "run", "-n", str(conda_env), str(profile.get("conda_python") or "python")]
        mode = "conda"
    else:
        command_prefix = [str(python_executable)]
        mode = "python"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "mode": mode,
        "profile": profile.get("profile"),
        "host": host,
        "remote_workdir": remote_workdir,
        "conda_env": str(conda_env) if conda_env else None,
        "conda_prefix": str(conda_prefix) if conda_prefix else None,
        "conda_executable": str(conda_executable) if conda_executable else None,
        "python": str(python_executable),
        "command_prefix": command_prefix,
        "isolation": {
            "per_run_directory": bool(isolation.get("per_run_directory", True)),
            "per_run_tmp": bool(isolation.get("per_run_tmp", True)),
            "per_run_cache": bool(isolation.get("per_run_cache", True)),
            "forbid_runtime_install": bool(isolation.get("forbid_runtime_install", True)),
            "output_policy": str(isolation.get("output_policy") or "run_scoped"),
        },
    }


def _shell_join(args: List[object]) -> str:
    return " ".join(shlex.quote(str(item)) for item in args)


def _parse_ssh_g_output(output: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip() or " " not in line:
            continue
        key, value = line.split(None, 1)
        if key in {"hostname", "user", "port", "identityfile"}:
            values[key] = value.strip()
    return values


def _remote_transport_report(
    *,
    host: str,
    ssh_info: Dict[str, object],
    remote_result: Dict[str, object] | None = None,
) -> Dict[str, object]:
    """Classify local SSH transport failures separately from remote readiness.

    Codex automations may run in a restricted local context that cannot read the
    user's SSH key/config or open the requested transport. In that case repeated
    remote retries do not add evidence about the remote workplace; the correct
    next step is a manual/elevated launch packet from the normal user session.
    """
    stderr = ""
    returncode: object = None
    if remote_result:
        stderr = str(remote_result.get("stderr") or "")
        returncode = remote_result.get("returncode")
    ssh_stderr = str(ssh_info.get("stderr") or "")
    combined_error = "\n".join(item for item in (ssh_stderr, stderr) if item).strip()
    normalized_error = combined_error.lower()
    ssh_hostname = str(ssh_info.get("hostname") or "").strip()
    ssh_user = str(ssh_info.get("user") or "").strip()
    ssh_port = str(ssh_info.get("port") or "").strip()

    if returncode in {0, "0"}:
        status = "ok"
        reason_code = "transport_ok"
        recovery_action = "continue_remote_collect_judge_flow"
        automation_retry_safe = True
    elif "identity file" in normalized_error and "not accessible" in normalized_error:
        status = "local_transport_auth_unavailable"
        reason_code = "ssh_identity_file_unreadable_from_current_context"
        recovery_action = "run the generated launch packet from the normal user session or an approved elevated Codex shell"
        automation_retry_safe = False
    elif "connect to host" in normalized_error and "permission denied" in normalized_error:
        status = "local_transport_permission_denied"
        reason_code = "ssh_connect_permission_denied_from_current_context"
        recovery_action = "run the generated launch packet from a context with network/SSH permission; do not spend another automation retry"
        automation_retry_safe = False
    elif "permission denied" in normalized_error:
        status = "local_transport_auth_unavailable"
        reason_code = "ssh_permission_denied_auth_or_key_unavailable"
        recovery_action = "verify the SSH profile/key in the normal user session or use the generated manual launch packet"
        automation_retry_safe = False
    elif (
        ssh_hostname.lower() == host.lower()
        and (not ssh_port or ssh_port == "22")
        and ssh_user.lower() in {"codexsandboxoffline", "sandbox", ""}
    ):
        status = "local_transport_profile_unresolved"
        reason_code = "ssh_alias_not_resolved_in_current_context"
        recovery_action = "provide SOLUTION_AGENT_SSH_CONFIG or run from the normal user session that can read ~/.ssh/config"
        automation_retry_safe = False
    elif returncode is None:
        status = "not_checked"
        reason_code = "transport_not_checked"
        recovery_action = "run remote-preflight before remote launch"
        automation_retry_safe = False
    else:
        status = "remote_or_transport_unreachable"
        reason_code = "ssh_unreachable_unclassified"
        recovery_action = "inspect stderr once; avoid repeated retries without a changed transport condition"
        automation_retry_safe = False

    return {
        "status": status,
        "reason_code": reason_code,
        "host": host,
        "ssh_hostname": ssh_hostname,
        "ssh_user": ssh_user,
        "ssh_port": ssh_port,
        "returncode": returncode,
        "automation_retry_safe": automation_retry_safe,
        "recovery_action": recovery_action,
        "error_excerpt": combined_error[:500],
    }


def _last_remote_preflight_path() -> Path:
    return state_dir() / "last_remote_preflight.json"


def _workplace_state_dir(profile_id: str) -> Path:
    return state_dir() / "workplaces" / _safe_profile_part(profile_id)


def _local_init_dir() -> Path:
    return state_dir() / "local_init"


def _parse_key_value_stdout(output: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _render_workplace_init_report(payload: Dict[str, object]) -> str:
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    probe = payload.get("probe") if isinstance(payload.get("probe"), dict) else {}
    missing_required = payload.get("missing_required") if isinstance(payload.get("missing_required"), list) else []
    missing_recommended = payload.get("missing_recommended") if isinstance(payload.get("missing_recommended"), list) else []
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    install_guidance = payload.get("install_guidance") if isinstance(payload.get("install_guidance"), list) else []
    security_warnings = payload.get("security_warnings") if isinstance(payload.get("security_warnings"), list) else []
    readiness_checks = payload.get("readiness_checks") if isinstance(payload.get("readiness_checks"), list) else []
    gpus = preflight.get("gpus") if isinstance(preflight.get("gpus"), list) else []
    lines = [
        f"# Workplace Init Report - {payload.get('profile_id')}",
        "",
        "## Status",
        "",
        f"- ok: {payload.get('ok')}",
        f"- checked_at: {payload.get('checked_at')}",
        f"- host: {payload.get('host')}",
        f"- remote_workdir: {payload.get('remote_workdir')}",
        f"- remote_output_root: {payload.get('remote_root')}",
        "",
        "## Identity",
        "",
        f"- ssh_host: {(preflight.get('ssh') or {}).get('hostname') if isinstance(preflight.get('ssh'), dict) else ''}",
        f"- ssh_port: {(preflight.get('ssh') or {}).get('port') if isinstance(preflight.get('ssh'), dict) else ''}",
        f"- remote_hostname: {(preflight.get('remote') or {}).get('hostname') if isinstance(preflight.get('remote'), dict) else ''}",
        f"- remote_user: {(preflight.get('remote') or {}).get('user') if isinstance(preflight.get('remote'), dict) else ''}",
        f"- remote_uid: {(preflight.get('remote') or {}).get('uid') if isinstance(preflight.get('remote'), dict) else ''}",
        "",
        "## Required / Recommended Tools",
        "",
    ]
    if missing_required:
        lines.append("- missing_required: " + ", ".join(str(item) for item in missing_required))
    else:
        lines.append("- missing_required: none")
    if missing_recommended:
        lines.append("- missing_recommended: " + ", ".join(str(item) for item in missing_recommended))
    else:
        lines.append("- missing_recommended: none")
    lines.extend(
        [
            "",
            "## Tool Versions",
            "",
            f"- bash: {probe.get('bash_version') or probe.get('bash_path')}",
            f"- python3: {probe.get('python_version') or probe.get('python3_path')}",
            f"- git: {probe.get('git_version') or probe.get('git_path')}",
            f"- tmux: {probe.get('tmux_version') or probe.get('tmux_path')}",
            f"- codex: {probe.get('codex_version') or probe.get('codex_path')}",
            f"- conda: {probe.get('conda_version') or probe.get('conda_path')}",
            f"- configured_conda_env: {probe.get('configured_conda_env') or '-'}",
            f"- configured_conda_env_ready: {probe.get('configured_conda_env_ready') or '-'}",
            "",
            "## Project",
            "",
            f"- git_inside_worktree: {probe.get('git_inside_worktree')}",
            f"- git_branch: {probe.get('git_branch')}",
            f"- git_head: {probe.get('git_head')}",
            f"- git_dirty_count: {probe.get('git_dirty_count')}",
            f"- disk_workdir: {probe.get('disk_workdir')}",
            f"- disk_output_root: {probe.get('disk_root')}",
            "",
            "## GPU",
            "",
        ]
    )
    if gpus:
        lines.append("| nvidia-smi id | uuid | name |")
        lines.append("| --- | --- | --- |")
        for gpu in gpus:
            if isinstance(gpu, dict):
                lines.append(f"| {gpu.get('index')} | {gpu.get('uuid')} | {gpu.get('name')} |")
    else:
        lines.append("No GPU records found.")
    if readiness_checks:
        lines.extend(["", "## Readiness Checks", ""])
        for item in readiness_checks:
            if not isinstance(item, dict):
                continue
            status = item.get("status") or "unknown"
            code = item.get("code") or "check"
            detail = item.get("detail") or ""
            lines.append(f"- {status}: {code}" + (f" - {detail}" if detail else ""))
    lines.extend(["", "## Suggestions", ""])
    if suggestions:
        lines.extend(f"- {item}" for item in suggestions)
    else:
        lines.append("- No action needed.")
    if security_warnings:
        lines.extend(["", "## Security Warnings", ""])
        lines.extend(f"- {item}" for item in security_warnings)
    if install_guidance:
        lines.extend(["", "## Install / Setup Guidance", ""])
        for item in install_guidance:
            if isinstance(item, dict):
                lines.append(f"### {item.get('title') or item.get('code') or 'Step'}")
                detail = item.get("detail")
                if detail:
                    lines.append(str(detail))
                commands = item.get("commands") if isinstance(item.get("commands"), list) else []
                if commands:
                    lines.append("")
                    lines.append("```bash")
                    lines.extend(str(command) for command in commands)
                    lines.append("```")
                lines.append("")
            else:
                lines.append(f"- {item}")
    materializer_env = payload.get("remote_materializer_env") if isinstance(payload.get("remote_materializer_env"), dict) else {}
    if materializer_env:
        lines.extend(
            [
                "",
                "## Remote Materializer Env",
                "",
                f"- ready: {materializer_env.get('ready')}",
                f"- skipped: {materializer_env.get('skipped', False)}",
                f"- reason: {materializer_env.get('reason', '')}",
                f"- remote: {materializer_env.get('remote', '~/.remote_materializer/.env')}",
            ]
        )
    lines.extend(["", "## Raw Probe", "", "```json", json.dumps(probe, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def _local_remote_materializer_env_path() -> Path:
    return Path.home() / ".remote_materializer" / ".env"


REMOTE_AUTH_ENV_KEY = "SOLUTION_AGENT_REMOTE_AUTH_TOKEN"


def _remote_auth_id(host: str, remote_workdir: str) -> str:
    raw = f"{host}::{remote_workdir.rstrip('/')}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{_safe_profile_part(host)}__{digest}"


def _remote_auth_token_path(host: str, remote_workdir: str) -> Path:
    return state_dir() / "remote_auth" / f"{_remote_auth_id(host, remote_workdir)}.token"


def _get_or_create_remote_auth_token(host: str, remote_workdir: str) -> str:
    path = _remote_auth_token_path(host, remote_workdir)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def _remote_auth_signature(token: str, run_id: str) -> str:
    return hmac.new(token.encode("utf-8"), run_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _env_text_with_remote_auth(env_text: str, token: str) -> str:
    lines = []
    found = False
    for line in env_text.splitlines():
        if line.strip().startswith(f"{REMOTE_AUTH_ENV_KEY}="):
            lines.append(f"{REMOTE_AUTH_ENV_KEY}={token}")
            found = True
        else:
            lines.append(line)
    if not found:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{REMOTE_AUTH_ENV_KEY}={token}")
    return "\n".join(lines).rstrip() + "\n"


def _verify_remote_materializer_env_auth(host: str, remote_workdir: str, timeout: int = 15) -> Dict[str, object]:
    token = _get_or_create_remote_auth_token(host, remote_workdir)
    nonce = secrets.token_urlsafe(16)
    expected = _remote_auth_signature(token, nonce)
    command = (
        f"NONCE={shlex.quote(nonce)} EXPECTED_SIG={shlex.quote(expected)} python3 - <<'PY'\n"
        "import hashlib,hmac,os,pathlib,sys\n"
        "env=pathlib.Path(os.path.expanduser('~/.remote_materializer/.env'))\n"
        "secret=''\n"
        "try:\n"
        "    for line in env.read_text(encoding='utf-8').splitlines():\n"
        "        line=line.strip()\n"
        f"        if line.startswith('{REMOTE_AUTH_ENV_KEY}='):\n"
        "            secret=line.split('=',1)[1].strip().strip('\"\\'')\n"
        "            break\n"
        "except OSError:\n"
        "    sys.exit(2)\n"
        "if not secret:\n"
        "    sys.exit(3)\n"
        "actual=hmac.new(secret.encode(), os.environ.get('NONCE','').encode(), hashlib.sha256).hexdigest()\n"
        "sys.exit(0 if hmac.compare_digest(actual, os.environ.get('EXPECTED_SIG','')) else 4)\n"
        "PY"
    )
    result = run_ssh(host, command, timeout=timeout)
    code = int(result.get("returncode", 1) or 0)
    return {
        "ready": code == 0,
        "method": "hmac-sha256",
        "secret_remote_env": REMOTE_AUTH_ENV_KEY,
        "returncode": code,
        "status": "matched" if code == 0 else ("missing_env" if code == 2 else "missing_token" if code == 3 else "mismatch" if code == 4 else "check_failed"),
        "check": result,
    }


def _workplace_init_readiness_checks(payload: Dict[str, object]) -> List[Dict[str, object]]:
    probe = payload.get("probe") if isinstance(payload.get("probe"), dict) else {}
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    materializer_env = payload.get("remote_materializer_env") if isinstance(payload.get("remote_materializer_env"), dict) else {}
    bootstrap = payload.get("bootstrap") if isinstance(payload.get("bootstrap"), dict) else {}
    remote_sync = payload.get("remote_sync") if isinstance(payload.get("remote_sync"), dict) else {}
    checks: List[Dict[str, object]] = []

    def add(code: str, ok: bool, detail: str = "") -> None:
        checks.append({"code": code, "status": "ok" if ok else "fail", "detail": detail})

    add("remote_preflight", bool(preflight.get("ok")), "; ".join(str(item) for item in preflight.get("errors") or [])[:300])
    add("workplace_probe_ssh", str(probe.get("returncode")) == "0", str(probe.get("stderr") or "")[:300])
    for name, key in {
        "tool:bash": "bash_path",
        "tool:tar": "tar_path",
        "tool:gzip": "gzip_path",
        "tool:python3": "python3_path",
        "tool:git": "git_path",
        "tool:nvidia-smi": "nvidia_smi_path",
    }.items():
        add(name, bool(str(probe.get(key) or "").strip()), str(probe.get(key) or "missing"))
    configured_conda = str(probe.get("configured_conda_env") or "").strip()
    configured_conda_prefix = str(probe.get("configured_conda_prefix") or "").strip()
    configured_conda_target = configured_conda_prefix or configured_conda
    conda_ready = str(probe.get("configured_conda_env_ready") or "")
    add(
        "conda_env",
        not configured_conda_target or conda_ready.startswith("yes") or conda_ready == "not_configured",
        f"{configured_conda_target or 'not_configured'} {conda_ready}".strip(),
    )
    if materializer_env:
        auth = materializer_env.get("auth") if isinstance(materializer_env.get("auth"), dict) else {}
        add("remote_materializer_env", bool(materializer_env.get("ready")), str(materializer_env.get("reason") or ""))
        add("remote_materializer_auth", bool(auth.get("ready")), str(auth.get("status") or ""))
    if bootstrap and not bootstrap.get("skipped"):
        add("runtime_bootstrap", bool(bootstrap.get("ok", True)), str(bootstrap.get("reason") or ""))
    if remote_sync and not remote_sync.get("skipped"):
        bundle = remote_sync.get("bundle") if isinstance(remote_sync.get("bundle"), dict) else {}
        copy = bundle.get("copy") if isinstance(bundle.get("copy"), dict) else {}
        extract = bundle.get("extract") if isinstance(bundle.get("extract"), dict) else {}
        add(
            "workplace_profile_sync",
            int(copy.get("returncode", 0) or 0) == 0 and int(extract.get("returncode", 0) or 0) == 0,
            str((copy.get("stderr") or extract.get("stderr") or ""))[:300],
        )
    return checks


def _workplace_agent_context(payload: Dict[str, object]) -> Dict[str, object]:
    probe = payload.get("probe") if isinstance(payload.get("probe"), dict) else {}
    execution_profile = payload.get("execution_profile") if isinstance(payload.get("execution_profile"), dict) else {}
    materializer_env = payload.get("remote_materializer_env") if isinstance(payload.get("remote_materializer_env"), dict) else {}
    auth = materializer_env.get("auth") if isinstance(materializer_env.get("auth"), dict) else {}
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "kind": "solution_agent.workplace_agent_context",
        "profile_id": payload.get("profile_id"),
        "host": payload.get("host"),
        "remote_workdir": payload.get("remote_workdir"),
        "remote_root": payload.get("remote_root"),
        "ok": bool(payload.get("ok")),
        "missing_required": payload.get("missing_required") if isinstance(payload.get("missing_required"), list) else [],
        "missing_recommended": payload.get("missing_recommended") if isinstance(payload.get("missing_recommended"), list) else [],
        "configured_conda_env": probe.get("configured_conda_env") or None,
        "configured_conda_prefix": probe.get("configured_conda_prefix") or None,
        "configured_conda_env_ready": probe.get("configured_conda_env_ready") or None,
        "git": {
            "inside_worktree": probe.get("git_inside_worktree"),
            "branch": probe.get("git_branch"),
            "head": probe.get("git_head"),
            "dirty_count": probe.get("git_dirty_count"),
        },
        "project_assets": execution_profile.get("project_assets") if isinstance(execution_profile.get("project_assets"), dict) else {},
        "remote_materializer": {
            "env_ready": bool(materializer_env.get("ready")),
            "auth_method": auth.get("method"),
            "auth_status": auth.get("status"),
            "auth_ready": bool(auth.get("ready")),
        },
        "readiness_checks": payload.get("readiness_checks") if isinstance(payload.get("readiness_checks"), list) else [],
        "security_notes": [
            "Provider secrets are not included in this context.",
            "Use this as the small workplace summary for API/context-pack prompts instead of scanning state/workplaces or remote outputs.",
        ],
    }


def _render_workplace_agent_context(context: Dict[str, object]) -> str:
    lines = [
        f"# Workplace Agent Context - {context.get('profile_id')}",
        "",
        "This is a sanitized, token-bounded summary for solution-agent/API context packs.",
        "",
        f"- host: {context.get('host')}",
        f"- remote_workdir: {context.get('remote_workdir')}",
        f"- remote_root: {context.get('remote_root')}",
        f"- ok: {context.get('ok')}",
        f"- configured_conda_env: {context.get('configured_conda_env') or '-'}",
        f"- configured_conda_prefix: {context.get('configured_conda_prefix') or '-'}",
        f"- configured_conda_env_ready: {context.get('configured_conda_env_ready') or '-'}",
        "",
        "## Readiness Checks",
        "",
    ]
    for item in context.get("readiness_checks") or []:
        if isinstance(item, dict):
            lines.append(f"- {item.get('status')}: {item.get('code')} - {item.get('detail') or ''}")
    assets = context.get("project_assets") if isinstance(context.get("project_assets"), dict) else {}
    if assets:
        lines.extend(["", "## Project Assets", ""])
        for key in (
            "workspace_root",
            "clean_repo",
            "agent_repo",
            "pretrained_weights_root",
            "pe_backbone",
        ):
            if assets.get(key):
                lines.append(f"- {key}: {assets.get(key)}")
        for key in ("checkpoint_roots", "datasets", "configs", "known_checkpoints"):
            value = assets.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        output_policy = assets.get("output_policy") if isinstance(assets.get("output_policy"), dict) else {}
        if output_policy:
            lines.append(f"- output_policy: {output_policy}")
        notes = assets.get("notes") if isinstance(assets.get("notes"), list) else []
        for note in notes[:8]:
            lines.append(f"- note: {note}")
    lines.extend(["", "## Security Notes", ""])
    for note in context.get("security_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _sync_remote_materializer_env(host: str, remote_workdir: str, local_env_path: Optional[Path] = None) -> Dict[str, object]:
    """Provision the API env file used by the remote materializer without exposing secrets."""
    local_env = local_env_path or _local_remote_materializer_env_path()
    remote_env = "~/.remote_materializer/.env"
    token = _get_or_create_remote_auth_token(host, remote_workdir)
    if not local_env.exists():
        auth = _verify_remote_materializer_env_auth(host, remote_workdir)
        ready = bool(auth.get("ready"))
        return {
            "ready": ready,
            "skipped": True,
            "reason": "local env missing; remote env auth matched" if ready else f"local env missing; remote auth {auth.get('status')}",
            "remote": remote_env,
            "auth": auth,
        }

    mkdir_remote = run_ssh(host, 'mkdir -p "$HOME/.remote_materializer" && chmod 700 "$HOME/.remote_materializer"', timeout=15)
    if int(mkdir_remote.get("returncode", 1)) != 0:
        return {
            "ready": False,
            "remote": remote_env,
            "local_env": "present",
            "mkdir": mkdir_remote,
            "copy": {"skipped": True, "reason": "remote directory setup failed"},
            "install": {"skipped": True, "reason": "remote directory setup failed"},
        }

    staged_env = state_dir() / "remote_auth" / f"{_remote_auth_id(host, remote_workdir)}.remote_materializer.env"
    staged_env.parent.mkdir(parents=True, exist_ok=True)
    staged_env.write_text(_env_text_with_remote_auth(local_env.read_text(encoding="utf-8"), token), encoding="utf-8")
    try:
        os.chmod(staged_env, 0o600)
    except OSError:
        pass
    remote_tmp = ".remote_materializer/.env.tmp"
    copy_env = scp_to_remote(staged_env, host, remote_tmp)
    try:
        staged_env.unlink()
    except OSError:
        pass
    if int(copy_env.get("returncode", 1)) != 0:
        return {
            "ready": False,
            "remote": remote_env,
            "local_env": "present",
            "mkdir": mkdir_remote,
            "copy": copy_env,
            "install": {"skipped": True, "reason": "copy failed"},
        }

    install_env = run_ssh(
        host,
        'chmod 600 "$HOME/.remote_materializer/.env.tmp" '
        '&& mv "$HOME/.remote_materializer/.env.tmp" "$HOME/.remote_materializer/.env" '
        '&& chmod 600 "$HOME/.remote_materializer/.env" '
        '&& test -s "$HOME/.remote_materializer/.env"',
        timeout=15,
    )
    auth = _verify_remote_materializer_env_auth(host, remote_workdir) if int(install_env.get("returncode", 1)) == 0 else {"ready": False, "status": "install_failed"}
    return {
        "ready": int(install_env.get("returncode", 1)) == 0 and bool(auth.get("ready")),
        "remote": remote_env,
        "local_env": "present",
        "auth": auth,
        "mkdir": mkdir_remote,
        "copy": copy_env,
        "install": install_env,
    }


def _check_remote_materializer_env(host: str, remote_workdir: str, reason: str = "check only") -> Dict[str, object]:
    remote_env = "~/.remote_materializer/.env"
    auth = _verify_remote_materializer_env_auth(host, remote_workdir)
    ready = bool(auth.get("ready"))
    return {
        "ready": ready,
        "skipped": True,
        "reason": reason if ready else f"{reason}; remote auth {auth.get('status')}",
        "remote": remote_env,
        "auth": auth,
    }


def _run_local_version(command: str, args: Optional[List[str]] = None, timeout: int = 10) -> Dict[str, object]:
    path = shutil.which(command)
    if not path:
        return {"found": False, "path": "", "version": "", "returncode": None}
    cmd = [path, *(args or ["--version"])]
    try:
        completed = run_subprocess(cmd, timeout=timeout)
        version = (completed.stdout or completed.stderr or "").strip().splitlines()
        return {
            "found": True,
            "path": path,
            "version": version[0] if version else "",
            "returncode": completed.returncode,
        }
    except Exception as exc:
        return {"found": True, "path": path, "version": "", "returncode": None, "error": str(exc)}


def _python_import_status(module: str) -> Dict[str, object]:
    try:
        import importlib.util

        spec = importlib.util.find_spec(module)
        return {"available": spec is not None, "module": module}
    except Exception as exc:
        return {"available": False, "module": module, "error": str(exc)}


def _path_write_probe(path: Path, create: bool = True) -> Dict[str, object]:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        return {"path": str(path), "exists": False, "writable": False, "error": "path does not exist"}
    probe = path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"path": str(path), "exists": path.exists(), "writable": True}
    except Exception as exc:
        return {"path": str(path), "exists": path.exists(), "writable": False, "error": str(exc)}


def _safe_path_exists(path: Path) -> Dict[str, object]:
    try:
        return {"path": str(path), "exists": path.exists(), "error": ""}
    except Exception as exc:
        return {"path": str(path), "exists": False, "error": str(exc)}


def _render_local_init_report(payload: Dict[str, object]) -> str:
    required = payload.get("missing_required") if isinstance(payload.get("missing_required"), list) else []
    recommended = payload.get("missing_recommended") if isinstance(payload.get("missing_recommended"), list) else []
    tools = payload.get("tools") if isinstance(payload.get("tools"), dict) else {}
    python_modules = payload.get("python_modules") if isinstance(payload.get("python_modules"), dict) else {}
    paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    lines = [
        f"# Local Init Report - {payload.get('profile_id')}",
        "",
        "## Status",
        "",
        f"- ok: {payload.get('ok')}",
        f"- checked_at: {payload.get('checked_at')}",
        f"- root: {payload.get('root')}",
        f"- python: {payload.get('python_executable')}",
        f"- platform: {payload.get('platform')}",
        "",
        "## Missing",
        "",
        "- missing_required: " + (", ".join(str(item) for item in required) if required else "none"),
        "- missing_recommended: " + (", ".join(str(item) for item in recommended) if recommended else "none"),
        "",
        "## Tools",
        "",
        "| tool | found | version/path |",
        "| --- | --- | --- |",
    ]
    for name in sorted(tools):
        item = tools.get(name) if isinstance(tools.get(name), dict) else {}
        lines.append(f"| {name} | {item.get('found')} | {item.get('version') or item.get('path') or ''} |")
    lines.extend(["", "## Python Modules", "", "| module | available |", "| --- | --- |"])
    for name in sorted(python_modules):
        item = python_modules.get(name) if isinstance(python_modules.get(name), dict) else {}
        lines.append(f"| {name} | {item.get('available')} |")
    lines.extend(["", "## Paths", "", "| path | writable |", "| --- | --- |"])
    for name in sorted(paths):
        item = paths.get(name) if isinstance(paths.get(name), dict) else {}
        lines.append(f"| {name}: {item.get('path')} | {item.get('writable')} |")
    lines.extend(["", "## Suggestions", ""])
    if suggestions:
        lines.extend(f"- {item}" for item in suggestions)
    else:
        lines.append("- No action needed.")
    lines.extend(["", "## Raw", "", "```json", json.dumps(payload, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def local_environment_init(create_dirs: bool = True) -> Dict[str, object]:
    checked_at = now_iso()
    if create_dirs:
        ensure_orchestrator_dirs()
        ensure_registry()
        ensure_local_memory_files()
    user = getpass.getuser()
    node = platform.node() or "local"
    profile_id = "__".join([_safe_profile_part("local"), _safe_profile_part(node), _safe_profile_part(user), _safe_profile_part(str(root()))])
    required_tools = {
        "python": [sys.executable, "--version"],
        "git": ["git", "--version"],
        "ssh": ["ssh", "-V"],
        "scp": ["scp", "-V"],
    }
    optional_tools = {
        "codex": ["codex", "--version"],
        "ollama": ["ollama", "--version"],
        "pueue": ["pueue", "--version"],
        "rclone": ["rclone", "version"],
        "dvc": ["dvc", "--version"],
        "mlflow": ["mlflow", "--version"],
    }
    tools: Dict[str, Dict[str, object]] = {}
    for name, cmd in {**required_tools, **optional_tools}.items():
        if name == "python":
            completed = run_subprocess(cmd)
            tools[name] = {
                "found": True,
                "path": sys.executable,
                "version": (completed.stdout or completed.stderr or "").strip(),
                "returncode": completed.returncode,
            }
        else:
            tools[name] = _run_local_version(cmd[0], cmd[1:])
    required_modules = ["click", "yaml", "jinja2", "pydantic"]
    optional_modules = ["rich", "ollama", "instructor", "mlflow", "dvc"]
    python_modules = {name: _python_import_status(name) for name in [*required_modules, *optional_modules]}
    path_checks = {
        "root": root(),
        "tasks_inbox": root() / "tasks" / "inbox",
        "tasks_pending": root() / "tasks" / "pending",
        "results": root() / "results",
        "reports": root() / "reports",
        "state": state_dir(),
        "dispatch_sent": root() / "dispatch" / "sent",
    }
    paths = {name: _path_write_probe(path, create=create_dirs) for name, path in path_checks.items()}
    ssh_config = Path.home() / ".ssh" / "config"
    ssh_config_status = _safe_path_exists(ssh_config)
    known_hosts_status = _safe_path_exists(Path.home() / ".ssh" / "known_hosts")
    ssh_info = {
        "config_exists": ssh_config_status.get("exists"),
        "config_path": str(ssh_config),
        "config_error": ssh_config_status.get("error"),
        "known_hosts_exists": known_hosts_status.get("exists"),
        "known_hosts_error": known_hosts_status.get("error"),
    }
    missing_required: List[str] = []
    missing_recommended: List[str] = []
    for name in required_tools:
        if not tools.get(name, {}).get("found"):
            missing_required.append(name)
    for name in optional_tools:
        if not tools.get(name, {}).get("found"):
            missing_recommended.append(name)
    for name in required_modules:
        if not python_modules.get(name, {}).get("available"):
            missing_required.append(f"python:{name}")
    for name in optional_modules:
        if not python_modules.get(name, {}).get("available"):
            missing_recommended.append(f"python:{name}")
    for name, item in paths.items():
        if not item.get("writable"):
            missing_required.append(f"path:{name}")
    suggestions: List[str] = []
    if ssh_info.get("config_error"):
        suggestions.append("Local SSH config could not be inspected because Windows denied access; dashboard/scheduler may still work when launched from your normal user session.")
    elif not ssh_info["config_exists"]:
        suggestions.append("Create ~/.ssh/config entries for remote targets before using remote scheduling.")
    if "codex" in missing_recommended:
        suggestions.append("Install Codex CLI locally only if you want local Codex smoke tests; remote workers use remote Codex.")
    if "ollama" in missing_recommended:
        suggestions.append("Install Ollama locally if you want local LLM summaries/advisor output.")
    if "pueue" in missing_recommended:
        suggestions.append("Install Pueue only if you want local command queue fallback.")
    payload: Dict[str, object] = {
        "ok": not missing_required,
        "checked_at": checked_at,
        "profile_id": profile_id,
        "root": str(root()),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "user": user,
        "node": node,
        "create_dirs": create_dirs,
        "tools": tools,
        "python_modules": python_modules,
        "paths": paths,
        "ssh": ssh_info,
        "registry": {"path": str(registry_path()), "exists": registry_path().exists()},
        "missing_required": sorted(set(missing_required)),
        "missing_recommended": sorted(set(missing_recommended)),
        "suggestions": suggestions,
    }
    out_dir = _local_init_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "local_preflight.json"
    report_path = out_dir / "local_preflight.md"
    profile_path = out_dir / "local_profile.json"
    profile = {
        "profile_id": profile_id,
        "root": str(root()),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "created_at": checked_at,
        "local_init_ok": payload["ok"],
        "ssh": ssh_info,
    }
    payload["local_paths"] = {"dir": str(out_dir), "json": str(json_path), "report": str(report_path), "profile": str(profile_path)}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_local_init_report(payload), encoding="utf-8")
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def remote_target_preflight(
    host: str,
    remote_workdir: str,
    remote_root: str,
    allowed_gpu_ids: Optional[List[str]] = None,
    expected_uid: Optional[str] = None,
    expected_project_marker: str = ".solution_agent_project",
    timeout_seconds: int = 30,
) -> Dict[str, object]:
    checked_at = now_iso()
    errors: List[str] = []
    warnings: List[str] = []
    ssh_info: Dict[str, object] = {}
    if not shutil.which("ssh"):
        transport = _remote_transport_report(
            host=host,
            ssh_info={"stderr": "ssh not found on PATH"},
            remote_result={"returncode": 127, "stderr": "ssh not found on PATH"},
        )
        result = {
            "ok": False,
            "checked_at": checked_at,
            "host": host,
            "remote_workdir": remote_workdir,
            "remote_root": remote_root,
            "profile_id": remote_profile_id(host, remote_workdir),
            "errors": ["ssh not found on PATH"],
            "warnings": warnings,
            "ssh": ssh_info,
            "transport": transport,
            "remote": {},
            "gpus": [],
        }
        _last_remote_preflight_path().parent.mkdir(parents=True, exist_ok=True)
        _last_remote_preflight_path().write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    ssh_g = run_subprocess([*transport_command("ssh"), "-G", host], timeout=10)
    ssh_values = _parse_ssh_g_output(ssh_g.stdout)
    ssh_info = {
        "returncode": ssh_g.returncode,
        "stderr": ssh_g.stderr[:500],
        "hostname": ssh_values.get("hostname"),
        "user": ssh_values.get("user"),
        "port": ssh_values.get("port", "22"),
        "identityfile": ssh_values.get("identityfile"),
    }
    if ssh_g.returncode != 0:
        errors.append(f"ssh -G failed: {ssh_g.stderr.strip()[:300]}")
    workdir_q = shlex.quote(remote_workdir)
    root_q = shlex.quote(remote_root)
    marker_q = shlex.quote(expected_project_marker)
    command = "\n".join(
        [
            "set -u",
            f"WORKDIR={workdir_q}",
            f"ROOT={root_q}",
            f"MARKER={marker_q}",
            'printf "hostname=%s\\n" "$(hostname 2>/dev/null || true)"',
            'printf "uid=%s\\n" "$(id -u 2>/dev/null || true)"',
            'printf "user=%s\\n" "$(id -un 2>/dev/null || true)"',
            'if [ -d "$WORKDIR" ]; then echo "workdir_exists=yes"; else echo "workdir_exists=no"; fi',
            'mkdir -p "$ROOT" 2>/dev/null || true',
            'if [ -d "$ROOT" ]; then echo "root_exists=yes"; else echo "root_exists=no"; fi',
            'if [ -d "$ROOT" ] && [ -w "$ROOT" ]; then echo "root_writable=yes"; else echo "root_writable=no"; fi',
            'if command -v python3 >/dev/null 2>&1; then echo "python3=yes"; else echo "python3=no"; fi',
            'if command -v tmux >/dev/null 2>&1; then echo "tmux=yes"; else echo "tmux=no"; fi',
            'if command -v nvidia-smi >/dev/null 2>&1; then echo "nvidia_smi=yes"; else echo "nvidia_smi=no"; fi',
            'if [ -e "$WORKDIR/$MARKER" ]; then echo "project_marker=yes"; else echo "project_marker=no"; fi',
            'if command -v nvidia-smi >/dev/null 2>&1; then',
            '  echo "__gpu_begin__"',
            '  nvidia-smi --query-gpu=index,uuid,name --format=csv,noheader,nounits 2>/dev/null || true',
            '  echo "__gpu_end__"',
            "fi",
        ]
    )
    remote_result = run_ssh(host, command, timeout=timeout_seconds)
    transport = _remote_transport_report(
        host=host,
        ssh_info=ssh_info,
        remote_result=remote_result,
    )
    remote_values: Dict[str, str] = {}
    gpus: List[Dict[str, str]] = []
    in_gpu = False
    for raw_line in str(remote_result.get("stdout") or "").splitlines():
        line = raw_line.strip()
        if line == "__gpu_begin__":
            in_gpu = True
            continue
        if line == "__gpu_end__":
            in_gpu = False
            continue
        if in_gpu:
            fields = [field.strip() for field in line.split(",", 2)]
            if fields and fields[0]:
                gpus.append(
                    {
                        "index": fields[0],
                        "uuid": fields[1] if len(fields) > 1 else "",
                        "name": fields[2] if len(fields) > 2 else "",
                    }
                )
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            remote_values[key.strip()] = value.strip()
    remote_probe_ok = int(remote_result.get("returncode", 1) or 0) == 0
    if not remote_probe_ok:
        errors.append(f"remote preflight ssh failed: {str(remote_result.get('stderr') or '').strip()[:300]}")
    if remote_probe_ok:
        if remote_values.get("workdir_exists") != "yes":
            errors.append(f"remote_workdir does not exist: {remote_workdir}")
        if remote_values.get("root_exists") != "yes" or remote_values.get("root_writable") != "yes":
            errors.append(f"remote_output_root is not writable: {remote_root}")
        if remote_values.get("python3") != "yes":
            errors.append("python3 not found on remote PATH")
        if remote_values.get("tmux") != "yes":
            warnings.append("tmux not found on remote PATH; detached shell fallback may still work, but tmux runner features are unavailable")
    requested_gpu_ids = [str(item).strip() for item in (allowed_gpu_ids or []) if str(item).strip()]
    if remote_probe_ok and requested_gpu_ids and remote_values.get("nvidia_smi") != "yes":
        errors.append("nvidia-smi not found on remote PATH but GPU ids are configured")
    available_gpu_ids = {str(gpu.get("index")) for gpu in gpus}
    missing_gpu_ids = sorted([gpu_id for gpu_id in requested_gpu_ids if gpu_id not in available_gpu_ids]) if remote_probe_ok else []
    if missing_gpu_ids:
        errors.append(f"configured GPU ids not visible on remote: {', '.join(missing_gpu_ids)}")
    if remote_probe_ok and remote_values.get("project_marker") != "yes":
        warnings.append(f"project marker not found: {remote_workdir.rstrip('/')}/{expected_project_marker}")
    if remote_probe_ok and expected_uid is not None and str(remote_values.get("uid") or "") != str(expected_uid):
        errors.append(f"remote uid mismatch: expected {expected_uid}, got {remote_values.get('uid')}")
    profile = {
        "profile_id": remote_profile_id(host, remote_workdir, str(ssh_info.get("port") or "22"), remote_values.get("uid") or ""),
        "ssh_alias": host,
        "ssh_port": str(ssh_info.get("port") or "22"),
        "remote_workdir": remote_workdir,
        "remote_output_root": remote_root,
        "expected_uid": expected_uid,
        "expected_project_marker": expected_project_marker,
        "allowed_gpu_ids": requested_gpu_ids,
    }
    result = {
        "ok": not errors,
        "checked_at": checked_at,
        "host": host,
        "remote_workdir": remote_workdir,
        "remote_root": remote_root,
        "profile_id": profile["profile_id"],
        "profile": profile,
        "errors": errors,
        "warnings": warnings,
        "ssh": ssh_info,
        "transport": transport,
        "remote": remote_values,
        "gpus": gpus,
        "command": command,
    }
    _last_remote_preflight_path().parent.mkdir(parents=True, exist_ok=True)
    _last_remote_preflight_path().write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def remote_workplace_init(
    host: str,
    remote_workdir: str,
    remote_root: str,
    allowed_gpu_ids: Optional[List[str]] = None,
    expected_uid: Optional[str] = None,
    write_marker: bool = True,
    sync_profile: bool = True,
    sync_materializer_env: bool = False,
    codex_smoke: bool = False,
) -> Dict[str, object]:
    checked_at = now_iso()
    preflight = remote_target_preflight(
        host=host,
        remote_workdir=remote_workdir,
        remote_root=remote_root,
        allowed_gpu_ids=allowed_gpu_ids,
        expected_uid=expected_uid,
    )
    profile_id = str(preflight.get("profile_id") or remote_profile_id(host, remote_workdir))
    workdir_q = shlex.quote(remote_workdir)
    root_q = shlex.quote(remote_root)
    execution_profile = _remote_execution_profile_for(host, remote_workdir)
    conda_profile = execution_profile.get("conda") if isinstance(execution_profile.get("conda"), dict) else {}
    configured_conda_env = str(conda_profile.get("env") or execution_profile.get("conda_env") or "").strip()
    configured_conda_prefix = str(conda_profile.get("prefix") or execution_profile.get("conda_prefix") or "").strip()
    configured_conda_executable = str(conda_profile.get("executable") or execution_profile.get("conda_executable") or "").strip()
    configured_conda_env_q = shlex.quote(configured_conda_env)
    configured_conda_prefix_q = shlex.quote(configured_conda_prefix)
    configured_conda_executable_q = shlex.quote(configured_conda_executable)
    smoke_line = ""
    if codex_smoke:
        smoke_line = (
            'if [ -n "$CODEX_BIN" ]; then '
            'SMOKE_DIR="$ROOT/.codex_smoke"; mkdir -p "$SMOKE_DIR"; '
            'CODEX_SMOKE_OUT="$(cd "$SMOKE_DIR" && "$CODEX_BIN" exec --skip-git-repo-check "Say workplace-init-ok and write nothing else." 2>&1 | tail -n 5 | tr "\\n" " " | sed "s/[=]/:/g")"; '
            'printf "codex_smoke=%s\\n" "$CODEX_SMOKE_OUT"; '
            'else echo "codex_smoke=skipped_no_codex"; fi'
        )
    else:
        smoke_line = 'echo "codex_smoke=skipped"'
    probe_command = "\n".join(
        [
            "set -u",
            f"WORKDIR={workdir_q}",
            f"ROOT={root_q}",
            'printf "bash_path=%s\\n" "$(command -v bash 2>/dev/null || true)"',
            'printf "bash_version=%s\\n" "$(bash --version 2>/dev/null | head -1 | sed "s/[=]/:/g" || true)"',
            'printf "tar_path=%s\\n" "$(command -v tar 2>/dev/null || true)"',
            'printf "gzip_path=%s\\n" "$(command -v gzip 2>/dev/null || true)"',
            'printf "tmux_path=%s\\n" "$(command -v tmux 2>/dev/null || true)"',
            'printf "tmux_version=%s\\n" "$(tmux -V 2>/dev/null | sed "s/[=]/:/g" || true)"',
            'printf "git_path=%s\\n" "$(command -v git 2>/dev/null || true)"',
            'printf "git_version=%s\\n" "$(git --version 2>/dev/null | sed "s/[=]/:/g" || true)"',
            'printf "python3_path=%s\\n" "$(command -v python3 2>/dev/null || true)"',
            'printf "python_version=%s\\n" "$(python3 --version 2>/dev/null | sed "s/[=]/:/g" || true)"',
            'printf "nvidia_smi_path=%s\\n" "$(command -v nvidia-smi 2>/dev/null || true)"',
            'CODEX_BIN="$(command -v codex 2>/dev/null || true)"',
            'if [ -z "$CODEX_BIN" ] && [ -x "$HOME/.local/bin/codex" ]; then CODEX_BIN="$HOME/.local/bin/codex"; fi',
            'printf "codex_path=%s\\n" "$CODEX_BIN"',
            'if [ -n "$CODEX_BIN" ]; then printf "codex_version=%s\\n" "$("$CODEX_BIN" --version 2>/dev/null | head -1 | sed "s/[=]/:/g")"; else echo "codex_version="; fi',
            'printf "conda_path=%s\\n" "$(command -v conda 2>/dev/null || true)"',
            'printf "conda_version=%s\\n" "$(conda --version 2>/dev/null | sed "s/[=]/:/g" || true)"',
            'printf "conda_default_env=%s\\n" "${CONDA_DEFAULT_ENV:-}"',
            f'CONFIGURED_CONDA_ENV={configured_conda_env_q}',
            f'CONFIGURED_CONDA_PREFIX={configured_conda_prefix_q}',
            f'CONFIGURED_CONDA_EXE={configured_conda_executable_q}',
            'if [ -n "$CONFIGURED_CONDA_EXE" ]; then CONDA_RUN_BIN="$CONFIGURED_CONDA_EXE"; else CONDA_RUN_BIN="$(command -v conda 2>/dev/null || true)"; fi',
            'printf "configured_conda_env=%s\\n" "$CONFIGURED_CONDA_ENV"',
            'printf "configured_conda_prefix=%s\\n" "$CONFIGURED_CONDA_PREFIX"',
            'printf "configured_conda_executable=%s\\n" "$CONDA_RUN_BIN"',
            'if [ -z "$CONFIGURED_CONDA_ENV" ] && [ -z "$CONFIGURED_CONDA_PREFIX" ]; then echo "configured_conda_env_ready=not_configured"; '
            'elif [ -n "$CONDA_RUN_BIN" ]; then CONDA_PROBE_OUT="$(mktemp)"; CONDA_PROBE_ERR="$(mktemp)"; '
            'if [ -n "$CONFIGURED_CONDA_PREFIX" ]; then CONDA_SELECTOR="-p"; CONDA_TARGET="$CONFIGURED_CONDA_PREFIX"; else CONDA_SELECTOR="-n"; CONDA_TARGET="$CONFIGURED_CONDA_ENV"; fi; '
            'if "$CONDA_RUN_BIN" run "$CONDA_SELECTOR" "$CONDA_TARGET" python -c "import sys; print(sys.version.split()[0])" >"$CONDA_PROBE_OUT" 2>"$CONDA_PROBE_ERR"; then '
            'printf "configured_conda_env_ready=yes:%s\\n" "$(head -1 "$CONDA_PROBE_OUT" | sed "s/[=]/:/g")"; '
            'else printf "configured_conda_env_ready=no:%s\\n" "$(tail -1 "$CONDA_PROBE_ERR" 2>/dev/null | sed "s/[=]/:/g" || true)"; fi; '
            'rm -f "$CONDA_PROBE_OUT" "$CONDA_PROBE_ERR"; '
            'else echo "configured_conda_env_ready=no:conda_not_found"; fi',
            'printf "rsync_path=%s\\n" "$(command -v rsync 2>/dev/null || true)"',
            'printf "workdir_writable=%s\\n" "$(test -w "$WORKDIR" && echo yes || echo no)"',
            'printf "root_writable=%s\\n" "$(test -w "$ROOT" && echo yes || echo no)"',
            'printf "disk_workdir=%s\\n" "$(df -hP "$WORKDIR" 2>/dev/null | tail -1 | tr -s " " | sed "s/[=]/:/g" || true)"',
            'printf "disk_root=%s\\n" "$(df -hP "$ROOT" 2>/dev/null | tail -1 | tr -s " " | sed "s/[=]/:/g" || true)"',
            'if git -C "$WORKDIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then echo "git_inside_worktree=yes"; else echo "git_inside_worktree=no"; fi',
            'printf "git_branch=%s\\n" "$(git -C "$WORKDIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"',
            'printf "git_head=%s\\n" "$(git -C "$WORKDIR" rev-parse --short HEAD 2>/dev/null || true)"',
            'printf "git_dirty_count=%s\\n" "$(git -C "$WORKDIR" status --porcelain 2>/dev/null | wc -l | tr -d " " || true)"',
            smoke_line,
        ]
    )
    probe_result = run_ssh(host, probe_command, timeout=45 if not codex_smoke else 180)
    probe = _parse_key_value_stdout(str(probe_result.get("stdout") or ""))
    probe["returncode"] = str(probe_result.get("returncode"))
    if probe_result.get("stderr"):
        probe["stderr"] = str(probe_result.get("stderr"))[:1000]
    required_checks = {
        "bash": "bash_path",
        "tar": "tar_path",
        "gzip": "gzip_path",
        "python3": "python3_path",
        "git": "git_path",
        "nvidia-smi": "nvidia_smi_path",
    }
    recommended_checks = {
        "tmux": "tmux_path",
        "conda": "conda_path",
        "rsync": "rsync_path",
        "codex": "codex_path",
    }
    missing_required = [name for name, key in required_checks.items() if not str(probe.get(key) or "").strip()]
    missing_recommended = [name for name, key in recommended_checks.items() if not str(probe.get(key) or "").strip()]
    if int(probe_result.get("returncode", 1) or 0) != 0:
        missing_required.append("workplace_probe_ssh")
    if not preflight.get("ok"):
        missing_required.append("remote_preflight")
    configured_conda_target = configured_conda_prefix or configured_conda_env
    if configured_conda_target and not str(probe.get("configured_conda_env_ready") or "").startswith("yes"):
        missing_required.append(f"conda_env:{configured_conda_target}")
    suggestions: List[str] = []
    install_guidance: List[Dict[str, object]] = []
    security_warnings: List[str] = [
        "Do not store provider API keys on a host shared by multiple people under the same Unix user. Same-uid processes can read the same HOME files."
    ]
    if "codex" in missing_recommended:
        suggestions.append("Codex CLI is missing on the remote. This is acceptable for materialized_execution, but Codex fallback/debug will be unavailable.")
        install_guidance.append(
            {
                "code": "optional_codex_cli",
                "title": "Optional: install Codex CLI for fallback/debug",
                "detail": "Only needed if this workplace should support worker=codex or explicit Codex fallback.",
                "commands": [
                    "# Install Codex CLI using your normal approved method for this host.",
                    "# Then ensure `codex --version` works in a non-interactive SSH shell.",
                ],
            }
        )
    if "tmux" in missing_recommended:
        suggestions.append("Install tmux if you want durable remote sessions; current runner can still use detached shell fallback.")
        install_guidance.append(
            {
                "code": "optional_tmux",
                "title": "Optional: install tmux",
                "detail": "Useful for durable remote sessions; detached shell fallback can still work without it.",
                "commands": ["# Ubuntu/Debian example:", "sudo apt-get update && sudo apt-get install -y tmux"],
            }
        )
    if "conda" in missing_recommended and not configured_conda_target:
        suggestions.append("No conda executable was detected. materialized_execution can use python3, but configured conda env profiles need conda.")
    if configured_conda_target and f"conda_env:{configured_conda_target}" in missing_required:
        suggestions.append(f"Configured remote conda env is not usable: {configured_conda_target}. Create it or update config/remote_execution_profiles.yaml.")
        install_guidance.append(
            {
                "code": "configured_conda_env_missing",
                "title": f"Create or fix conda env `{configured_conda_target}`",
                "detail": "This env is configured for the workplace, so launches using this profile will fail until it works.",
                "commands": [
                    f"conda run {'-p' if configured_conda_prefix else '-n'} {shlex.quote(configured_conda_target)} python --version",
                    "# Install project-specific packages according to your experiment environment notes.",
                ],
            }
        )
    if not configured_conda_target:
        suggestions.append("No remote conda env is configured for this workplace profile. Future launches will use python3 unless task front matter sets remote_conda_env.")
        install_guidance.append(
            {
                "code": "configure_remote_execution_profile",
                "title": "Optional: record the remote conda env in config/remote_execution_profiles.yaml",
                "detail": "Recommended for new hosts so solution-agent does not guess the environment.",
                "commands": [
                    "profiles:",
                    f"  {host}:",
                    f"    host: {host}",
                    f"    remote_workdir: {remote_workdir}",
                    "    conda:",
                    "      env: YOUR_ENV_NAME",
                    "      executable: conda",
                ],
            }
        )
    if probe.get("git_inside_worktree") != "yes":
        suggestions.append("Remote workdir is not detected as a git worktree; Codex may need --skip-git-repo-check.")
    if (preflight.get("warnings") or []) and "project marker" in " ".join(str(item) for item in preflight.get("warnings", [])):
        suggestions.append("Run workplace init with marker writing enabled to identify this remote project root.")
    payload: Dict[str, object] = {
        "ok": not missing_required,
        "checked_at": checked_at,
        "host": host,
        "remote_workdir": remote_workdir,
        "remote_root": remote_root,
        "profile_id": profile_id,
        "preflight": preflight,
        "probe": probe,
        "probe_status": probe_result,
        "missing_required": sorted(set(missing_required)),
        "missing_recommended": sorted(set(missing_recommended)),
        "suggestions": suggestions,
        "install_guidance": install_guidance,
        "security_warnings": security_warnings,
        "execution_profile": execution_profile,
        "write_marker": write_marker,
        "sync_profile": sync_profile,
        "sync_materializer_env": sync_materializer_env,
        "codex_smoke": codex_smoke,
    }
    report_md = _render_workplace_init_report(payload)
    workspace_dir = _workplace_state_dir(profile_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    json_path = workspace_dir / "preflight.json"
    report_path = workspace_dir / "preflight.md"
    profile_path = workspace_dir / "workplace_profile.json"
    agent_context_yaml_path = workspace_dir / "agent_context.yaml"
    agent_context_md_path = workspace_dir / "agent_context.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(report_md, encoding="utf-8")
    profile = {
        "profile_id": profile_id,
        "host": host,
        "remote_workdir": remote_workdir,
        "remote_output_root": remote_root,
        "allowed_gpu_ids": [str(item).strip() for item in (allowed_gpu_ids or []) if str(item).strip()],
        "created_at": checked_at,
        "preflight_ok": payload["ok"],
        "remote_identity": preflight.get("remote"),
        "ssh": preflight.get("ssh"),
        "tool_paths": {key: probe.get(key) for key in sorted(probe) if key.endswith("_path")},
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    remote_sync: Dict[str, object] = {"skipped": True, "reason": "sync_profile disabled"}
    bootstrap: Dict[str, object] = {"skipped": True, "reason": "sync_profile disabled"}
    materializer_env: Dict[str, object] = {"skipped": True, "reason": "sync_profile disabled"}
    if sync_profile:
        bootstrap = remote_bootstrap_context(host, remote_workdir)
        if sync_materializer_env:
            materializer_env = _sync_remote_materializer_env(host, remote_workdir)
        else:
            materializer_env = _check_remote_materializer_env(host, remote_workdir, reason="sync_materializer_env disabled; remote env check only")
        if not materializer_env.get("ready"):
            payload["missing_required"] = sorted(set([*payload["missing_required"], "remote_materializer_env"]))
            payload["ok"] = False
            payload["suggestions"].append("Remote materializer API env is not ready. Sync ~/.remote_materializer/.env locally or create ~/.remote_materializer/.env on the remote.")
            payload["install_guidance"].append(
                {
                    "code": "remote_materializer_env_missing",
                    "title": "Set remote materializer API env",
                    "detail": "Needed for true materializer API calls on this host. Secrets are not printed in reports.",
                    "commands": [
                        "mkdir -p ~/.remote_materializer && chmod 700 ~/.remote_materializer",
                        "nano ~/.remote_materializer/.env",
                        "chmod 600 ~/.remote_materializer/.env",
                    ],
                }
            )
        payload["readiness_checks"] = _workplace_init_readiness_checks(payload)
        agent_context = _workplace_agent_context(payload)
        payload["agent_context"] = {
            "yaml": str(agent_context_yaml_path),
            "markdown": str(agent_context_md_path),
            "purpose": "sanitized token-bounded workplace summary for context packs and agent bootstrapping",
        }
        agent_context_yaml_path.write_text(yaml.safe_dump(agent_context, sort_keys=False, allow_unicode=True), encoding="utf-8")
        agent_context_md_path.write_text(_render_workplace_agent_context(agent_context), encoding="utf-8")
        remote_bundle_path = _write_tar_gz(
            workspace_dir / "workplace_init_bundle.tar.gz",
            {
                "workplace_profile.json": profile_path,
                "workplace_init_report.md": report_path,
                "agent_context.yaml": agent_context_yaml_path,
                "agent_context.md": agent_context_md_path,
            },
        )
        remote_bundle = f"/tmp/solution_agent_workplace_init_{_safe_profile_part(profile_id)}.tar.gz"
        copy_bundle = scp_to_remote(remote_bundle_path, host, remote_bundle)
        marker_command = ""
        if write_marker:
            marker_command = (
                f" && printf '%s\\n' {shlex.quote(f'solution-agent profile: {profile_id}')} "
                f"> {shlex.quote(remote_workdir.rstrip('/'))}/.solution_agent_project"
            )
        extract_command = (
            f"mkdir -p {shlex.quote(remote_workdir.rstrip('/') + '/.codex-orchestrator')} "
            f"&& tar -xzf {shlex.quote(remote_bundle)} -C {shlex.quote(remote_workdir.rstrip('/') + '/.codex-orchestrator')} "
            f"&& rm -f {shlex.quote(remote_bundle)}"
            f"{marker_command}"
        )
        extract_bundle = run_ssh(host, extract_command, timeout=30)
        remote_sync = {
            "bundle": {"local": str(remote_bundle_path), "remote": remote_bundle, "copy": copy_bundle, "extract": extract_bundle},
            "remote_profile": remote_workdir.rstrip("/") + "/.codex-orchestrator/workplace_profile.json",
            "remote_report": remote_workdir.rstrip("/") + "/.codex-orchestrator/workplace_init_report.md",
            "remote_agent_context": remote_workdir.rstrip("/") + "/.codex-orchestrator/agent_context.yaml",
            "remote_marker": remote_workdir.rstrip("/") + "/.solution_agent_project" if write_marker else None,
        }
    payload["bootstrap"] = bootstrap
    payload["remote_sync"] = remote_sync
    payload["remote_materializer_env"] = materializer_env
    payload["readiness_checks"] = _workplace_init_readiness_checks(payload)
    agent_context = _workplace_agent_context(payload)
    payload["agent_context"] = {
        "yaml": str(agent_context_yaml_path),
        "markdown": str(agent_context_md_path),
        "purpose": "sanitized token-bounded workplace summary for context packs and agent bootstrapping",
    }
    profile["preflight_ok"] = payload["ok"]
    payload["local_paths"] = {
        "dir": str(workspace_dir),
        "json": str(json_path),
        "report": str(report_path),
        "profile": str(profile_path),
        "agent_context_yaml": str(agent_context_yaml_path),
        "agent_context_md": str(agent_context_md_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    agent_context_yaml_path.write_text(yaml.safe_dump(agent_context, sort_keys=False, allow_unicode=True), encoding="utf-8")
    agent_context_md_path.write_text(_render_workplace_agent_context(agent_context), encoding="utf-8")
    report_path.write_text(_render_workplace_init_report(payload), encoding="utf-8")
    return payload


def _stop_local_pid(pid: int) -> Dict[str, object]:
    stopped = False
    stop_result: Dict[str, object] = {}
    if pid <= 0 or not _pid_alive(pid):
        return {"stopped": False, "pid": pid, "stop_result": stop_result}
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            if not _pid_alive(pid):
                stopped = True
                break
    except OSError:
        stopped = False
    if not stopped and os.name == "nt":
        completed = run_subprocess(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
        )
        stop_result = {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "method": "taskkill"}
        stopped = completed.returncode == 0 or not _pid_alive(pid)
    if not stopped and os.name == "nt":
        completed = run_subprocess(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction Stop"],
        )
        stop_result = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "method": "powershell Stop-Process",
        }
        stopped = completed.returncode == 0 or not _pid_alive(pid)
    return {"stopped": stopped, "pid": pid, "stop_result": stop_result}


def acquire_scheduler_lock(owner: str) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    lock_path = _scheduler_lock_file()
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        existing_pid = int(existing.get("pid") or 0)
        if _pid_alive(existing_pid):
            return {"acquired": False, "lock_path": str(lock_path), "existing": existing}
    payload = {"owner": owner, "pid": os.getpid(), "started_at": now_iso(), "heartbeat_at": now_iso()}
    lock_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"acquired": True, "lock_path": str(lock_path), "lock": payload}


def refresh_scheduler_lock() -> None:
    lock_path = _scheduler_lock_file()
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if int(payload.get("pid") or 0) != os.getpid():
        return
    payload["heartbeat_at"] = now_iso()
    lock_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def release_scheduler_lock() -> None:
    lock_path = _scheduler_lock_file()
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if int(payload.get("pid") or 0) == os.getpid():
        lock_path.unlink()


def _acquire_remote_watch_lock(owner: str) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    lock_path = _remote_watch_lock_file()
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        existing_pid = int(existing.get("pid") or 0)
        if _pid_alive(existing_pid):
            return {"acquired": False, "lock_path": str(lock_path), "existing": existing}
    payload = {"owner": owner, "pid": os.getpid(), "started_at": now_iso(), "heartbeat_at": now_iso()}
    lock_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"acquired": True, "lock_path": str(lock_path), "lock": payload}


def _refresh_remote_watch_lock() -> None:
    lock_path = _remote_watch_lock_file()
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if int(payload.get("pid") or 0) != os.getpid():
        return
    payload["heartbeat_at"] = now_iso()
    lock_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _release_remote_watch_lock() -> None:
    lock_path = _remote_watch_lock_file()
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if int(payload.get("pid") or 0) == os.getpid():
        lock_path.unlink()


def _write_remote_watch_state(payload: Dict[str, object]) -> None:
    path = _remote_watch_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def remote_watch_status() -> Dict[str, object]:
    lock_path = _remote_watch_lock_file()
    state_path = _remote_watch_state_file()
    state: Dict[str, object] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}
    if not lock_path.exists():
        return {"running": False, "healthy": False, "stale": False, "lock_path": str(lock_path), "lock": None, "state": state}
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        lock = {}
    pid = int(lock.get("pid") or 0)
    running = _pid_alive(pid)
    heartbeat_age = _iso_age_seconds(lock.get("heartbeat_at"))
    stale = bool(running and heartbeat_age is not None and heartbeat_age > 180)
    return {
        "running": running,
        "healthy": bool(running and not stale),
        "stale": stale,
        "heartbeat_age_seconds": heartbeat_age,
        "lock_path": str(lock_path),
        "lock": lock,
        "state": state,
    }


def remote_watch_stop() -> Dict[str, object]:
    status_payload = remote_watch_status()
    lock_path = Path(str(status_payload["lock_path"]))
    lock = status_payload.get("lock") or {}
    pid = int(lock.get("pid") or 0) if isinstance(lock, dict) else 0
    stop_payload = _stop_local_pid(pid) if pid and status_payload.get("running") else {"stopped": False, "stop_result": {}}
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass
    state = {
        "state": "stopped",
        "stopped_at": now_iso(),
        "pid": pid,
        "stop_result": stop_payload.get("stop_result"),
    }
    _write_remote_watch_state(state)
    return {"stopped": stop_payload.get("stopped"), "pid": pid, "lock_path": str(lock_path), "stop_result": stop_payload.get("stop_result")}


def scheduler_status() -> Dict[str, object]:
    lock_path = _scheduler_lock_file()
    if not lock_path.exists():
        return {"running": False, "lock_path": str(lock_path), "lock": None}
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        payload = {}
    pid = int(payload.get("pid") or 0)
    running = _pid_alive(pid)
    heartbeat_age = _iso_age_seconds(payload.get("heartbeat_at"))
    stale = bool(running and heartbeat_age is not None and heartbeat_age > 180)
    return {
        "running": running,
        "healthy": bool(running and not stale),
        "stale": stale,
        "heartbeat_age_seconds": heartbeat_age,
        "lock_path": str(lock_path),
        "lock": payload,
    }


def v1_stability_report() -> Dict[str, object]:
    """Summarize the V1 long-running health signals without mutating state."""
    ensure_orchestrator_dirs()
    registry = load_registry()
    tasks = registry.get("tasks", {}) if isinstance(registry.get("tasks"), dict) else {}
    status_counts: Dict[str, int] = {}
    worker_counts: Dict[str, int] = {}
    active_statuses = {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}
    terminal_statuses = {"completed", "partial", "failed", "manual_pause"}
    remote_summary: Dict[str, object] = {
        "total_records": 0,
        "active_records": 0,
        "terminal_records": 0,
        "by_worker": {},
        "unknown_health_active": 0,
    }
    stale_active: List[str] = []

    for task_id, record in sorted(tasks.items()):
        if not isinstance(record, dict):
            continue
        status_text = str(record.get("status") or "unknown").strip().lower() or "unknown"
        status_counts[status_text] = status_counts.get(status_text, 0) + 1
        worker = str(record.get("worker") or "unknown").strip() or "unknown"
        worker_counts[worker] = worker_counts.get(worker, 0) + 1

        has_remote = any(record.get(key) for key in ("remote", "remote_run", "remote_state"))
        runs = record.get("runs") if isinstance(record.get("runs"), list) else []
        has_remote = has_remote or any(isinstance(run, dict) and run.get("remote_task_dir") for run in runs)
        if not has_remote:
            continue

        remote_summary["total_records"] = int(remote_summary["total_records"]) + 1
        by_worker = remote_summary["by_worker"]
        if isinstance(by_worker, dict):
            by_worker[worker] = int(by_worker.get(worker) or 0) + 1
        if status_text in active_statuses:
            remote_summary["active_records"] = int(remote_summary["active_records"]) + 1
            remote_health = str(record.get("remote_health") or record.get("remote_status") or "").strip().lower()
            if remote_health in {"unknown", "unreachable", "stale", "lost_process"}:
                remote_summary["unknown_health_active"] = int(remote_summary["unknown_health_active"]) + 1
            latest_run = _latest_run_record(record)
            heartbeat_age = None
            if latest_run:
                heartbeat_age = _iso_age_seconds(latest_run.get("heartbeat_at") or latest_run.get("remote_heartbeat_at"))
            if heartbeat_age is not None and heartbeat_age > 900:
                stale_active.append(str(task_id))
        elif status_text in terminal_statuses:
            remote_summary["terminal_records"] = int(remote_summary["terminal_records"]) + 1

    queue_items = queue()
    ready_count = sum(1 for item in queue_items if bool(item.get("ready")))
    blocked_count = len(queue_items) - ready_count
    scheduler = scheduler_status()
    watcher = remote_watch_status()
    hygiene = registry_hygiene(apply_safe=False)

    issues: List[Dict[str, object]] = []
    warnings: List[Dict[str, object]] = []
    if hygiene.get("issue_count"):
        issues.append({"code": "registry_hygiene_issues", "issue_count": hygiene.get("issue_count")})
    if scheduler.get("running") and not scheduler.get("healthy", True):
        issues.append({"code": "scheduler_unhealthy", "stale": scheduler.get("stale"), "heartbeat_age_seconds": scheduler.get("heartbeat_age_seconds")})
    elif not scheduler.get("running"):
        warnings.append({"code": "scheduler_not_running", "message": "scheduler daemon is not active; background scheduling will not advance automatically"})
    if watcher.get("running") and not watcher.get("healthy", True):
        issues.append({"code": "remote_watch_unhealthy", "stale": watcher.get("stale"), "heartbeat_age_seconds": watcher.get("heartbeat_age_seconds")})
    elif not watcher.get("running"):
        warnings.append({"code": "remote_watch_not_running", "message": "GPU watcher is not active; dashboard may rely on cached or one-shot GPU snapshots"})
    if remote_summary.get("unknown_health_active"):
        issues.append({"code": "active_remote_health_unknown", "count": remote_summary.get("unknown_health_active")})
    if stale_active:
        issues.append({"code": "stale_active_remote_heartbeat", "task_ids": stale_active[:20], "truncated": len(stale_active) > 20})

    status_text = "ok" if not issues else "attention"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "status": status_text,
        "ok": not issues,
        "baseline_version": V1_BASELINE_VERSION,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "remote_runtime_version": REMOTE_RUNTIME_VERSION,
        "task_summary": {
            "total": len([record for record in tasks.values() if isinstance(record, dict)]),
            "by_status": dict(sorted(status_counts.items())),
            "by_worker": dict(sorted(worker_counts.items())),
        },
        "queue_summary": {
            "pending": len(queue_items),
            "ready": ready_count,
            "blocked": blocked_count,
        },
        "remote_summary": remote_summary,
        "scheduler": scheduler,
        "remote_watch": watcher,
        "registry_hygiene": {
            "issue_count": hygiene.get("issue_count", 0),
            "fixable_count": hygiene.get("fixable_count", 0),
        },
        "issues": issues,
        "warnings": warnings,
        "recommended_actions": _v1_stability_recommended_actions(issues, warnings),
    }


def _v1_stability_recommended_actions(issues: List[Dict[str, object]], warnings: List[Dict[str, object]]) -> List[str]:
    codes = {str(item.get("code")) for item in [*issues, *warnings] if isinstance(item, dict)}
    actions: List[str] = []
    if "registry_hygiene_issues" in codes:
        actions.append("Run `python cli.py registry-hygiene --apply-safe`, then rerun `python cli.py v1-stability-report`.")
    if "scheduler_unhealthy" in codes:
        actions.append("Stop and restart the scheduler daemon after confirming no duplicate scheduler process is active.")
    if "scheduler_not_running" in codes:
        actions.append("Start the scheduler daemon before unattended background execution.")
    if "remote_watch_unhealthy" in codes:
        actions.append("Restart the remote GPU watcher or fall back to one-shot GPU snapshots.")
    if "remote_watch_not_running" in codes:
        actions.append("Start the remote GPU watcher before unattended soak if live GPU capacity matters.")
    if "active_remote_health_unknown" in codes or "stale_active_remote_heartbeat" in codes:
        actions.append("Run `python cli.py supervise-running-remote --collect-finished` and inspect stale remote task directories.")
    if not actions:
        actions.append("No immediate V1 stability action required.")
    return actions


def local_maintenance_report() -> Dict[str, object]:
    """Classify local maintenance state without mutating files.

    This report is intentionally read-only. It should help decide whether a
    future cleanup/migration action is needed, without silently repairing old
    artifacts during normal V1 operation.
    """
    from solution_agent.remote_materializer.api_metrics import summarize_api_metrics
    from solution_agent.context_index import context_index_status

    repo = root()
    issues: List[Dict[str, object]] = []
    warnings: List[Dict[str, object]] = []
    observations: List[Dict[str, object]] = []

    try:
        inbox_state = scan_inbox_staging()
    except Exception as exc:
        inbox_state = {"lines": [], "anomalies": []}
        issues.append({"code": "inbox_scan_failed", "message": f"{type(exc).__name__}: {exc}"})
    anomalies = inbox_state.get("anomalies") if isinstance(inbox_state, dict) else []
    lines = inbox_state.get("lines") if isinstance(inbox_state, dict) else []
    if anomalies:
        warnings.append(
            {
                "code": "inbox_anomalies_present",
                "count": len(anomalies),
                "examples": [str(item.get("name") or item.get("path") or "") for item in anomalies[:5] if isinstance(item, dict)],
            }
        )
    observations.append({"code": "inbox_lines", "count": len(lines or [])})

    def path_size_bytes(path: Path) -> int:
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total

    cache_candidates: List[str] = []
    for candidate in (repo / ".pytest_cache", repo / ".test_tmp"):
        if candidate.exists():
            cache_candidates.append(str(candidate))
    state = state_dir()
    remote_context_paths: List[Path] = []
    if state.exists():
        for item in sorted(state.glob("pytest-cache-files-*")):
            cache_candidates.append(str(item))
        if (state / "pytest_cache").exists():
            cache_candidates.append(str(state / "pytest_cache"))
        remote_context_paths = sorted(state.glob("remote_context_*"))
    if cache_candidates:
        warnings.append(
            {
                "code": "local_test_cache_residue",
                "count": len(cache_candidates),
                "paths": cache_candidates[:10],
                "note": "Safe to review later; pytest.ini avoids collecting these as tests.",
            }
        )
    remote_context_size = sum(path_size_bytes(path) for path in remote_context_paths)
    if len(remote_context_paths) >= 20:
        warnings.append(
            {
                "code": "remote_context_cache_accumulating",
                "count": len(remote_context_paths),
                "size_bytes": remote_context_size,
                "examples": [str(path) for path in remote_context_paths[:5]],
                "note": "Read-only observation. Do not delete until a retention policy is chosen.",
            }
        )
    remote_auth_dir = state / "remote_auth"
    remote_auth_tokens: List[Path] = []
    remote_auth_staged_envs: List[Path] = []
    if remote_auth_dir.exists():
        remote_auth_tokens = sorted(remote_auth_dir.glob("*.token"))
        remote_auth_staged_envs = sorted(remote_auth_dir.glob("*.remote_materializer.env"))
    if remote_auth_staged_envs:
        warnings.append(
            {
                "code": "remote_auth_staged_env_residue",
                "count": len(remote_auth_staged_envs),
                "paths": [str(path) for path in remote_auth_staged_envs[:10]],
                "note": "These are temporary copies of API env files and should not persist after workplace-init.",
            }
        )
    if remote_auth_tokens:
        observations.append(
            {
                "code": "remote_auth_tokens",
                "count": len(remote_auth_tokens),
                "note": "Expected for workplaces using HMAC remote materializer auth; token file contents are not included.",
            }
        )

    profile_path = remote_execution_profiles_path()
    profile_summary: Dict[str, object] = {"path": str(profile_path), "exists": profile_path.exists(), "profiles": 0}
    if profile_path.exists():
        try:
            payload = yaml.safe_load(profile_path.read_text(encoding="utf-8-sig")) or {}
            profiles = payload.get("profiles") if isinstance(payload, dict) and isinstance(payload.get("profiles"), dict) else {}
            profile_summary["profiles"] = len(profiles)
            invalid_profiles = []
            for name, profile in profiles.items():
                if not isinstance(profile, dict):
                    invalid_profiles.append(str(name))
                    continue
                if not (profile.get("host") or name):
                    invalid_profiles.append(str(name))
            if invalid_profiles:
                issues.append({"code": "invalid_remote_execution_profiles", "profiles": invalid_profiles[:10]})
        except Exception as exc:
            issues.append({"code": "remote_execution_profiles_unreadable", "message": f"{type(exc).__name__}: {exc}"})
    else:
        observations.append({"code": "remote_execution_profiles_missing", "message": "No profile file yet; new hosts can be initialized later."})

    api_metrics = summarize_api_metrics()
    if not api_metrics.get("total_calls"):
        observations.append({"code": "api_metrics_no_samples", "message": "API metrics pipeline is ready but has no live call samples yet."})
    context_status = context_index_status(repo)
    stale = context_status.get("stale") if isinstance(context_status.get("stale"), dict) else {}
    if stale.get("dirty"):
        warnings.append(
            {
                "code": "context_index_dirty",
                "documents_stale": bool(stale.get("documents_stale")),
                "index_stale": bool(stale.get("index_stale")),
                "note": "Context index is manual and will not rebuild in the background. Run: python cli.py context-index build",
            }
        )
    else:
        observations.append({"code": "context_index_current", "indexed_docs": context_status.get("indexed_docs", 0)})

    hygiene = registry_hygiene(apply_safe=False)
    if hygiene.get("issue_count"):
        issues.append(
            {
                "code": "registry_hygiene_issues",
                "issue_count": hygiene.get("issue_count"),
                "fixable_count": hygiene.get("fixable_count"),
            }
        )

    status_text = "ok" if not issues else "attention"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "status": status_text,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "observations": observations,
        "inbox": {
            "line_count": len(lines or []),
            "anomaly_count": len(anomalies or []),
        },
        "local_residue": {
            "cache_candidate_count": len(cache_candidates),
            "cache_candidates": cache_candidates[:20],
            "remote_context_cache_count": len(remote_context_paths),
            "remote_context_cache_size_bytes": remote_context_size,
            "remote_auth_token_count": len(remote_auth_tokens),
            "remote_auth_staged_env_count": len(remote_auth_staged_envs),
        },
        "remote_execution_profiles": profile_summary,
        "api_model_metrics": {
            "path": api_metrics.get("path"),
            "total_calls": api_metrics.get("total_calls", 0),
            "group_count": len(api_metrics.get("groups") or []),
        },
        "context_index": context_status,
        "registry_hygiene": {
            "issue_count": hygiene.get("issue_count", 0),
            "fixable_count": hygiene.get("fixable_count", 0),
        },
    }


def scheduler_stop() -> Dict[str, object]:
    status_payload = scheduler_status()
    lock_path = Path(str(status_payload["lock_path"]))
    lock = status_payload.get("lock") or {}
    pid = int(lock.get("pid") or 0) if isinstance(lock, dict) else 0
    stop_payload = _stop_local_pid(pid) if pid and status_payload.get("running") else {"stopped": False, "stop_result": {}}
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass
    return {"stopped": stop_payload.get("stopped"), "pid": pid, "lock_path": str(lock_path), "stop_result": stop_payload.get("stop_result")}


def scheduler_start(
    host: str,
    remote_root: str,
    remote_workdir: str,
    max_starts_per_cycle: int = 4,
    max_running_total: int = 8,
    max_running_gpu: int = 8,
    max_running_cpu: int = 2,
    min_free_gpu_mem_gb: float = 10.0,
    allowed_gpu_ids: str = "0,1,2,4",
    max_tasks_per_gpu: int = 2,
    interval_minutes: int = 15,
    max_cycles: int = 0,
    model: str = DEFAULT_OLLAMA_MODEL,
    gpu_snapshot_cache_seconds: int = 120,
    worker: str = "materialized_execution",
    preflight: bool = True,
    v1_stability_check: bool = True,
) -> Dict[str, object]:
    current = scheduler_status()
    if current.get("running"):
        return {"started": False, "reason": "scheduler already running", "status": current}
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    guard = scheduler_startup_guard(
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        max_starts=max_starts_per_cycle,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed,
        max_tasks_per_gpu=max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=gpu_snapshot_cache_seconds,
        worker=worker,
    )
    if not guard.get("ok"):
        return {"started": False, "reason": "startup guard failed", "startup_guard": guard}
    stability: Optional[Dict[str, object]] = None
    if v1_stability_check:
        stability = v1_stability_report()
        if not stability.get("ok"):
            return {
                "started": False,
                "reason": "v1 stability check failed",
                "v1_stability_report": stability,
                "startup_guard": guard,
            }
    preflight_result: Optional[Dict[str, object]] = None
    if preflight:
        preflight_result = remote_target_preflight(
            host=host,
            remote_workdir=remote_workdir,
            remote_root=remote_root,
            allowed_gpu_ids=allowed,
        )
        if not preflight_result.get("ok"):
            return {
                "started": False,
                "reason": "remote preflight failed",
                "preflight": preflight_result,
            }
    log_dir = state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "scheduler_stdout.log"
    stderr_path = log_dir / "scheduler_stderr.log"
    command = [
        sys.executable,
        "-u",
        "-m",
        "solution_agent.scheduler_daemon",
        "--host",
        host,
        "--remote-root",
        remote_root,
        "--remote-workdir",
        remote_workdir,
        "--max-starts-per-cycle",
        str(max_starts_per_cycle),
        "--max-running-total",
        str(max_running_total),
        "--max-running-gpu",
        str(max_running_gpu),
        "--max-running-cpu",
        str(max_running_cpu),
        "--min-free-gpu-mem-gb",
        str(min_free_gpu_mem_gb),
        "--allowed-gpu-ids",
        allowed_gpu_ids,
        "--max-tasks-per-gpu",
        str(max_tasks_per_gpu),
        "--interval-minutes",
        str(interval_minutes),
        "--max-cycles",
        str(max_cycles),
        "--model",
        model,
        "--gpu-cache-seconds",
        str(gpu_snapshot_cache_seconds),
        "--worker",
        worker,
        "--log-dir",
        str(log_dir),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(root()),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            env=env,
            **hidden_subprocess_kwargs(new_process_group=True, detached=True),
        )
    return {
        "started": True,
        "pid": process.pid,
        "command": command,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "preflight": preflight_result,
        "v1_stability_report": stability,
    }


def init_workspace() -> Dict[str, str]:
    ensure_orchestrator_dirs()
    reg = ensure_registry()
    ensure_local_memory_files()
    return {"registry": str(reg)}


def new_task(task_id: str, title: str, worker: str = "codex") -> Path:
    ensure_orchestrator_dirs()
    target = task_file("pending", task_id)
    if target.exists():
        raise FileExistsError(f"task already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_new_task(task_id, title, worker=worker), encoding="utf-8")
    task = load_task(target)
    upsert_task(task, status="pending")
    return target


def _is_inbox_source(path: Path) -> bool:
    try:
        path.resolve().relative_to((root() / "tasks" / "inbox").resolve())
        return True
    except ValueError:
        return False


def _should_ignore_inbox_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("task_batch_index") or name.startswith("readme")


def _archive_inbox_source(md_path: Path, task_id: str) -> Optional[Path]:
    if not md_path.exists() or not _is_inbox_source(md_path):
        return None
    processed = root() / "tasks" / "inbox" / "processed" / task_id
    processed.mkdir(parents=True, exist_ok=True)
    target = processed / md_path.name
    if target.exists():
        target = processed / f"{md_path.stem}.{_timestamp_id()}{md_path.suffix}"
    md_path.rename(target)
    return target


def _known_task_status(task_id: str) -> Optional[str]:
    for status_name in ("pending", "running", "completed", "partial", "failed"):
        if task_file(status_name, task_id).exists():
            return status_name
    if task_id in load_registry().get("tasks", {}):
        return str(load_registry().get("tasks", {}).get(task_id, {}).get("status") or "registered")
    return None


def ingest(source: Path, worker: str = "codex", move: bool = False) -> List[Dict[str, str]]:
    ensure_orchestrator_dirs()
    source = source.resolve()
    if source.is_dir():
        files = [path for path in sorted(source.glob("*.md")) if not (_is_inbox_source(path) and _should_ignore_inbox_file(path))]
    elif source.is_file() and source.suffix.lower() == ".md":
        files = [source]
    else:
        raise FileNotFoundError(f"no Markdown task source found: {source}")

    imported: List[Dict[str, str]] = []
    for md_path in files:
        task_markdown = ensure_task_front_matter(md_path, worker=worker)
        tmp = root() / ".ingest_tmp.md"
        tmp.write_text(task_markdown, encoding="utf-8")
        try:
            task = load_task(tmp)
        finally:
            if tmp.exists():
                tmp.unlink()
        known_status = _known_task_status(task.task_id)
        if known_status:
            archived = _archive_inbox_source(md_path, task.task_id) if (move or _is_inbox_source(md_path)) else None
            imported.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": "skipped_existing",
                    "existing_status": known_status,
                    "archived_source": str(archived) if archived else "",
                }
            )
            continue
        target = task_file("pending", task.task_id)
        if target.exists():
            raise FileExistsError(f"pending task already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(task_markdown, encoding="utf-8")
        loaded = load_task(target)
        upsert_task(loaded, status="pending")
        archived = _archive_inbox_source(md_path, loaded.task_id) if (move or _is_inbox_source(md_path)) else None
        imported.append({"task_id": loaded.task_id, "title": loaded.title, "path": str(target), "archived_source": str(archived) if archived else ""})
    return imported


def archive_processed_inbox() -> List[Dict[str, str]]:
    ensure_orchestrator_dirs()
    archived_items: List[Dict[str, str]] = []
    inbox = root() / "tasks" / "inbox"
    for md_path in sorted(inbox.glob("*.md")):
        try:
            task_markdown = ensure_task_front_matter(md_path)
            tmp = root() / ".inbox_archive_tmp.md"
            tmp.write_text(task_markdown, encoding="utf-8")
            try:
                task = load_task(tmp)
            finally:
                if tmp.exists():
                    tmp.unlink()
        except Exception as exc:
            archived_items.append({"path": str(md_path), "status": "left_in_place", "reason": str(exc)})
            continue
        known_status = _known_task_status(task.task_id)
        if known_status and known_status != "pending":
            archived = _archive_inbox_source(md_path, task.task_id)
            archived_items.append(
                {
                    "task_id": task.task_id,
                    "existing_status": known_status,
                    "status": "archived",
                    "archived_source": str(archived) if archived else "",
                }
            )
        else:
            archived_items.append({"task_id": task.task_id, "existing_status": known_status or "", "status": "left_in_place", "path": str(md_path)})
    return archived_items


# ---------------------------------------------------------------------------
# Inbox staging
# ---------------------------------------------------------------------------
#
# Staging treats `tasks/inbox/` as a candidate area where the user can drop
# zip files or sub-folders; the dashboard surfaces them so the user can
# explicitly Promote (move into `tasks/pending/` and archive into
# `tasks/inbox/processed/<index_root>/`) or Discard (move into
# `tasks/inbox/_discarded/<timestamp>__<name>/`).
#
# Hard rule: a "line" is only valid if its container holds at least one
# `TASK_BATCH_INDEX*.md` (case-insensitive). Anything else is reported as an
# anomaly and cannot be promoted from the dashboard.


def _is_batch_index_name(name: str) -> bool:
    return name.lower().startswith("task_batch_index") and name.lower().endswith(".md")


def _is_inbox_readme_name(name: str) -> bool:
    return name.lower().startswith("readme") and name.lower().endswith(".md")


def _index_root_from_index_name(index_filename: str) -> str:
    # `TASK_BATCH_INDEX_RECIPE_ABLATION.md` -> `TASK_BATCH_INDEX_RECIPE_ABLATION`
    stem = Path(index_filename).stem
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", stem).strip("_")
    return cleaned or "TASK_BATCH_INDEX"


def _load_task_from_text(markdown_text: str, fallback_id: str) -> Optional[Any]:
    """Parse a markdown task book from raw text. Returns None on failure."""
    text = markdown_text.lstrip("\ufeff")
    if not _has_explicit_front_matter(text):
        # Wrap the raw markdown with a synthetic header so load_task accepts it.
        title = fallback_id.replace("_", " ").replace("-", " ").strip() or fallback_id
        text = (
            "---\n"
            f"task_id: {fallback_id}\n"
            f"title: {title}\n"
            "priority: normal\n"
            "worker: codex\n"
            "status: pending\n"
            f"created_at: {now_iso()}\n"
            "objective: >\n"
            "  Execute the Markdown task book and collect the required outputs.\n"
            "success_criteria:\n"
            "  - Produce result.yaml\n"
            "  - Produce worker_notes.md\n"
            "  - Produce stdout.log and stderr.log\n"
            "expected_outputs:\n"
            "  - result.yaml\n"
            "  - worker_notes.md\n"
            "  - metrics.json\n"
            "  - patch.diff\n"
            "  - stdout.log\n"
            "  - stderr.log\n"
            "---\n\n" + markdown_text
        )
    tmp = root() / f".staging_scan_tmp_{uuid.uuid4().hex}.md"
    tmp.write_text(text, encoding="utf-8")
    try:
        return load_task(tmp)
    except Exception:
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _has_explicit_front_matter(markdown_text: Optional[str]) -> bool:
    if not markdown_text:
        return False
    return markdown_text.lstrip("\ufeff").lstrip().startswith("---")


_INBOX_HARD_FORBIDDEN_TRUE_FLAGS = {
    "materialized_execution_bypass_materializer",
    "disable_execution_plan_gate",
}

_INBOX_RISKY_TRUE_FLAGS = {
    "allow_heavy_data_scan",
    "allow_recursive_dataset_scan",
    "allow_image_header_scan",
    "allow_image_pixel_access",
    "allow_data_copy",
    "allow_dataset_modification",
}


def _truthy_front_matter_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _inbox_task_lint(task: Any, *, explicit_front_matter: bool) -> Dict[str, List[str]]:
    """Return conservative inbox lint for GPT/API-authored task books.

    Inbox lint is intentionally about launch hygiene, not task correctness. It
    catches fields that would bypass the normal materializer/supervision path
    before a task is promoted into pending.
    """
    meta = dict(getattr(task, "raw_front_matter", {}) or {})
    errors: List[str] = []
    warnings: List[str] = []
    if not explicit_front_matter:
        warnings.append("task file has no explicit YAML front matter; promote would synthesize defaults")
    for key in sorted(_INBOX_HARD_FORBIDDEN_TRUE_FLAGS):
        if _truthy_front_matter_value(meta.get(key)):
            errors.append(f"{key} is not allowed in inbox tasks")
    for key in sorted(_INBOX_RISKY_TRUE_FLAGS):
        if _truthy_front_matter_value(meta.get(key)):
            warnings.append(f"{key}=true requires explicit human approval")
    worker = str(meta.get("worker") or getattr(task, "worker", "") or "").strip()
    if not worker:
        warnings.append("worker is missing; use materialized_execution or codex explicitly")
    elif worker == "remote_materializer":
        errors.append("worker: remote_materializer is a component, not a full execution worker")
    elif worker not in {"materialized_execution", "codex"}:
        warnings.append(f"worker {worker!r} is unusual for inbox promotion")
    if worker == "materialized_execution" and not str(meta.get("allow_codex_fallback", "")).strip():
        warnings.append("allow_codex_fallback is not explicit; default is false")
    return {"errors": errors, "warnings": warnings}


def _zip_md_entries(zip_path: Path) -> List[str]:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return [
                name
                for name in zf.namelist()
                if name.lower().endswith(".md") and not name.endswith("/")
            ]
    except (zipfile.BadZipFile, OSError):
        return []


def _zip_read_text(zip_path: Path, entry: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(entry) as fh:
                return fh.read().decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None


def _zip_parent(entry: str) -> str:
    normalized = entry.replace("\\", "/").strip("/")
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def _zip_scope_matches(entry: str, scope: str) -> bool:
    normalized = entry.replace("\\", "/").strip("/")
    scope = scope.strip("/")
    if not scope:
        return "/" not in normalized
    return normalized.startswith(scope + "/")


def _line_name(container_name: str, index_root: str, total_lines: int) -> str:
    return container_name if total_lines <= 1 else f"{container_name} / {index_root}"


def _line_key(kind: str, container_name: str, scope: str, index_root: str) -> str:
    cleaned_scope = re.sub(r"[^A-Za-z0-9_\-./]+", "_", scope or ".").strip("_") or "."
    return f"{kind}:{container_name}:{cleaned_scope}:{index_root}"


def _scan_zip_container(zip_path: Path) -> List[Dict[str, object]]:
    md_entries = _zip_md_entries(zip_path)
    index_entries = [name for name in md_entries if _is_batch_index_name(Path(name).name)]
    task_entries = [name for name in md_entries if not _is_batch_index_name(Path(name).name)]
    if not index_entries:
        return [{
            "kind": "zip",
            "path": str(zip_path),
            "name": zip_path.stem,
            "valid": False,
            "reason": "missing TASK_BATCH_INDEX*.md",
            "task_count": len(task_entries),
        }]

    scope_to_indexes: Dict[str, List[str]] = {}
    for entry in sorted(index_entries):
        scope_to_indexes.setdefault(_zip_parent(entry), []).append(entry)
    total_lines = len(scope_to_indexes)

    assigned_tasks: Dict[str, List[str]] = {scope: [] for scope in scope_to_indexes}
    for entry in sorted(task_entries):
        matching_scopes = [scope for scope in scope_to_indexes if _zip_scope_matches(entry, scope)]
        if not matching_scopes:
            continue
        best_scope = max(matching_scopes, key=len)
        assigned_tasks.setdefault(best_scope, []).append(entry)

    lines: List[Dict[str, object]] = []
    for scope, scoped_indexes in sorted(scope_to_indexes.items(), key=lambda item: item[0].lower()):
        index_name = Path(scoped_indexes[0]).name
        index_root = _index_root_from_index_name(index_name)
        tasks: List[Dict[str, object]] = []
        for entry in assigned_tasks.get(scope, []):
            text = _zip_read_text(zip_path, entry)
            fallback = Path(entry).stem
            explicit_front_matter = _has_explicit_front_matter(text)
            task = _load_task_from_text(text or "", fallback) if text is not None else None
            if task is None:
                tasks.append(
                    {
                        "task_id": fallback,
                        "title": fallback,
                        "entry": entry,
                        "status": "unparsable",
                        "meta": {},
                        "load_error": True,
                    }
                )
                continue
            lint = _inbox_task_lint(task, explicit_front_matter=explicit_front_matter)
            tasks.append(
                {
                    "task_id": task.task_id or fallback,
                    "title": task.title or fallback,
                    "entry": entry,
                    "status": _known_task_status(task.task_id) or "inbox_only",
                    "meta": dict(task.raw_front_matter or {}),
                    "lint": lint,
                    "load_error": False,
                }
            )
        lines.append(
            {
                "kind": "zip",
                "path": str(zip_path),
                "name": _line_name(zip_path.stem, index_root, total_lines),
                "container_name": zip_path.stem,
                "line_key": _line_key("zip", zip_path.name, scope, index_root),
                "valid": True,
                "scope": scope,
                "index_name": index_name,
                "index_entries": scoped_indexes,
                "index_root": index_root,
                "container_line_count": total_lines,
                "tasks": tasks,
            }
        )
    return lines


def _scan_folder_container(folder: Path) -> List[Dict[str, object]]:
    md_files = sorted(folder.rglob("*.md"))
    index_files = [p for p in md_files if _is_batch_index_name(p.name)]
    task_files_list = [p for p in md_files if not _is_batch_index_name(p.name)]
    if not index_files:
        return [{
            "kind": "folder",
            "path": str(folder),
            "name": folder.name,
            "valid": False,
            "reason": "missing TASK_BATCH_INDEX*.md",
            "task_count": len(task_files_list),
        }]

    scope_to_indexes: Dict[Path, List[Path]] = {}
    for index_path in sorted(index_files):
        scope_to_indexes.setdefault(index_path.parent, []).append(index_path)
    total_lines = len(scope_to_indexes)

    assigned_tasks: Dict[Path, List[Path]] = {scope: [] for scope in scope_to_indexes}
    for md_path in task_files_list:
        matching_scopes = [
            scope for scope in scope_to_indexes
            if scope == md_path.parent or scope in md_path.parents
        ]
        if not matching_scopes:
            continue
        best_scope = max(matching_scopes, key=lambda path: len(path.parts))
        assigned_tasks.setdefault(best_scope, []).append(md_path)

    lines: List[Dict[str, object]] = []
    for scope, scoped_indexes in sorted(scope_to_indexes.items(), key=lambda item: str(item[0]).lower()):
        primary_index = scoped_indexes[0]
        index_root = _index_root_from_index_name(primary_index.name)
        tasks: List[Dict[str, object]] = []
        for md_path in assigned_tasks.get(scope, []):
            try:
                raw = md_path.read_text(encoding="utf-8-sig")
            except OSError:
                tasks.append(
                    {
                        "task_id": md_path.stem,
                        "title": md_path.stem,
                        "path": str(md_path),
                        "status": "unreadable",
                        "meta": {},
                        "load_error": True,
                    }
                )
                continue
            explicit_front_matter = _has_explicit_front_matter(raw)
            task = _load_task_from_text(raw, md_path.stem)
            if task is None:
                tasks.append(
                    {
                        "task_id": md_path.stem,
                        "title": md_path.stem,
                        "path": str(md_path),
                        "status": "unparsable",
                        "meta": {},
                        "load_error": True,
                    }
                )
                continue
            lint = _inbox_task_lint(task, explicit_front_matter=explicit_front_matter)
            tasks.append(
                {
                    "task_id": task.task_id or md_path.stem,
                    "title": task.title or md_path.stem,
                    "path": str(md_path),
                    "status": _known_task_status(task.task_id) or "inbox_only",
                    "meta": dict(task.raw_front_matter or {}),
                    "lint": lint,
                    "load_error": False,
                }
            )
        relative_scope = scope.relative_to(folder).as_posix() if scope != folder else ""
        lines.append(
            {
                "kind": "folder",
                "path": str(folder),
                "name": _line_name(folder.name, index_root, total_lines),
                "container_name": folder.name,
                "line_key": _line_key("folder", folder.name, relative_scope, index_root),
                "valid": True,
                "scope": relative_scope,
                "scope_path": str(scope),
                "index_name": primary_index.name,
                "index_path": str(primary_index),
                "index_paths": [str(p) for p in scoped_indexes],
                "index_root": index_root,
                "container_line_count": total_lines,
                "tasks": tasks,
            }
        )
    return lines


def _scan_loose_index(index_path: Path) -> Dict[str, object]:
    """A `TASK_BATCH_INDEX*.md` left at the inbox root counts as a one-line
    container (legacy / leftover from a previous scan that was not archived)."""
    return {
        "kind": "loose_index",
        "path": str(index_path),
        "name": index_path.stem,
        "valid": True,
        "index_name": index_path.name,
        "index_path": str(index_path),
        "index_root": _index_root_from_index_name(index_path.name),
        "tasks": [],
        "note": "Loose TASK_BATCH_INDEX*.md at inbox root; no companion task books",
    }


def _is_reserved_inbox_dir(name: str) -> bool:
    lowered = name.lower()
    return lowered in {"processed", "_discarded", "_consumed", "_proposed"} or lowered.startswith(".")


def scan_inbox_staging() -> Dict[str, object]:
    """Inspect `tasks/inbox/` and report each container as a line.

    Returns a dict with `lines` (valid containers) and `anomalies` (containers
    or loose files that lack TASK_BATCH_INDEX*.md and therefore cannot be
    promoted from the dashboard).
    """
    inbox = root() / "tasks" / "inbox"
    if not inbox.exists():
        return {"lines": [], "anomalies": []}

    lines: List[Dict[str, object]] = []
    anomalies: List[Dict[str, object]] = []

    for entry in sorted(inbox.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            if _is_reserved_inbox_dir(entry.name):
                continue
            scanned_items = _scan_folder_container(entry)
        elif entry.is_file() and entry.suffix.lower() == ".zip":
            scanned_items = _scan_zip_container(entry)
        elif entry.is_file() and entry.suffix.lower() == ".md":
            if _is_batch_index_name(entry.name):
                scanned_items = [_scan_loose_index(entry)]
            elif _is_inbox_readme_name(entry.name):
                continue
            else:
                anomalies.append(
                    {
                        "kind": "loose_md",
                        "path": str(entry),
                        "name": entry.name,
                        "reason": "loose .md outside of any TASK_BATCH_INDEX line",
                    }
                )
                continue
        else:
            continue

        for scanned in scanned_items:
            if scanned.get("valid"):
                lines.append(scanned)
            else:
                anomalies.append(scanned)

    return {"lines": lines, "anomalies": anomalies}


def _inbox_line_for_source(source: Optional[Path] = None) -> List[Dict[str, object]]:
    state = scan_inbox_staging()
    lines = list(state.get("lines", []) or [])
    if source is None:
        return lines
    source_resolved = source.resolve()
    selected: List[Dict[str, object]] = []
    for line in lines:
        line_path = Path(str(line.get("path") or ""))
        try:
            if line_path.resolve() == source_resolved or source_resolved == Path(str(line.get("scope_path") or "")).resolve():
                selected.append(line)
        except OSError:
            continue
    return selected


def _inbox_boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "gpu"}


def _inbox_string_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _inbox_infer_resource_hint(meta: Dict[str, object]) -> str:
    explicit = str(meta.get("resource_hint") or "").strip().lower()
    if explicit:
        return explicit
    intent = str(meta.get("intent") or meta.get("execution_stage") or "").strip().lower()
    if _inbox_boolish(meta.get("needs_gpu")) or "gpu" in intent or "train" in intent:
        return "gpu"
    return "cpu"


def _inbox_infer_resource_lane(meta: Dict[str, object]) -> str:
    explicit = str(meta.get("resource_lane") or "").strip()
    if explicit:
        return explicit
    intent = str(meta.get("intent") or meta.get("execution_stage") or "").strip().lower()
    worker = str(meta.get("worker") or "").strip()
    if _inbox_infer_resource_hint(meta) == "gpu":
        return "gpu_run"
    if meta.get("allow_code_changes") is True or intent in {"source_change", "code_change", "implementation"}:
        return "source_change"
    if worker == "remote_materializer" or "material" in intent:
        return "materializer_api"
    return "cpu_analysis"


def _inbox_infer_dag_root(tasks: List[Dict[str, object]]) -> str:
    explicit = [
        str((task.get("meta") or {}).get("dag_root") or "").strip()
        for task in tasks
        if isinstance(task.get("meta"), dict) and str((task.get("meta") or {}).get("dag_root") or "").strip()
    ]
    if explicit:
        return sorted(explicit)[0]
    no_deps: List[str] = []
    for task in tasks:
        meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
        deps = _inbox_string_list(meta.get("depends_on")) if isinstance(meta, dict) else []
        if not deps:
            no_deps.append(str(task.get("task_id") or ""))
    preferred_tokens = ("_000", "_001", "root", "prepare", "inventory")
    preferred = [task_id for task_id in no_deps if any(token in task_id.lower() for token in preferred_tokens)]
    candidates = [task_id for task_id in (preferred or no_deps or [str(t.get("task_id") or "") for t in tasks]) if task_id]
    return sorted(candidates)[0] if candidates else ""


A_RESIDUAL_DECISION_FIELDS = [
    "detector_validated",
    "best_residual_feature",
    "best_source_rule",
    "best_fusion",
    "fusion_beats_detector",
    "best_iou_gain",
    "best_boundary_f1_gain",
    "real_side_fp_reduction",
    "fake_core_recall_loss",
    "recommended_use",
]

A_RESIDUAL_DECISION_FIELD_TYPES = {
    "detector_validated": "bool",
    "fusion_beats_detector": "bool",
    "best_iou_gain": "number",
    "best_boundary_f1_gain": "number",
    "real_side_fp_reduction": "number",
    "fake_core_recall_loss": "number",
    "recommended_use": "string",
}


def _inbox_infer_decision_contract(meta: Dict[str, object], task_id: str) -> Dict[str, object]:
    if isinstance(meta.get("decision_contract"), dict) or isinstance(meta.get("final_contract"), dict):
        return {}
    outputs = _inbox_string_list(meta.get("outputs") or meta.get("expected_outputs"))
    if "decision_summary.json" not in outputs:
        return {}
    required_files = list(outputs)
    required_fields = _inbox_string_list(meta.get("required_decision_fields") or meta.get("decision_fields"))
    scope_text = " ".join(
        str(item or "")
        for item in (
            task_id,
            meta.get("line"),
            meta.get("batch"),
            meta.get("dag_root"),
            meta.get("title"),
            meta.get("intent"),
            meta.get("execution_stage"),
        )
    ).lower()
    is_a_residual = "a_residual" in scope_text or "a_rescorr" in scope_text
    if not required_fields and is_a_residual:
        required_fields = list(A_RESIDUAL_DECISION_FIELDS)
    contract: Dict[str, object] = {
        "primary_json": "decision_summary.json",
        "required_files": required_files,
    }
    if required_fields:
        contract["required_fields"] = required_fields
    if is_a_residual:
        contract["field_types"] = {
            key: value
            for key, value in A_RESIDUAL_DECISION_FIELD_TYPES.items()
            if not required_fields or key in required_fields
        }
        contract["semantic_checks"] = ["a_residual_decision_consistency"]
    return contract


def _inbox_metabolic_updates(
    task_record: Dict[str, object],
    *,
    line: Dict[str, object],
    dag_root: str,
    default_worker: str,
) -> Dict[str, object]:
    meta = dict(task_record.get("meta") or {})
    updates: Dict[str, object] = {}
    line_name = str(line.get("container_name") or line.get("name") or "").strip()
    batch_name = str(line.get("index_root") or line.get("container_name") or line.get("name") or "").strip()
    if not str(meta.get("priority") or "").strip():
        updates["priority"] = "normal"
    if not str(meta.get("status") or "").strip():
        updates["status"] = "pending"
    if not str(meta.get("worker") or "").strip():
        updates["worker"] = default_worker
        meta["worker"] = default_worker
    if not str(meta.get("line") or "").strip() and line_name:
        updates["line"] = line_name
    if not str(meta.get("batch") or "").strip() and batch_name:
        updates["batch"] = batch_name
    if not str(meta.get("dag_root") or "").strip() and dag_root:
        updates["dag_root"] = dag_root
    if not str(meta.get("resource_hint") or "").strip():
        updates["resource_hint"] = _inbox_infer_resource_hint({**meta, **updates})
    if not str(meta.get("resource_lane") or "").strip():
        updates["resource_lane"] = _inbox_infer_resource_lane({**meta, **updates})
    worker = str(updates.get("worker") or meta.get("worker") or "").strip()
    if worker and not meta.get("allowed_workers"):
        updates["allowed_workers"] = [worker]
    if worker == "materialized_execution" and "allow_codex_fallback" not in meta:
        updates["allow_codex_fallback"] = False
    decision_contract = _inbox_infer_decision_contract({**meta, **updates}, str(task_record.get("task_id") or ""))
    if decision_contract:
        updates["decision_contract"] = decision_contract
    return updates


def _inbox_apply_front_matter_updates(md_path: Path, updates: Dict[str, object]) -> None:
    original = md_path.read_text(encoding="utf-8-sig")
    text = original.lstrip("\ufeff")
    if _has_explicit_front_matter(text):
        parts = text.split("---", 2)
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            meta = {}
        body = parts[2].lstrip()
    else:
        meta = {
            "task_id": md_path.stem,
            "title": md_path.stem.replace("_", " ").replace("-", " ").strip() or md_path.stem,
        }
        body = text
    meta.update(updates)
    rendered = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + body
    md_path.write_text(rendered, encoding="utf-8")


def inbox_lint(source: Optional[Path] = None) -> Dict[str, object]:
    lines = _inbox_line_for_source(source)
    issues: List[Dict[str, object]] = []
    task_count = 0
    for line in lines:
        tasks = list(line.get("tasks", []) or [])
        task_count += len(tasks)
        task_ids = {str(task.get("task_id") or "") for task in tasks}
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            lint = task.get("lint") if isinstance(task.get("lint"), dict) else {}
            for error in lint.get("errors", []) or []:
                issues.append({"severity": "error", "task_id": task_id, "message": str(error)})
            for warning in lint.get("warnings", []) or []:
                issues.append({"severity": "warning", "task_id": task_id, "message": str(warning)})
            meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
            for field in ("task_id", "title"):
                if not str(meta.get(field) or task.get(field) or "").strip():
                    issues.append({"severity": "error", "task_id": task_id, "message": f"{field} is required"})
            for dep in _inbox_string_list(meta.get("depends_on")):
                if dep not in task_ids and not _known_task_status(dep):
                    issues.append({"severity": "warning", "task_id": task_id, "message": f"depends_on target is not in this inbox line or registry: {dep}"})
            if str(meta.get("worker") or "").strip() == "materialized_execution" and not str(meta.get("intent") or meta.get("execution_stage") or "").strip():
                issues.append({"severity": "warning", "task_id": task_id, "message": "intent is missing; metabolizer can still infer cpu_analysis but claims may be weaker"})
    errors = [item for item in issues if item.get("severity") == "error"]
    return {
        "ok": not errors,
        "source": str(source) if source else None,
        "line_count": len(lines),
        "task_count": task_count,
        "issues": issues,
    }


def inbox_metabolize(
    source: Optional[Path] = None,
    *,
    write: bool = False,
    default_worker: str = "materialized_execution",
) -> Dict[str, object]:
    lines = _inbox_line_for_source(source)
    lint_result = inbox_lint(source)
    planned: List[Dict[str, object]] = []
    written: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    for line in lines:
        tasks = list(line.get("tasks", []) or [])
        dag_root = _inbox_infer_dag_root(tasks)
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if task.get("load_error"):
                skipped.append({"task_id": task_id, "reason": "load_error"})
                continue
            updates = _inbox_metabolic_updates(task, line=line, dag_root=dag_root, default_worker=default_worker)
            entry = {
                "task_id": task_id,
                "line": line.get("name"),
                "kind": line.get("kind"),
                "updates": updates,
                "write_supported": bool(line.get("kind") == "folder" and task.get("path")),
            }
            planned.append(entry)
            if not write:
                continue
            if not entry["write_supported"]:
                skipped.append({"task_id": task_id, "reason": "write_not_supported_for_container", "kind": line.get("kind")})
                continue
            if not updates:
                skipped.append({"task_id": task_id, "reason": "no_updates_needed"})
                continue
            md_path = Path(str(task.get("path")))
            _inbox_apply_front_matter_updates(md_path, updates)
            written.append({"task_id": task_id, "path": str(md_path), "updates": updates})
    return {
        "ok": lint_result.get("ok"),
        "write": write,
        "lint": lint_result,
        "planned": planned,
        "written": written,
        "skipped": skipped,
    }


def _ensure_unique_dir(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate
    return parent / f"{name}.{_timestamp_id()}"


def _processed_index_root_dir(index_root: str) -> Path:
    return root() / "tasks" / "inbox" / "processed" / index_root


def _archive_index_into_processed(
    *, index_root: str, source_text: str, index_filename: str
) -> Path:
    target_dir = _processed_index_root_dir(index_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / index_filename
    if target.exists():
        target = target_dir / f"{Path(index_filename).stem}.{_timestamp_id()}{Path(index_filename).suffix}"
    target.write_text(source_text, encoding="utf-8")
    return target


def _archive_task_md_into_processed(
    *, index_root: str, task_id: str, md_filename: str, md_text: str
) -> Path:
    target_dir = _processed_index_root_dir(index_root) / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / md_filename
    if target.exists():
        target = target_dir / f"{Path(md_filename).stem}.{_timestamp_id()}{Path(md_filename).suffix}"
    target.write_text(md_text, encoding="utf-8")
    return target


def _write_pending_task(task_markdown: str, task_id: str) -> Path:
    target = task_file("pending", task_id)
    if target.exists():
        raise FileExistsError(f"pending task already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(task_markdown, encoding="utf-8")
    return target


def _read_zip_entry_text(zip_path: Path, entry: str) -> str:
    text = _zip_read_text(zip_path, entry)
    if text is None:
        raise FileNotFoundError(f"could not read entry {entry!r} from {zip_path}")
    return text


def _delete_zip_entries(zip_path: Path, entries_to_remove: List[str]) -> None:
    """Rewrite the zip without the given entries."""
    if not entries_to_remove:
        return
    remove_set = set(entries_to_remove)
    tmp_path = zip_path.with_suffix(zip_path.suffix + f".tmp.{_timestamp_id()}")
    with zipfile.ZipFile(zip_path, "r") as src, zipfile.ZipFile(
        tmp_path, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            if item.filename in remove_set:
                continue
            dst.writestr(item, src.read(item.filename))
    tmp_path.replace(zip_path)


def _zip_is_empty_of_md(zip_path: Path) -> bool:
    return not _zip_md_entries(zip_path)


def _move_consumed_zip(zip_path: Path) -> Path:
    consumed = root() / "tasks" / "inbox" / "_consumed"
    consumed.mkdir(parents=True, exist_ok=True)
    target = consumed / f"{_timestamp_id()}__{zip_path.name}"
    zip_path.rename(target)
    return target


def _container_for_task(line: Dict[str, object], task_id: str) -> Optional[Dict[str, object]]:
    for task in line.get("tasks", []) or []:
        if str(task.get("task_id")) == task_id:
            return task
    return None


def _line_lookup_by_name(lines: List[Dict[str, object]], name: str) -> Optional[Dict[str, object]]:
    for line in lines:
        if str(line.get("line_key") or "") == name or str(line.get("name")) == name:
            return line
    return None


def promote_from_staging(
    *,
    line_name: str,
    task_ids: Optional[List[str]] = None,
    worker: str = "codex",
) -> Dict[str, object]:
    """Promote one or more tasks from a staging line into pending.

    If `task_ids` is None or empty, promote every task in the line.
    """
    ensure_orchestrator_dirs()
    state = scan_inbox_staging()
    line = _line_lookup_by_name(state["lines"], line_name)
    if line is None:
        raise FileNotFoundError(f"no valid staging line named {line_name!r}")
    if not line.get("valid"):
        raise ValueError(f"line {line_name!r} is anomalous; cannot promote")

    target_task_ids: List[str]
    if task_ids:
        target_task_ids = list(task_ids)
    else:
        target_task_ids = [str(t.get("task_id")) for t in line.get("tasks", []) or []]

    promoted: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    index_root = str(line.get("index_root") or "TASK_BATCH_INDEX")
    kind = str(line.get("kind"))

    # Archive the batch index alongside the first promotion (idempotent).
    index_archived = False
    archived_index_path: Optional[Path] = None

    for task_id in target_task_ids:
        task_record = _container_for_task(line, task_id)
        if task_record is None:
            skipped.append({"task_id": task_id, "reason": "not_in_line"})
            continue
        if task_record.get("load_error"):
            skipped.append({"task_id": task_id, "reason": "unparsable_source"})
            continue
        lint = task_record.get("lint") if isinstance(task_record.get("lint"), dict) else {}
        lint_errors = lint.get("errors") if isinstance(lint.get("errors"), list) else []
        if lint_errors:
            skipped.append({"task_id": task_id, "reason": "unsafe_inbox_task", "errors": lint_errors})
            continue
        known_status = _known_task_status(task_id)
        if known_status:
            skipped.append({"task_id": task_id, "reason": "already_known", "existing_status": known_status})
            continue

        # Read the source markdown.
        if kind == "zip":
            zip_path = Path(str(line["path"]))
            entry = str(task_record.get("entry"))
            md_text = _read_zip_entry_text(zip_path, entry)
            md_filename = Path(entry).name
        elif kind == "folder":
            md_path = Path(str(task_record.get("path")))
            md_text = md_path.read_text(encoding="utf-8-sig").lstrip("\ufeff")
            md_filename = md_path.name
        else:
            skipped.append({"task_id": task_id, "reason": f"unsupported_kind:{kind}"})
            continue

        # Ensure front matter, write to pending.
        task_markdown = md_text
        if not _has_explicit_front_matter(task_markdown):
            tmp = root() / f".promote_tmp_{uuid.uuid4().hex}.md"
            tmp.write_text(md_text, encoding="utf-8")
            try:
                task_markdown = ensure_task_front_matter(tmp, worker=worker)
            finally:
                if tmp.exists():
                    tmp.unlink()
        try:
            pending_path = _write_pending_task(task_markdown, task_id)
        except FileExistsError as exc:
            skipped.append({"task_id": task_id, "reason": "pending_exists", "detail": str(exc)})
            continue
        loaded = load_task(pending_path)
        upsert_task(loaded, status="pending")

        # Archive the batch index once per call.
        if not index_archived:
            if kind == "zip":
                index_entry_name = str(line.get("index_name") or "TASK_BATCH_INDEX.md")
                index_entries = line.get("index_entries") or []
                first_entry = index_entries[0] if index_entries else index_entry_name
                index_text = _read_zip_entry_text(Path(str(line["path"])), str(first_entry))
            else:
                index_path = Path(str(line.get("index_path")))
                index_text = index_path.read_text(encoding="utf-8-sig").lstrip("\ufeff")
            archived_index_path = _archive_index_into_processed(
                index_root=index_root,
                source_text=index_text,
                index_filename=str(line.get("index_name") or "TASK_BATCH_INDEX.md"),
            )
            index_archived = True

        # Archive the original task md.
        archived_md = _archive_task_md_into_processed(
            index_root=index_root,
            task_id=task_id,
            md_filename=md_filename,
            md_text=md_text,
        )

        # Remove the source from staging.
        if kind == "zip":
            _delete_zip_entries(Path(str(line["path"])), [str(task_record.get("entry"))])
        elif kind == "folder":
            try:
                Path(str(task_record.get("path"))).unlink()
            except OSError:
                pass

        promoted.append(
            {
                "task_id": task_id,
                "pending_path": str(pending_path),
                "archived_md": str(archived_md),
            }
        )

    # Folder line: if the folder is now empty (no md left except possibly the index),
    # archive the index file as well by removing it from staging.
    if kind == "folder":
        folder = Path(str(line["path"]))
        scope_folder = Path(str(line.get("scope_path") or folder))
        remaining_tasks = [
            p for p in scope_folder.rglob("*.md")
            if not _is_batch_index_name(p.name)
        ]
        if not remaining_tasks and index_archived:
            # Remove the index file(s) from staging now that we have a copy in processed.
            for index_path_str in line.get("index_paths") or [line.get("index_path")]:
                if not index_path_str:
                    continue
                try:
                    Path(str(index_path_str)).unlink()
                except OSError:
                    pass
            # If the folder is now completely empty (recursively), remove it.
            try:
                if not any(folder.rglob("*")):
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    # Zip line: when the zip has no remaining md files, move the whole zip to _consumed/.
    if kind == "zip":
        zip_path = Path(str(line["path"]))
        scope = str(line.get("scope") or "")
        remaining_line_tasks = [
            entry for entry in _zip_md_entries(zip_path)
            if not _is_batch_index_name(Path(entry).name) and _zip_scope_matches(entry, scope)
        ] if zip_path.exists() else []
        if not remaining_line_tasks and index_archived:
            _delete_zip_entries(zip_path, [str(entry) for entry in line.get("index_entries") or []])
        if zip_path.exists() and _zip_is_empty_of_md(zip_path):
            _move_consumed_zip(zip_path)

    # Loose index that has been "promoted with no tasks" simply moves the index to processed.
    if kind == "loose_index" and not promoted and target_task_ids == []:
        # Nothing to promote (no tasks attached). Treat as no-op.
        pass

    return {
        "line": line_name,
        "index_root": index_root,
        "promoted": promoted,
        "skipped": skipped,
        "archived_index": str(archived_index_path) if archived_index_path else "",
    }


def discard_from_staging(
    *,
    line_name: str,
    task_ids: Optional[List[str]] = None,
) -> Dict[str, object]:
    """Discard one or more tasks from a staging line into `tasks/inbox/_discarded/`.

    If `task_ids` is None or empty, the entire container is discarded.
    Anomalies can also be discarded by their `name` (use line_name = anomaly name).
    """
    ensure_orchestrator_dirs()
    state = scan_inbox_staging()
    line = _line_lookup_by_name(state["lines"], line_name)
    anomaly: Optional[Dict[str, object]] = None
    if line is None:
        for item in state["anomalies"]:
            if str(item.get("name")) == line_name:
                anomaly = item
                break
        if anomaly is None:
            raise FileNotFoundError(f"no staging line or anomaly named {line_name!r}")

    discarded_root = root() / "tasks" / "inbox" / "_discarded"
    discarded_root.mkdir(parents=True, exist_ok=True)

    if anomaly is not None:
        src = Path(str(anomaly["path"]))
        target = discarded_root / f"{_timestamp_id()}__{src.name}"
        if src.is_dir():
            shutil.move(str(src), str(target))
        else:
            src.rename(target)
        return {
            "line": line_name,
            "discarded_full_container": True,
            "moved_to": str(target),
            "kind": str(anomaly.get("kind")),
        }

    # Valid line: either discard a subset or the whole container.
    target_task_ids: List[str]
    if task_ids:
        target_task_ids = list(task_ids)
    else:
        target_task_ids = []  # signal full-container discard below

    kind = str(line.get("kind"))
    discarded_items: List[Dict[str, object]] = []

    if not target_task_ids:
        # Whole-line discard. If the physical container only has one line, move
        # the whole container; otherwise remove only this index scope.
        src = Path(str(line["path"]))
        target = discarded_root / f"{_timestamp_id()}__{src.name}"
        if int(line.get("container_line_count") or 1) <= 1 and src.is_dir():
            shutil.move(str(src), str(target))
        elif int(line.get("container_line_count") or 1) <= 1 and src.is_file():
            src.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            if kind == "zip":
                zip_path = src
                entries = [str(item) for item in line.get("index_entries") or []]
                entries.extend(str(task.get("entry")) for task in line.get("tasks", []) or [] if task.get("entry"))
                with zipfile.ZipFile(zip_path) as zf:
                    for entry in entries:
                        try:
                            out_path = target / Path(entry).name
                            out_path.write_bytes(zf.read(entry))
                        except KeyError:
                            continue
                _delete_zip_entries(zip_path, entries)
                if zip_path.exists() and _zip_is_empty_of_md(zip_path):
                    _move_consumed_zip(zip_path)
            elif kind == "folder":
                paths_to_move = [Path(str(item)) for item in line.get("index_paths") or []]
                paths_to_move.extend(Path(str(task.get("path"))) for task in line.get("tasks", []) or [] if task.get("path"))
                for source in paths_to_move:
                    if not source.exists():
                        continue
                    destination = target / source.name
                    if destination.exists():
                        destination = target / f"{source.stem}.{_timestamp_id()}{source.suffix}"
                    shutil.move(str(source), str(destination))
                scope_folder = Path(str(line.get("scope_path") or src))
                try:
                    if scope_folder.exists() and not any(scope_folder.rglob("*")):
                        shutil.rmtree(scope_folder, ignore_errors=True)
                except OSError:
                    pass
        return {
            "line": line_name,
            "discarded_full_container": True,
            "moved_to": str(target),
            "kind": kind,
        }

    # Partial discard.
    for task_id in target_task_ids:
        task_record = _container_for_task(line, task_id)
        if task_record is None:
            discarded_items.append({"task_id": task_id, "status": "not_in_line"})
            continue
        if kind == "zip":
            zip_path = Path(str(line["path"]))
            entry = str(task_record.get("entry"))
            md_text = _zip_read_text(zip_path, entry) or ""
            target_dir = discarded_root / f"{_timestamp_id()}__{line_name}__{task_id}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_md = target_dir / Path(entry).name
            target_md.write_text(md_text, encoding="utf-8")
            _delete_zip_entries(zip_path, [entry])
            discarded_items.append(
                {"task_id": task_id, "status": "moved", "moved_to": str(target_md)}
            )
        elif kind == "folder":
            md_path = Path(str(task_record.get("path")))
            target_dir = discarded_root / f"{_timestamp_id()}__{line_name}__{task_id}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_md = target_dir / md_path.name
            shutil.move(str(md_path), str(target_md))
            discarded_items.append(
                {"task_id": task_id, "status": "moved", "moved_to": str(target_md)}
            )
        else:
            discarded_items.append({"task_id": task_id, "status": f"unsupported_kind:{kind}"})

    return {
        "line": line_name,
        "discarded_full_container": False,
        "discarded": discarded_items,
        "kind": kind,
    }


def find_task(task_id: str) -> tuple[str, Path]:
    for status in ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed"):
        candidate = task_file(status, task_id)
        if candidate.exists():
            return status, candidate
    raise FileNotFoundError(f"task not found: {task_id}")


def status(task_id: Optional[str] = None) -> Dict[str, object]:
    registry = load_registry()
    tasks = registry.get("tasks", {})
    if task_id:
        return tasks.get(task_id, {})
    return tasks


def _existing_result_path_for_status(
    record: Dict[str, object],
    allowed_statuses: set[str],
    *,
    include_historical_runs: bool = True,
) -> Optional[Path]:
    candidates: List[object] = []
    if str(record.get("status")) in allowed_statuses:
        candidates.extend([record.get("result_path"), record.get("latest_result_path")])
    has_allowed_run = False
    if include_historical_runs:
        for run in reversed(record.get("runs", []) or []):
            if isinstance(run, dict) and str(run.get("status")) in allowed_statuses:
                has_allowed_run = True
                candidates.append(run.get("result_path"))
        if has_allowed_run:
            candidates.extend([record.get("latest_result_path"), record.get("archived_result_path")])
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if path.exists():
            return path
    return None


def _dependency_effective_status(record: Dict[str, object], allow_partial: bool) -> Optional[str]:
    allowed = {"completed"}
    if allow_partial:
        allowed.add("partial")
    status_value = str(record.get("status"))
    if status_value in allowed:
        return status_value
    return None


def _task_allows_soft_dependency_continue(task: TaskBook) -> bool:
    raw = task.raw_front_matter or {}
    if raw.get("allow_soft_dependency_continue") is True:
        return True
    if raw.get("allow_soft_dependency_continue") is False:
        return False
    stage = str(raw.get("execution_stage") or "").strip().lower()
    if stage in {"analysis", "report", "diagnosis", "audit", "review", "decision", "judge"}:
        return True
    text = " ".join(
        str(value or "").lower()
        for value in (task.task_id, task.title, raw.get("objective"))
    )
    return any(token in text for token in ("analysis", "analyze", "report", "summary", "diagnosis", "audit", "review", "decision"))


def _soft_dependency_status(record: Dict[str, object]) -> str:
    status_value = str(record.get("status") or record.get("task_status") or "").strip().lower()
    if status_value in {"partial", "failed", "launch_failed", "launch_unknown"}:
        return status_value
    latest = _latest_run_record(record)
    if isinstance(latest, dict):
        latest_status = str(latest.get("status") or "").strip().lower()
        if latest_status in {"partial", "failed", "launch_failed", "launch_unknown"}:
            return latest_status
    return status_value


def _soft_dependency_entry(task_id: str, status_value: str, reason: str, *, detail: str = "") -> Dict[str, object]:
    entry: Dict[str, object] = {
        "task_id": task_id,
        "status": status_value,
        "reason": reason,
        "soft_block": True,
        "routing": "continue_for_analysis_or_report",
    }
    if detail:
        entry["detail"] = detail
    return entry


def _normalize_acceptable_statuses(values: object) -> set[str]:
    normalized: set[str] = set()
    for value in values or []:
        text = str(value).strip().lower()
        if text == "success":
            normalized.add("completed")
        elif text == "failed_with_artifacts":
            normalized.add("failed")
        elif text in {"completed", "partial", "failed"}:
            normalized.add(text)
    return normalized or {"completed"}


def _artifact_candidates(base: Path, artifact: str) -> List[Path]:
    artifact_path = artifact.replace("\\", "/").lstrip("/")
    candidates = [
        base / artifact_path,
        base / artifact_path.replace("_worker_out/", "", 1),
    ]
    name = Path(artifact_path).name
    if name:
        candidates.append(base / name)
        candidates.append(base / "artifacts" / name)
    return candidates


def _find_artifact_path(base: Path, artifact: str) -> Optional[Path]:
    for path in _artifact_candidates(base, artifact):
        if path.exists():
            return path
    return None


def _artifact_exists(base: Path, artifact: str) -> bool:
    return _find_artifact_path(base, artifact) is not None


def _default_input_artifacts() -> List[str]:
    return ["_worker_out/result.yaml", "_worker_out/worker_notes.md"]


def _artifact_contract(base: Path, required_artifacts: Optional[List[str]] = None) -> Dict[str, object]:
    required = list(required_artifacts or _default_input_artifacts())
    available: Dict[str, str] = {}
    missing: List[str] = []
    invalid: List[str] = []
    if not base.exists():
        return {
            "ok": False,
            "available_artifacts": {},
            "missing_artifacts": required or ["result root"],
            "invalid_artifacts": [],
        }
    for artifact in required:
        found = _find_artifact_path(base, artifact)
        if found:
            available[artifact] = str(found)
        else:
            missing.append(artifact)
    result_yaml = _find_artifact_path(base, "_worker_out/result.yaml") or _find_artifact_path(base, "result.yaml")
    if result_yaml:
        try:
            yaml.safe_load(result_yaml.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            invalid.append(f"result.yaml: {exc}")
    elif "_worker_out/result.yaml" in required or "result.yaml" in required:
        missing.append("result.yaml")
    metrics = _find_artifact_path(base, "_worker_out/metrics.json") or _find_artifact_path(base, "metrics.json")
    if metrics:
        try:
            json.loads(metrics.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append(f"metrics.json: {exc}")
    return {
        "ok": not missing and not invalid,
        "available_artifacts": available,
        "missing_artifacts": sorted(set(missing)),
        "invalid_artifacts": invalid,
    }


def _canonical_run_status(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "success":
        return "completed"
    if text in {"completed", "partial", "failed", "running", "launching", "launch_pending", "launch_unknown", "launch_failed"}:
        return text
    return text


def _find_run_record(record: Dict[str, object], run_id: object) -> Optional[Dict[str, object]]:
    needle = str(run_id or "")
    if not needle:
        return None
    for run in record.get("runs", []) or []:
        if isinstance(run, dict) and str(run.get("run_id") or "") == needle:
            return run
    return None


def _run_result_path(run: Dict[str, object]) -> Optional[Path]:
    for key in ("result_path", "latest_result_path", "archived_result_path"):
        value = run.get(key)
        if value:
            path = Path(str(value))
            if path.exists():
                return path
    return None


def _run_is_input_usable(run: Dict[str, object]) -> bool:
    status_value = _canonical_run_status(run.get("status"))
    if status_value not in {"completed", "partial"}:
        return False
    if "usable_as_input" in run:
        return bool(run.get("usable_as_input"))
    if "artifact_contract_valid" in run:
        return bool(run.get("artifact_contract_valid"))
    result_path = _run_result_path(run)
    return bool(result_path and _artifact_contract(result_path).get("ok"))


def _refresh_task_run_index(task_id: str) -> Dict[str, object]:
    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        runs = [run for run in (record.get("runs", []) or []) if isinstance(run, dict)]
        latest_success_run_id = None
        latest_partial_run_id = None
        valid_run_ids: List[str] = []
        for run in runs:
            run_id = str(run.get("run_id") or "")
            if not run_id or not _run_is_input_usable(run):
                continue
            status_value = _canonical_run_status(run.get("status"))
            if status_value == "completed":
                latest_success_run_id = run_id
                valid_run_ids.append(run_id)
            elif status_value == "partial":
                latest_partial_run_id = run_id
                valid_run_ids.append(run_id)
        latest_valid_run_id = latest_success_run_id or latest_partial_run_id
        index = {
            "task_id": task_id,
            "latest_run_id": record.get("latest_run_id"),
            "latest_success_run_id": latest_success_run_id,
            "latest_partial_run_id": latest_partial_run_id,
            "latest_valid_run_id": latest_valid_run_id,
            "valid_run_ids": valid_run_ids,
        }
        record.update(index)
        record["task_run_index"] = index
        return record

    return mutate_registry(_mutate)


def _terminal_status_from_result_status(status: object) -> str:
    text = str(status or "").strip().lower()
    if text in {"success", "completed", "complete"}:
        return "completed"
    if text == "partial":
        return "partial"
    return "failed"


def _terminal_task_status(record: Dict[str, object]) -> Optional[str]:
    status = str(record.get("status") or "").strip().lower()
    if status in {"completed", "partial", "failed"}:
        return status
    task_status = str(record.get("task_status") or "").strip().lower()
    if task_status in {"completed", "partial", "failed"}:
        return task_status
    return None


def _latest_run_record(record: Dict[str, object]) -> Optional[Dict[str, object]]:
    runs = record.get("runs") if isinstance(record.get("runs"), list) else []
    latest_run_id = record.get("latest_run_id")
    if latest_run_id:
        for run in reversed(runs):
            if isinstance(run, dict) and run.get("run_id") == latest_run_id:
                return run
    for run in reversed(runs):
        if isinstance(run, dict):
            return run
    return None


def _registry_hygiene_issue(
    task_id: str,
    code: str,
    message: str,
    *,
    field: str = "",
    current: object = None,
    expected: object = None,
    fixable: bool = False,
) -> Dict[str, object]:
    return {
        "task_id": task_id,
        "code": code,
        "message": message,
        "field": field,
        "current": current,
        "expected": expected,
        "fixable": fixable,
    }


def _registry_hygiene_issues_for_record(task_id: str, record: Dict[str, object]) -> List[Dict[str, object]]:
    issues: List[Dict[str, object]] = []
    final_status = _terminal_task_status(record)
    latest_run = _latest_run_record(record)
    active_values = {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}
    status_text = str(record.get("status") or "").strip().lower()
    if not final_status and status_text in active_values:
        has_task_file = False
        try:
            find_task(task_id)
            has_task_file = True
        except FileNotFoundError:
            has_task_file = False
        local_evidence = any(
            path.exists()
            for path in (worktree_dir(task_id), worker_out_dir(task_id), result_dir(task_id), dispatch_dir(task_id))
        )
        remote_evidence = any(
            record.get(key)
            for key in ("remote", "remote_run", "remote_state", "latest_run_id", "result_path", "latest_result_path", "archived_result_path")
        )
        has_runs = bool(record.get("runs") if isinstance(record.get("runs"), list) else [])
        if not has_task_file and not local_evidence and not remote_evidence and not has_runs:
            issues.append(
                _registry_hygiene_issue(
                    task_id,
                    "registry_only_active_task",
                    "Active registry record has no task folder or run evidence and cannot be reconciled.",
                    field="status",
                    current=record.get("status"),
                    expected="failed",
                    fixable=True,
                )
            )
    if final_status:
        if str(record.get("task_status") or "").strip().lower() in active_values:
            issues.append(
                _registry_hygiene_issue(
                    task_id,
                    "terminal_task_task_status_active",
                    "Terminal task should not retain an active top-level task_status.",
                    field="task_status",
                    current=record.get("task_status"),
                    expected=final_status,
                    fixable=True,
                )
            )
        if str(record.get("run_phase") or "").strip().lower() in active_values:
            issues.append(
                _registry_hygiene_issue(
                    task_id,
                    "terminal_task_run_phase_active",
                    "Terminal task should not retain an active top-level run_phase.",
                    field="run_phase",
                    current=record.get("run_phase"),
                    expected="collected",
                    fixable=True,
                )
            )
        if str(record.get("remote_state") or "").strip().lower() in active_values:
            issues.append(
                _registry_hygiene_issue(
                    task_id,
                    "terminal_task_remote_state_active",
                    "Terminal task should not retain an active top-level remote_state.",
                    field="remote_state",
                    current=record.get("remote_state"),
                    expected="collected",
                    fixable=True,
                )
            )
        if latest_run:
            for field, expected in {
                "status": final_status,
                "task_status": final_status,
                "run_phase": "collected",
                "state": final_status,
                "remote_state": "collected",
            }.items():
                current = latest_run.get(field)
                if current is None:
                    continue
                current_text = str(current).strip().lower()
                if current == expected or current_text not in active_values | {"launch_failed"}:
                    continue
                if field == "status" and current_text == "launch_failed" and latest_run.get("result_path"):
                    continue
                if field == "remote_state" and current_text == "unknown":
                    continue
                if True:
                    issues.append(
                        _registry_hygiene_issue(
                            task_id,
                            f"terminal_latest_run_{field}_stale",
                            f"Terminal task latest run should have {field}={expected}.",
                            field=f"runs[{latest_run.get('run_id')}].{field}",
                            current=current,
                            expected=expected,
                            fixable=True,
                        )
                    )
            if latest_run.get("collected_at") is None and any(
                str(latest_run.get(field) or "").strip().lower() in active_values
                for field in ("status", "task_status", "run_phase", "state", "remote_state")
            ):
                issues.append(
                    _registry_hygiene_issue(
                        task_id,
                        "terminal_latest_run_missing_collected_at",
                        "Terminal task latest run should record collected_at.",
                        field=f"runs[{latest_run.get('run_id')}].collected_at",
                        current=None,
                        expected="timestamp",
                        fixable=True,
                    )
                )
    if record.get("status") == "pending" and str(record.get("task_status") or "").lower() in {"completed", "partial", "failed"}:
        issues.append(
            _registry_hygiene_issue(
                task_id,
                "pending_task_has_terminal_task_status",
                "Pending task has stale terminal task_status.",
                field="task_status",
                current=record.get("task_status"),
                expected=None,
                fixable=False,
            )
        )
    runs = record.get("runs") if isinstance(record.get("runs"), list) else []
    run_ids = {run.get("run_id") for run in runs if isinstance(run, dict)}
    for key in ("latest_valid_run_id", "latest_success_run_id", "latest_partial_run_id"):
        value = record.get(key)
        if value and value not in run_ids:
            issues.append(
                _registry_hygiene_issue(
                    task_id,
                    f"{key}_missing_run",
                    f"{key} points to a missing run.",
                    field=key,
                    current=value,
                    expected="existing run_id",
                    fixable=False,
                )
            )
    if record.get("queue_lane") == "ready_front" and final_status:
        issues.append(
            _registry_hygiene_issue(
                task_id,
                "terminal_task_in_ready_front",
                "Terminal task should not remain in ready_front.",
                field="queue_lane",
                current="ready_front",
                expected=None,
                fixable=True,
            )
        )
    return issues


def registry_hygiene(apply_safe: bool = False, path: Optional[Path] = None) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    before = load_registry(path)
    tasks = before.get("tasks") if isinstance(before.get("tasks"), dict) else {}
    issues: List[Dict[str, object]] = []
    for task_id, record in sorted(tasks.items()):
        if isinstance(record, dict):
            issues.extend(_registry_hygiene_issues_for_record(task_id, record))
    applied: List[Dict[str, object]] = []
    if apply_safe:
        now = now_iso()

        def _mutate(registry: Dict[str, object]) -> None:
            reg_tasks = registry.setdefault("tasks", {})
            for task_id, record in reg_tasks.items():
                if not isinstance(record, dict):
                    continue
                final_status = _terminal_task_status(record)
                status_text = str(record.get("status") or "").strip().lower()
                if not final_status and status_text in {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}:
                    has_task_file = False
                    try:
                        find_task(task_id)
                        has_task_file = True
                    except FileNotFoundError:
                        has_task_file = False
                    local_evidence = any(
                        path.exists()
                        for path in (worktree_dir(task_id), worker_out_dir(task_id), result_dir(task_id), dispatch_dir(task_id))
                    )
                    remote_evidence = any(
                        record.get(key)
                        for key in ("remote", "remote_run", "remote_state", "latest_run_id", "result_path", "latest_result_path", "archived_result_path")
                    )
                    has_runs = bool(record.get("runs") if isinstance(record.get("runs"), list) else [])
                    if not has_task_file and not local_evidence and not remote_evidence and not has_runs:
                        record["status"] = "failed"
                        record["task_status"] = "failed"
                        record["finished_at"] = record.get("finished_at") or now
                        record["summary_short"] = record.get("summary_short") or "registry hygiene: stale registry-only active record"
                        record["last_hygiene_at"] = now
                        record["last_hygiene_fixes"] = ["registry_only_active_task"]
                        record["status_transition_source"] = "registry_hygiene"
                        record["status_transition_reason"] = "registry_only_active_task"
                        record["status_transition_actor"] = "scheduler"
                        applied.append({"task_id": task_id, "fixes": ["registry_only_active_task"]})
                    continue
                if not final_status:
                    continue
                fixes: List[str] = []
                if str(record.get("task_status") or "").strip().lower() in {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}:
                    record["task_status"] = final_status
                    fixes.append("task_status")
                if str(record.get("run_phase") or "").strip().lower() in {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}:
                    record["run_phase"] = "collected"
                    fixes.append("run_phase")
                if str(record.get("remote_state") or "").strip().lower() in {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote"}:
                    record["remote_state"] = "collected"
                    fixes.append("remote_state")
                if record.get("queue_lane") == "ready_front":
                    record.pop("queue_lane", None)
                    fixes.append("queue_lane")
                latest_run = _latest_run_record(record)
                if latest_run:
                    run_had_stale_active_state = False
                    for field, expected in {
                        "status": final_status,
                        "task_status": final_status,
                        "run_phase": "collected",
                        "state": final_status,
                        "remote_state": "collected",
                    }.items():
                        current = latest_run.get(field)
                        current_text = str(current or "").strip().lower()
                        if current is not None and current != expected and current_text in {"running", "launching", "launch_pending", "launch_unknown", "execute_run", "finished_remote", "launch_failed"}:
                            latest_run[field] = expected
                            fixes.append(f"latest_run.{field}")
                            run_had_stale_active_state = True
                    if run_had_stale_active_state and not latest_run.get("finished_at"):
                        latest_run["finished_at"] = record.get("finished_at") or now
                        fixes.append("latest_run.finished_at")
                    if run_had_stale_active_state and not latest_run.get("collected_at"):
                        latest_run["collected_at"] = record.get("finished_at") or now
                        fixes.append("latest_run.collected_at")
                if fixes:
                    record["last_hygiene_at"] = now
                    record["last_hygiene_fixes"] = sorted(set(fixes))
                    record["status_transition_source"] = "registry_hygiene"
                    record["status_transition_reason"] = "safe stale collected-state cleanup"
                    record["status_transition_actor"] = "scheduler"
                    applied.append({"task_id": task_id, "fixes": sorted(set(fixes))})

        mutate_registry(_mutate, path=path)
    after_issues: List[Dict[str, object]] = []
    if apply_safe:
        after = load_registry(path)
        after_tasks = after.get("tasks") if isinstance(after.get("tasks"), dict) else {}
        for task_id, record in sorted(after_tasks.items()):
            if isinstance(record, dict):
                after_issues.extend(_registry_hygiene_issues_for_record(task_id, record))
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "apply_safe": apply_safe,
        "issue_count": len(issues),
        "fixable_count": sum(1 for issue in issues if issue.get("fixable")),
        "issues": issues,
        "applied": applied,
        "remaining_issues": after_issues if apply_safe else None,
    }


def partial_pending_event_hash(transition: Dict[str, object]) -> str:
    canonical = {
        "task_id": transition.get("task_id"),
        "from_status": transition.get("before_status"),
        "to_status": transition.get("after_status"),
        "from_task_status": transition.get("before_task_status"),
        "to_task_status": transition.get("after_task_status"),
        "event_time": transition.get("ts"),
        "source": transition.get("source") or "unknown",
        "registry_event_line": transition.get("registry_event_line"),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def partial_pending_ack_path() -> Path:
    return state_dir() / "audits" / "acknowledged_partial_pending_audit.json"


def load_partial_pending_acknowledgment(path: Optional[Path] = None) -> Dict[str, object]:
    ack_path = path or partial_pending_ack_path()
    if not ack_path.exists():
        return {
            "schema_version": 1,
            "audit_type": "partial_to_pending_unknown_transition",
            "acknowledged_events": [],
            "policy": {
                "acknowledged_legacy_events": "warn_only",
                "new_unknown_partial_to_pending": "fail_closed",
            },
            "path": str(ack_path),
        }
    try:
        payload = json.loads(ack_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "schema_version": 1,
            "audit_type": "partial_to_pending_unknown_transition",
            "acknowledged_events": [],
            "error": str(exc),
            "path": str(ack_path),
        }
    if isinstance(payload, dict):
        payload.setdefault("acknowledged_events", [])
        payload.setdefault("path", str(ack_path))
        return payload
    return {"schema_version": 1, "acknowledged_events": [], "error": "acknowledgment file is not a JSON object", "path": str(ack_path)}


def _acknowledged_event_hashes(ack: Dict[str, object]) -> set[str]:
    hashes: set[str] = set()
    for item in ack.get("acknowledged_events") or []:
        if isinstance(item, dict) and item.get("event_hash"):
            hashes.add(str(item.get("event_hash")))
    return hashes


def partial_pending_audit(path: Optional[Path] = None) -> Dict[str, object]:
    registry_file = path or registry_path()
    journal = registry_journal_path(registry_file)
    transitions: List[Dict[str, object]] = []
    unknown: List[Dict[str, object]] = []
    if journal.exists():
        try:
            lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line_number, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            before_values = {str(event.get("before_status") or ""), str(event.get("before_task_status") or "")}
            after_values = {str(event.get("after_status") or ""), str(event.get("after_task_status") or "")}
            if "partial" not in before_values or "pending" not in after_values:
                continue
            item = {
                "ts": event.get("ts"),
                "task_id": event.get("task_id"),
                "source": event.get("source") or "unknown",
                "reason": event.get("reason"),
                "actor": event.get("actor"),
                "before_status": event.get("before_status"),
                "after_status": event.get("after_status"),
                "before_task_status": event.get("before_task_status"),
                "after_task_status": event.get("after_task_status"),
                "registry_event_line": line_number,
            }
            item["event_hash"] = partial_pending_event_hash(item)
            transitions.append(item)
            if str(item["source"] or "unknown") == "unknown":
                unknown.append(item)
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "journal_path": str(journal),
        "transition_count": len(transitions),
        "unknown_count": len(unknown),
        "transitions": transitions,
        "unknown": unknown,
    }


def partial_pending_audit_ack_status(path: Optional[Path] = None, ack_path: Optional[Path] = None) -> Dict[str, object]:
    audit = partial_pending_audit(path=path)
    ack = load_partial_pending_acknowledgment(path=ack_path)
    acknowledged_hashes = _acknowledged_event_hashes(ack)
    unknown = audit.get("unknown") if isinstance(audit.get("unknown"), list) else []
    acknowledged: List[Dict[str, object]] = []
    unacknowledged: List[Dict[str, object]] = []
    for item in unknown:
        if not isinstance(item, dict):
            continue
        if str(item.get("event_hash") or "") in acknowledged_hashes:
            acknowledged.append(item)
        else:
            unacknowledged.append(item)
    if unacknowledged:
        status = "red"
        ok = False
    elif acknowledged:
        status = "yellow"
        ok = True
    else:
        status = "green"
        ok = True
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "ok": ok,
        "status": status,
        "audit": audit,
        "acknowledgment": ack,
        "unknown_count": len(unknown),
        "acknowledged_count": len(acknowledged),
        "unacknowledged_count": len(unacknowledged),
        "acknowledged": acknowledged,
        "unacknowledged": unacknowledged,
        "message": (
            f"{len(unacknowledged)} unacknowledged partial->pending unknown transitions"
            if unacknowledged
            else f"{len(acknowledged)} acknowledged legacy partial->pending unknown transitions"
            if acknowledged
            else "no partial->pending unknown transitions"
        ),
    }


def acknowledge_current_partial_pending_audit(reason: str, *, actor: str = "manual_maintenance", path: Optional[Path] = None, ack_path: Optional[Path] = None) -> Dict[str, object]:
    audit = partial_pending_audit(path=path)
    target = ack_path or partial_pending_ack_path()
    existing = load_partial_pending_acknowledgment(path=target)
    existing_by_hash: Dict[str, Dict[str, object]] = {}
    for item in existing.get("acknowledged_events") or []:
        if isinstance(item, dict) and item.get("event_hash"):
            existing_by_hash[str(item.get("event_hash"))] = dict(item)
    added: List[Dict[str, object]] = []
    for item in audit.get("unknown") or []:
        if not isinstance(item, dict):
            continue
        event_hash = str(item.get("event_hash") or partial_pending_event_hash(item))
        if event_hash in existing_by_hash:
            continue
        ack_item = {
            "event_hash": event_hash,
            "task_id": item.get("task_id"),
            "from_status": item.get("before_status"),
            "to_status": item.get("after_status"),
            "event_time": item.get("ts"),
            "source": item.get("source") or "unknown",
            "registry_event_line": item.get("registry_event_line"),
        }
        existing_by_hash[event_hash] = ack_item
        added.append(ack_item)
    payload = {
        "schema_version": 1,
        "audit_type": "partial_to_pending_unknown_transition",
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "created_by": existing.get("created_by") or actor,
        "updated_by": actor,
        "reason": reason,
        "acknowledged_events": sorted(existing_by_hash.values(), key=lambda x: (str(x.get("event_time") or ""), str(x.get("task_id") or ""), str(x.get("event_hash") or ""))),
        "policy": {
            "acknowledged_legacy_events": "warn_only",
            "new_unknown_partial_to_pending": "fail_closed",
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    status = partial_pending_audit_ack_status(path=path, ack_path=target)
    return {
        "schema_version": 1,
        "acknowledged": True,
        "path": str(target),
        "added_count": len(added),
        "total_acknowledged_count": len(payload["acknowledged_events"]),
        "added": added,
        "status": status,
    }


def action_requests_root() -> Path:
    return state_dir() / "action_requests"


def action_request_registry_path() -> Path:
    return action_requests_root() / "action_request_registry.jsonl"


def action_resolution_events_path() -> Path:
    return action_requests_root() / "action_resolution_event.jsonl"


def action_request_status_path() -> Path:
    return action_requests_root() / "action_request_status.yaml"


def ensure_action_request_dirs() -> None:
    for name in ("pending", "processing", "done", "rejected", "failed"):
        (action_requests_root() / name).mkdir(parents=True, exist_ok=True)


def registry_fingerprint(path: Optional[Path] = None) -> Dict[str, object]:
    target = path or registry_path()
    if not target.exists():
        return {"path": str(target), "exists": False, "mtime_ns": None, "size": 0, "sha256": ""}
    stat = target.stat()
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(target),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "sha256": digest.hexdigest(),
    }


def _action_request_id() -> str:
    stamp = now_iso().replace("-", "").replace(":", "").replace("+", "_").replace("T", "_")[:15]
    return f"act_{stamp}_{uuid.uuid4().hex[:10]}"


def _action_request_path(status: str, action_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", action_id).strip("._")
    return action_requests_root() / status / f"{safe}.json"


def _action_lifecycle_status(queue_status: str) -> str:
    return {
        "pending": "open",
        "processing": "open",
        "done": "resolved",
        "rejected": "rejected",
        "failed": "failed",
    }.get(queue_status, queue_status)


def _action_payload(request: Dict[str, object]) -> Dict[str, object]:
    return request.get("payload") if isinstance(request.get("payload"), dict) else {}


def _action_lifecycle_fields(request: Dict[str, object], status: str) -> Dict[str, object]:
    payload = _action_payload(request)
    resolution = request.get("resolution") if isinstance(request.get("resolution"), dict) else {}
    response = request.get("response") if isinstance(request.get("response"), dict) else {}
    if not resolution and response:
        resolution = {
            "decision": response.get("decision"),
            "processed_at": response.get("processed_at"),
            "message": response.get("message"),
            "reason_code": response.get("reason_code"),
        }
    return {
        "lifecycle_status": _action_lifecycle_status(status),
        "next_owner": payload.get("next_owner") or request.get("next_owner"),
        "blocks": payload.get("blocks") or request.get("blocks"),
        "blocks_claim_not_execution": bool(payload.get("blocks_claim_not_execution", request.get("blocks_claim_not_execution", False))),
        "blocks_execution": bool(payload.get("blocks_execution", request.get("blocks_execution", False))),
        "unlocks": payload.get("unlocks") if isinstance(payload.get("unlocks"), list) else [],
        "resolution": resolution,
    }


def _append_jsonl(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _record_action_lifecycle_event(
    request: Dict[str, object],
    *,
    status: str,
    event_type: str,
    previous_status: Optional[str] = None,
) -> None:
    action_id = str(request.get("action_id") or "")
    if not action_id:
        return
    fields = _action_lifecycle_fields(request, status)
    snapshot = {
        "schema_version": 1,
        "event_type": event_type,
        "action_id": action_id,
        "action_type": request.get("action_type"),
        "task_id": request.get("task_id"),
        "queue_status": status,
        "previous_queue_status": previous_status,
        "updated_at": now_iso(),
        "safe_level": request.get("safe_level"),
        "source": request.get("source"),
        "reason": request.get("reason"),
        "payload": _action_payload(request),
        **fields,
    }
    _append_jsonl(action_request_registry_path(), snapshot)
    if fields["lifecycle_status"] in {"resolved", "rejected", "failed"}:
        _append_jsonl(action_resolution_events_path(), snapshot)
    _refresh_action_request_status_index()


def _refresh_action_request_status_index() -> None:
    ensure_action_request_dirs()
    statuses = ["pending", "processing", "done", "rejected", "failed"]
    counts: Dict[str, int] = {}
    open_requests: List[Dict[str, object]] = []
    blocks_by_task: Dict[str, List[Dict[str, object]]] = {}
    unlocks: Dict[str, List[Dict[str, object]]] = {}
    for status in statuses:
        paths = sorted((action_requests_root() / status).glob("*.json"))
        counts[status] = len(paths)
        for path in paths:
            request = _load_action_request_file(path, status)
            fields = _action_lifecycle_fields(request, status)
            item = {
                "action_id": request.get("action_id"),
                "action_type": request.get("action_type"),
                "task_id": request.get("task_id"),
                "status": status,
                "lifecycle_status": fields["lifecycle_status"],
                "next_owner": fields["next_owner"],
                "blocks": fields["blocks"],
                "blocks_execution": fields["blocks_execution"],
                "blocks_claim_not_execution": fields["blocks_claim_not_execution"],
                "unlocks": fields["unlocks"],
                "reason": request.get("reason"),
            }
            if fields["lifecycle_status"] == "open":
                open_requests.append(item)
                task_id = str(request.get("task_id") or "")
                if task_id:
                    blocks_by_task.setdefault(task_id, []).append(item)
            for unlocked in fields["unlocks"]:
                unlocks.setdefault(str(unlocked), []).append(item)
    payload = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "root": str(action_requests_root()),
        "counts": counts,
        "open_count": len(open_requests),
        "open_requests": open_requests,
        "blocks_by_task": blocks_by_task,
        "unlocks": unlocks,
    }
    action_request_status_path().write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def create_action_request(
    action_type: str,
    *,
    task_id: Optional[str] = None,
    payload: Optional[Dict[str, object]] = None,
    reason: str = "",
    actor: str = "user",
    source: str = "dashboard",
    safe_level: str = "safe_after_revalidation",
    expected_task_status: Optional[str] = None,
    requires_scheduler_validation: bool = True,
) -> Dict[str, object]:
    mode = load_maintenance_mode()
    if mode.get("enabled") and not mode.get("allow_action_requests"):
        return {
            "created": False,
            "error": "maintenance mode is enabled; action request creation is disabled",
            "maintenance_mode": mode,
        }

    existing = _find_action_request(
        action_type,
        task_id=task_id,
        payload_match=payload or {},
    )
    if existing:
        return {
            "created": False,
            "reason": "already_exists",
            "action_id": existing.get("action_id"),
            "status": existing.get("_status_dir") or existing.get("status"),
            "request": existing,
        }

    ensure_action_request_dirs()
    action_id = _action_request_id()
    request = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": action_type,
        "source": source,
        "actor": actor,
        "created_at": now_iso(),
        "safe_level": safe_level,
        "registry_fingerprint_seen": registry_fingerprint(),
        "task_id": task_id,
        "expected_task_status": expected_task_status,
        "reason": reason,
        "payload": payload or {},
        "requires_scheduler_validation": requires_scheduler_validation,
        "status": "pending",
    }
    request.update(_action_lifecycle_fields(request, "pending"))
    final_path = _action_request_path("pending", action_id)
    tmp = final_path.with_suffix(final_path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(final_path)
    _record_action_lifecycle_event(request, status="pending", event_type="created")
    return {"created": True, "action_id": action_id, "status": "pending", "path": str(final_path), "request": request}


def _load_action_request_file(path: Path, status: str) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        payload = {"action_id": path.stem, "error": str(exc)}
    if isinstance(payload, dict):
        payload.setdefault("action_id", path.stem)
        payload["_status_dir"] = status
        payload["_path"] = str(path)
        payload.update(_action_lifecycle_fields(payload, status))
        return payload
    return {"action_id": path.stem, "error": "request JSON is not an object", "_status_dir": status, "_path": str(path)}


def list_action_requests(status: Optional[str] = None) -> Dict[str, object]:
    ensure_action_request_dirs()
    _refresh_action_request_status_index()
    statuses = [status] if status else ["pending", "processing", "done", "rejected", "failed"]
    requests: List[Dict[str, object]] = []
    counts: Dict[str, int] = {}
    for name in statuses:
        if name not in {"pending", "processing", "done", "rejected", "failed"}:
            continue
        paths = sorted((action_requests_root() / name).glob("*.json"))
        counts[name] = len(paths)
        requests.extend(_load_action_request_file(path, name) for path in paths)
    return {"schema_version": 1, "generated_at": now_iso(), "root": str(action_requests_root()), "counts": counts, "requests": requests}


def _find_action_request(
    action_type: str,
    *,
    task_id: Optional[str] = None,
    payload_match: Optional[Dict[str, object]] = None,
) -> Optional[Dict[str, object]]:
    ensure_action_request_dirs()
    match = payload_match or {}
    for status_name in ("pending", "processing", "done", "rejected", "failed"):
        for path in sorted((action_requests_root() / status_name).glob("*.json")):
            request = _load_action_request_file(path, status_name)
            if str(request.get("action_type") or "") != action_type:
                continue
            if task_id is not None and str(request.get("task_id") or "") != str(task_id):
                continue
            payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
            if all(payload.get(key) == value for key, value in match.items()):
                return request
    return None


def create_failure_action_request(
    task_id: str,
    *,
    run_id: str,
    failure_type: str,
    recommended_action: Optional[Dict[str, object]] = None,
    result_path: str = "",
    reason: str = "",
) -> Dict[str, object]:
    action = recommended_action or recommended_action_for_failure(failure_type)
    if not isinstance(action, dict) or action.get("type") != "create_action_request":
        return {"created": False, "reason": "failure_type_not_action_requestable", "failure_type": failure_type}
    action_type = "missing_capability_followup" if failure_type == "missing_capability" else "failure_followup"
    payload = {
        "run_id": run_id,
        "failure_type": failure_type,
        "recommended_action": action,
        "result_path": result_path,
    }
    existing = _find_action_request(
        action_type,
        task_id=task_id,
        payload_match={"run_id": run_id, "failure_type": failure_type},
    )
    if existing:
        return {
            "created": False,
            "reason": "already_exists",
            "action_id": existing.get("action_id"),
            "status": existing.get("_status_dir") or existing.get("status"),
            "request": existing,
        }
    return create_action_request(
        action_type,
        task_id=task_id,
        payload=payload,
        reason=reason or "missing_capability requires a follow-up capability/action request",
        actor="system",
        source="collector",
        safe_level="diagnostic",
        expected_task_status=None,
        requires_scheduler_validation=False,
    )


_ARTIFACT_ACTION_REQUEST_REQUIRED_FIELDS = {
    "kind",
    "missing_or_needed",
    "why_needed",
    "next_owner",
    "blocks",
    "blocks_claim_not_execution",
    "blocks_execution",
    "suggested_next_step",
}


def _artifact_action_requests_path(result_path: Path) -> Optional[Path]:
    for candidate in (result_path / "artifacts" / "action_requests.json", result_path / "action_requests.json"):
        if candidate.exists():
            return candidate
    return None


def _artifact_action_request_items(payload: object) -> List[Dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("action_requests", "requests", "actions_required"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_artifact_action_request(item: Dict[str, object]) -> Dict[str, object]:
    normalized = dict(item)
    if "missing_or_needed" not in normalized and normalized.get("action"):
        normalized["missing_or_needed"] = str(normalized.get("action") or "")
    if "missing_or_needed" not in normalized and normalized.get("message"):
        normalized["missing_or_needed"] = str(normalized.get("message") or "")
    if "why_needed" not in normalized:
        for key in ("justification", "reason", "rationale", "requested_action"):
            if normalized.get(key):
                normalized["why_needed"] = str(normalized.get(key) or "")
                break
    normalized.setdefault("kind", "request_context")
    kind = str(normalized.get("kind") or "").strip().lower()
    if kind == "missing_capability":
        normalized.setdefault("next_owner", "codex")
        normalized.setdefault("blocks", "execution")
        normalized.setdefault("blocks_claim_not_execution", False)
        normalized.setdefault("blocks_execution", True)
    else:
        normalized.setdefault("next_owner", "scheduler")
        normalized.setdefault("blocks", "claim")
        normalized.setdefault("blocks_claim_not_execution", True)
        normalized.setdefault("blocks_execution", False)
    normalized.setdefault("suggested_next_step", str(normalized.get("why_needed") or "Review the requested follow-up."))
    normalized["blocks_claim_not_execution"] = bool(normalized.get("blocks_claim_not_execution"))
    normalized["blocks_execution"] = bool(normalized.get("blocks_execution"))
    if "unlocks" in normalized and not isinstance(normalized.get("unlocks"), list):
        normalized["unlocks"] = [str(normalized.get("unlocks"))]
    return normalized


def _artifact_action_request_quality_errors(item: Dict[str, object], known_task_ids: Optional[set[str]] = None) -> List[str]:
    errors: List[str] = []
    missing_fields = sorted(field for field in _ARTIFACT_ACTION_REQUEST_REQUIRED_FIELDS if item.get(field) in (None, ""))
    errors.extend(f"missing_required_field:{field}" for field in missing_fields)
    why = str(item.get("why_needed") or "").strip().lower()
    if why in {"need more context", "needs more context", "more context", "need context", "unknown", "n/a", "na"}:
        errors.append("why_needed_too_generic")
    next_owner = str(item.get("next_owner") or "").strip().lower()
    if next_owner not in {"user", "scheduler", "materializer", "codex", "human_reviewer"}:
        errors.append("next_owner_invalid")
    blocks = str(item.get("blocks") or "").strip().lower()
    if blocks not in {"claim", "execution", "routing", "collect"}:
        errors.append("blocks_invalid")
    kind = str(item.get("kind") or "").strip().lower()
    needed = str(item.get("missing_or_needed") or "").strip()
    if kind == "request_approval" and not needed.startswith("allow_"):
        errors.append("request_approval_missing_flag")
    if bool(item.get("blocks_execution")) and blocks != "execution":
        errors.append("blocks_execution_requires_blocks_execution")
    if bool(item.get("blocks_claim_not_execution")) and bool(item.get("blocks_execution")):
        errors.append("ambiguous_claim_and_execution_block")
    unlocks = item.get("unlocks")
    if unlocks is not None:
        if not isinstance(unlocks, list):
            errors.append("unlocks_not_list")
        else:
            for unlocked in unlocks:
                unlocked_id = str(unlocked or "").strip()
                if not unlocked_id:
                    errors.append("unlock_target_empty")
                elif known_task_ids is not None and unlocked_id not in known_task_ids:
                    errors.append(f"unlock_target_unknown:{unlocked_id}")
    return errors


def _artifact_action_request_safe_level(item: Dict[str, object]) -> str:
    next_owner = str(item.get("next_owner") or "").strip().lower()
    if item.get("blocks_execution") or next_owner in {"codex", "human_reviewer", "user"}:
        return "diagnostic"
    if next_owner == "scheduler":
        return "safe_after_revalidation"
    return "diagnostic"


def create_artifact_action_requests(
    task_id: str,
    *,
    run_id: str,
    result_path: str | Path,
) -> Dict[str, object]:
    source_result = Path(result_path)
    source_path = _artifact_action_requests_path(source_result)
    if source_path is None:
        return {"created": [], "existing": [], "skipped": [], "source_path": None}
    try:
        raw_payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "created": [],
            "existing": [],
            "skipped": [{"reason": "invalid_json", "error": str(exc), "source_path": str(source_path)}],
            "source_path": str(source_path),
        }
    created: List[Dict[str, object]] = []
    existing: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    registry = load_registry()
    tasks = registry.get("tasks") if isinstance(registry.get("tasks"), dict) else {}
    known_task_ids = {str(task_id) for task_id in tasks.keys()}
    for index, raw_item in enumerate(_artifact_action_request_items(raw_payload)):
        item = _normalize_artifact_action_request(raw_item)
        quality_errors = _artifact_action_request_quality_errors(item, known_task_ids=known_task_ids)
        if quality_errors:
            skipped.append({"index": index, "reason": "invalid_action_request", "errors": quality_errors, "item": raw_item})
            continue
        payload = {
            "source_run_id": str(run_id),
            "source_result_path": str(source_result),
            "source_action_requests_path": str(source_path),
            "source_index": index,
            "kind": str(item.get("kind") or ""),
            "missing_or_needed": str(item.get("missing_or_needed") or ""),
            "why_needed": str(item.get("why_needed") or ""),
            "next_owner": str(item.get("next_owner") or ""),
            "blocks": str(item.get("blocks") or ""),
            "blocks_claim_not_execution": bool(item.get("blocks_claim_not_execution")),
            "blocks_execution": bool(item.get("blocks_execution")),
            "suggested_next_step": str(item.get("suggested_next_step") or ""),
            "unlocks": item.get("unlocks") if isinstance(item.get("unlocks"), list) else [],
        }
        action_type = "artifact_followup"
        duplicate = _find_action_request(
            action_type,
            task_id=task_id,
            payload_match={
                "source_run_id": payload["source_run_id"],
                "kind": payload["kind"],
                "missing_or_needed": payload["missing_or_needed"],
            },
        )
        if duplicate:
            existing.append(
                {
                    "action_id": duplicate.get("action_id"),
                    "status": duplicate.get("_status_dir") or duplicate.get("status"),
                    "kind": payload["kind"],
                    "missing_or_needed": payload["missing_or_needed"],
                }
            )
            continue
        result = create_action_request(
            action_type,
            task_id=task_id,
            payload=payload,
            reason=f"{payload['kind']}: {payload['missing_or_needed']}",
            actor="system",
            source="collector",
            safe_level=_artifact_action_request_safe_level(payload),
            expected_task_status=None,
            requires_scheduler_validation=False,
        )
        if result.get("created"):
            created.append(
                {
                    "action_id": result.get("action_id"),
                    "status": result.get("status"),
                    "kind": payload["kind"],
                    "missing_or_needed": payload["missing_or_needed"],
                }
            )
        else:
            skipped.append({"index": index, "reason": "create_failed", "result": result, "item": raw_item})
    return {"created": created, "existing": existing, "skipped": skipped, "source_path": str(source_path)}


def _move_action_request(path: Path, target_status: str, payload: Dict[str, object]) -> Path:
    ensure_action_request_dirs()
    action_id = str(payload.get("action_id") or path.stem)
    previous_status = str(payload.get("_status_dir") or payload.get("status") or "")
    payload["status"] = target_status
    payload.update(_action_lifecycle_fields(payload, target_status))
    target = _action_request_path(target_status, action_id)
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(target)
    try:
        if path.exists() and path.resolve() != target.resolve():
            path.unlink()
    except OSError:
        pass
    _record_action_lifecycle_event(payload, status=target_status, event_type="transition", previous_status=previous_status)
    return target


def _validate_action_request(request: Dict[str, object]) -> List[str]:
    validation: List[str] = []
    action_type = str(request.get("action_type") or "")
    task_id = str(request.get("task_id") or "")
    records = load_registry().get("tasks", {})
    record = records.get(task_id) if task_id and isinstance(records, dict) else None
    expected = request.get("expected_task_status")
    requires_scheduler_validation = bool(request.get("requires_scheduler_validation", True))
    if task_id:
        if not isinstance(record, dict):
            if not requires_scheduler_validation:
                validation.append("task was not found; scheduler validation skipped")
                return validation
            raise ValueError(f"task not found: {task_id}")
        validation.append("task exists")
        if expected:
            current = str(record.get("status") or "")
            if current != str(expected):
                raise ValueError(f"expected task status {expected}, current status is {current}")
            validation.append(f"task status is still {expected}")
    if action_type in {"schedule_once", "dependency_reconcile_apply_safe"}:
        validation.append("global action does not require a task_id")
    return validation


def _execute_action_request(
    request: Dict[str, object],
    *,
    allow_launch: bool = False,
    allow_dangerous: bool = False,
) -> Dict[str, object]:
    action_type = str(request.get("action_type") or "")
    task_id = str(request.get("task_id") or "")
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    validation = _validate_action_request(request)
    before = registry_fingerprint()
    seen = request.get("registry_fingerprint_seen") if isinstance(request.get("registry_fingerprint_seen"), dict) else {}
    fingerprint_changed = bool(seen.get("sha256") and seen.get("sha256") != before.get("sha256"))
    if fingerprint_changed:
        validation.append("registry fingerprint changed since request creation; action was revalidated before execution")
    if action_type == "dependency_reconcile_apply_safe":
        result = dependency_reconcile(apply_safe=True)
        message = "dependency reconcile apply-safe completed"
    elif action_type == "registry_hygiene_apply_safe":
        result = registry_hygiene(apply_safe=True)
        message = "registry hygiene apply-safe completed"
    elif action_type == "enter_drain":
        result = set_drain_mode(True, reason=str(request.get("reason") or "entered from action request"))
        message = "drain mode enabled"
    elif action_type == "exit_drain":
        result = set_drain_mode(False, reason=str(request.get("reason") or "exited from action request"))
        message = "drain mode disabled"
    elif action_type == "schedule_once":
        if not allow_launch:
            raise PermissionError("schedule_once requires --allow-launch")
        scheduler = scheduler_status()
        if scheduler.get("running"):
            raise RuntimeError("background scheduler is running; schedule_once rejected")
        guard = scheduler_startup_guard(
            host=str(payload.get("host") or ""),
            remote_root=str(payload.get("remote_root") or ""),
            remote_workdir=str(payload.get("remote_workdir") or ""),
            max_starts=int(payload.get("max_starts") or 1),
            max_running_total=int(payload.get("max_running_total") or 8),
            max_running_gpu=int(payload.get("max_running_gpu") or 4),
            max_running_cpu=int(payload.get("max_running_cpu") or 4),
            min_free_gpu_mem_gb=float(payload.get("min_free_gpu_mem_gb") or 10.0),
            allowed_gpu_ids=[str(item) for item in payload.get("allowed_gpu_ids", []) or []],
            max_tasks_per_gpu=int(payload.get("max_tasks_per_gpu") or 2),
            worker=str(payload.get("worker") or "materialized_execution"),
        )
        if not guard.get("ok"):
            raise RuntimeError("startup guard failed; schedule_once rejected")
        lock = acquire_scheduler_lock("action-request-schedule-once")
        if not lock.get("acquired"):
            raise RuntimeError("scheduler lock is held")
        try:
            reconnect = remote_reconcile_running_tasks()
            result = schedule_remote(
                host=str(payload.get("host") or ""),
                remote_root=str(payload.get("remote_root") or ""),
                remote_workdir=str(payload.get("remote_workdir") or ""),
                max_starts=int(payload.get("max_starts") or 1),
                max_running_total=int(payload.get("max_running_total") or 8),
                max_running_gpu=int(payload.get("max_running_gpu") or 4),
                max_running_cpu=int(payload.get("max_running_cpu") or 4),
                min_free_gpu_mem_gb=float(payload.get("min_free_gpu_mem_gb") or 10.0),
                allowed_gpu_ids=[str(item) for item in payload.get("allowed_gpu_ids", []) or []],
                max_tasks_per_gpu=int(payload.get("max_tasks_per_gpu") or 2),
                worker=str(payload.get("worker") or "materialized_execution"),
                dry_run=False,
            )
            result["reconnect_reconcile"] = reconnect
        finally:
            release_scheduler_lock()
        message = "schedule once completed"
    elif action_type == "requeue_partial":
        if not allow_dangerous:
            raise PermissionError("requeue_partial requires --allow-dangerous")
        result = manual_pause(task_id, reason=str(request.get("reason") or "Requeued from action request"), reset_to_pending=True, explicit_requeue=True)
        message = f"{task_id} requeued to pending"
    elif action_type == "remote_stop":
        if not allow_dangerous:
            raise PermissionError("remote_stop requires --allow-dangerous")
        result = remote_stop(task_id, host=str(payload.get("host") or ""), remote_root=str(payload.get("remote_root") or ""), reason=str(request.get("reason") or "stopped from action request"))
        message = f"{task_id} remote stop requested"
    elif action_type in {"missing_capability_followup", "failure_followup"}:
        result = {"noted": True, "task_id": task_id, "payload": payload}
        message = "failure follow-up recorded"
    elif action_type == "artifact_followup":
        result = {"noted": True, "task_id": task_id, "payload": payload}
        message = "artifact follow-up recorded"
    else:
        raise ValueError(f"unsupported action_type: {action_type}")
    after = registry_fingerprint()
    return {
        "action_id": request.get("action_id"),
        "processed_at": now_iso(),
        "decision": "accepted",
        "message": message,
        "validation": validation,
        "registry_fingerprint_before": before,
        "registry_fingerprint_after": after,
        "fingerprint_changed_since_request": fingerprint_changed,
        "result": result,
    }


def process_action_requests(
    *,
    apply_safe: bool = False,
    apply_selected: Optional[str] = None,
    allow_launch: bool = False,
    allow_dangerous: bool = False,
) -> Dict[str, object]:
    ensure_action_request_dirs()
    selected = str(apply_selected or "")
    pending_paths = sorted((action_requests_root() / "pending").glob("*.json"))
    processed: List[Dict[str, object]] = []
    for path in pending_paths:
        request = _load_action_request_file(path, "pending")
        action_id = str(request.get("action_id") or path.stem)
        if selected and action_id != selected:
            continue
        action_type = str(request.get("action_type") or "")
        safe_level = str(request.get("safe_level") or "")
        if apply_safe and safe_level not in {"safe_after_revalidation", "diagnostic"}:
            request["response"] = {
                "action_id": action_id,
                "processed_at": now_iso(),
                "decision": "rejected",
                "reason_code": "not_apply_safe",
                "message": f"{action_type} is not safe for --apply-safe",
            }
            rejected = _move_action_request(path, "rejected", request)
            processed.append({"action_id": action_id, "status": "rejected", "path": str(rejected), "reason": "not_apply_safe"})
            continue
        processing = _move_action_request(path, "processing", request)
        request = _load_action_request_file(processing, "processing")
        try:
            response = _execute_action_request(request, allow_launch=allow_launch, allow_dangerous=allow_dangerous)
            request["response"] = response
            done = _move_action_request(processing, "done", request)
            processed.append({"action_id": action_id, "status": "done", "path": str(done), "response": response})
        except PermissionError as exc:
            request["response"] = {
                "action_id": action_id,
                "processed_at": now_iso(),
                "decision": "rejected",
                "reason_code": "permission_required",
                "message": str(exc),
            }
            rejected = _move_action_request(processing, "rejected", request)
            processed.append({"action_id": action_id, "status": "rejected", "path": str(rejected), "reason": str(exc)})
        except Exception as exc:
            request["response"] = {
                "action_id": action_id,
                "processed_at": now_iso(),
                "decision": "failed",
                "reason_code": type(exc).__name__,
                "message": str(exc),
            }
            failed = _move_action_request(processing, "failed", request)
            processed.append({"action_id": action_id, "status": "failed", "path": str(failed), "reason": str(exc)})
        if selected:
            break
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "apply_safe": apply_safe,
        "apply_selected": apply_selected,
        "allow_launch": allow_launch,
        "allow_dangerous": allow_dangerous,
        "processed": processed,
        "queues": list_action_requests().get("counts", {}),
    }


def _transition_nearby_events(journal: Path, transition: Dict[str, object], *, window_lines: int = 3) -> List[Dict[str, object]]:
    if not journal.exists():
        return []
    try:
        lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    target_task = str(transition.get("task_id") or "")
    target_ts = str(transition.get("ts") or "")
    matches: List[int] = []
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("task_id") or "") == target_task and str(event.get("ts") or "") == target_ts:
            matches.append(index)
    nearby: List[Dict[str, object]] = []
    seen = set()
    for match in matches:
        start = max(0, match - window_lines)
        end = min(len(lines), match + window_lines + 1)
        for index in range(start, end):
            if index in seen:
                continue
            seen.add(index)
            try:
                event = json.loads(lines[index])
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                nearby.append(event)
    return nearby


def _classify_partial_pending_transition(transition: Dict[str, object], record: Dict[str, object]) -> Dict[str, object]:
    source = str(transition.get("source") or "unknown")
    reason = str(transition.get("reason") or "")
    actor = str(transition.get("actor") or "")
    current_status = str(record.get("status") or "")
    runs = [run for run in record.get("runs", []) or [] if isinstance(run, dict)]
    has_later_terminal_run = bool(
        run.get("collected_at") or run.get("finished_at") or str(run.get("status") or "") in {"completed", "partial", "failed"}
        for run in runs
    )
    latest_valid = record.get("latest_valid_run_id")
    if source in {"manual_pause", "dashboard_requeue", "explicit_requeue"} or "requeue" in reason.lower() or actor == "user":
        classification = "explicit_or_manual_likely"
        confidence = "medium"
    elif source == "unknown":
        classification = "unknown_legacy_transition"
        confidence = "low"
    else:
        classification = "scheduler_or_helper_transition"
        confidence = "medium"
    notes: List[str] = []
    if source == "unknown":
        notes.append("event predates/source logging was missing, so actor cannot be proven from journal alone")
    if current_status in {"completed", "partial", "failed"} and has_later_terminal_run:
        notes.append(f"task currently has terminal status {current_status}; later collection/rerun appears to have resolved the queue state")
    elif current_status == "pending":
        notes.append("task is still pending; inspect whether the requeue was intended before clearing guard")
    if latest_valid:
        notes.append(f"latest_valid_run_id={latest_valid}")
    return {
        "classification": classification,
        "confidence": confidence,
        "current_status": current_status,
        "latest_valid_run_id": latest_valid,
        "has_later_terminal_run": has_later_terminal_run,
        "notes": notes,
    }


def _write_partial_pending_audit_markdown(report: Dict[str, object], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = report.get("transitions") if isinstance(report.get("transitions"), list) else []
    lines = [
        "# Partial To Pending Audit",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Journal: {report.get('journal_path')}",
        f"- Transition count: {report.get('transition_count')}",
        f"- Unknown source count: {report.get('unknown_count')}",
        "",
        "## Summary",
        "",
    ]
    if not rows:
        lines.append("No partial -> pending transitions were found.")
    else:
        lines.extend(
            [
                "| Time | Task | Source | Actor | Classification | Current | Latest valid run | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in rows:
            classification = item.get("classification", {}) if isinstance(item.get("classification"), dict) else {}
            notes = "; ".join(str(note) for note in classification.get("notes", []) or [])
            lines.append(
                "| "
                + " | ".join(
                    str(value).replace("|", "/")
                    for value in [
                        item.get("ts") or "",
                        item.get("task_id") or "",
                        item.get("source") or "",
                        item.get("actor") or "",
                        classification.get("classification") or "",
                        classification.get("current_status") or "",
                        classification.get("latest_valid_run_id") or "",
                        notes,
                    ]
                )
                + " |"
            )
    lines.extend(["", "## Detail", ""])
    for item in rows:
        classification = item.get("classification", {}) if isinstance(item.get("classification"), dict) else {}
        lines.extend(
            [
                f"### {item.get('task_id')} at {item.get('ts')}",
                "",
                f"- Before: status={item.get('before_status')} task_status={item.get('before_task_status')}",
                f"- After: status={item.get('after_status')} task_status={item.get('after_task_status')}",
                f"- Source: {item.get('source')}",
                f"- Reason: {item.get('reason')}",
                f"- Actor: {item.get('actor')}",
                f"- Classification: {classification.get('classification')} ({classification.get('confidence')} confidence)",
                f"- Current status: {classification.get('current_status')}",
                f"- Latest valid run: {classification.get('latest_valid_run_id')}",
                "",
                "Notes:",
            ]
        )
        notes = classification.get("notes", []) if isinstance(classification, dict) else []
        lines.extend([f"- {note}" for note in notes] or ["- None"])
        nearby = item.get("nearby_events") if isinstance(item.get("nearby_events"), list) else []
        lines.extend(["", "Nearby registry events:", "", "```json"])
        lines.append(json.dumps(nearby, indent=2, ensure_ascii=False, default=str))
        lines.extend(["```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_partial_pending_audit_report(path: Optional[Path] = None) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    registry_file = path or registry_path()
    report = partial_pending_audit(path=registry_file)
    registry = load_registry(registry_file)
    records = registry.get("tasks", {}) if isinstance(registry.get("tasks"), dict) else {}
    journal = Path(str(report.get("journal_path")))
    enriched: List[Dict[str, object]] = []
    for item in report.get("transitions", []) or []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or "")
        record = records.get(task_id) if isinstance(records.get(task_id), dict) else {}
        row = dict(item)
        row["classification"] = _classify_partial_pending_transition(item, record)
        row["nearby_events"] = _transition_nearby_events(journal, item)
        enriched.append(row)
    report["transitions"] = enriched
    report["unknown"] = [item for item in enriched if str(item.get("source") or "unknown") == "unknown"]
    report["unknown_count"] = len(report["unknown"])
    state_path = state_dir() / "partial_pending_audit.json"
    report_path = root() / "reports" / "partial_pending_audit.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["state_path"] = str(state_path)
    report["report_path"] = str(report_path)
    state_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    _write_partial_pending_audit_markdown(report, report_path)
    return report


def input_binding_audit(path: Optional[Path] = None) -> Dict[str, object]:
    registry = load_registry(path)
    tasks = registry.get("tasks", {}) if isinstance(registry.get("tasks"), dict) else {}
    issues: List[Dict[str, object]] = []
    for task_id, record in sorted(tasks.items()):
        if not isinstance(record, dict):
            continue
        bindings = record.get("input_bindings")
        if not isinstance(bindings, dict):
            continue
        for source_task_id, binding in sorted(bindings.items()):
            if not isinstance(binding, dict):
                issues.append({"task_id": task_id, "source_task_id": source_task_id, "code": "binding_not_object"})
                continue
            parent = tasks.get(str(source_task_id))
            if not isinstance(parent, dict):
                issues.append({"task_id": task_id, "source_task_id": source_task_id, "code": "source_task_missing"})
                continue
            run_id = str(binding.get("run_id") or "")
            run = _find_run_record(parent, run_id)
            if not run:
                issues.append({"task_id": task_id, "source_task_id": source_task_id, "run_id": run_id, "code": "source_run_missing"})
                continue
            if not _run_is_input_usable(run):
                issues.append({"task_id": task_id, "source_task_id": source_task_id, "run_id": run_id, "code": "source_run_not_usable"})
            artifact_root = binding.get("artifact_root")
            if artifact_root and not Path(str(artifact_root)).exists():
                issues.append({"task_id": task_id, "source_task_id": source_task_id, "run_id": run_id, "code": "artifact_root_missing", "artifact_root": str(artifact_root)})
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "issue_count": len(issues),
        "issues": issues,
    }


def scheduler_startup_guard(
    *,
    host: str,
    remote_root: str,
    remote_workdir: Optional[str],
    max_starts: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: List[str],
    max_tasks_per_gpu: int,
    gpu_snapshot_max_age_seconds: int = 120,
    worker: str = "materialized_execution",
) -> Dict[str, object]:
    maintenance = load_maintenance_mode()
    hygiene = registry_hygiene(apply_safe=False)
    reconcile = dependency_reconcile(apply_safe=False)
    binding_audit = input_binding_audit()
    partial_audit = partial_pending_audit()
    partial_ack_status = partial_pending_audit_ack_status()
    plan = schedule_remote(
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        max_starts=max_starts,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed_gpu_ids,
        max_tasks_per_gpu=max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=gpu_snapshot_max_age_seconds,
        gpu_snapshot_cache_only=True,
        worker=worker,
        dry_run=True,
    )
    failures: List[Dict[str, object]] = []
    if maintenance.get("enabled") and not maintenance.get("allow_scheduler_launch"):
        failures.append({"code": "maintenance_mode_disallows_scheduler_launch", "reason": maintenance.get("reason")})
    if hygiene.get("issue_count"):
        failures.append({"code": "registry_hygiene_issues", "issue_count": hygiene.get("issue_count")})
    if partial_ack_status.get("unacknowledged_count"):
        failures.append({
            "code": "partial_to_pending_unknown_source",
            "unknown_count": partial_ack_status.get("unknown_count"),
            "unacknowledged_count": partial_ack_status.get("unacknowledged_count"),
        })
    if binding_audit.get("issue_count"):
        failures.append({"code": "input_binding_issues", "issue_count": binding_audit.get("issue_count")})
    if any(plan.get("started") or []):
        failures.append({"code": "plan_only_started_tasks", "started": plan.get("started")})
    payload = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "ok": not failures,
        "status": "red" if failures else ("yellow" if partial_ack_status.get("acknowledged_count") else "green"),
        "failures": failures,
        "warnings": (
            [{
                "code": "partial_to_pending_acknowledged_legacy",
                "acknowledged_count": partial_ack_status.get("acknowledged_count"),
                "message": partial_ack_status.get("message"),
            }]
            if partial_ack_status.get("acknowledged_count") and not partial_ack_status.get("unacknowledged_count")
            else []
        ),
        "maintenance_mode": maintenance,
        "registry_hygiene": hygiene,
        "dependency_reconcile": reconcile,
        "input_binding_audit": binding_audit,
        "partial_pending_audit": partial_audit,
        "partial_pending_ack_status": partial_ack_status,
        "schedule_plan": plan,
    }
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "scheduler_startup_guard.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def _select_parent_run(record: Dict[str, object], requirement: Dict[str, object]) -> Optional[Dict[str, object]]:
    strategy = str(requirement.get("resolve_strategy") or "latest_success").strip().lower()
    pinned = requirement.get("run_id") or requirement.get("pinned_run_id")
    if strategy == "pinned_run" or pinned:
        return _find_run_record(record, pinned)
    key_by_strategy = {
        "latest_success": "latest_success_run_id",
        "latest_partial": "latest_partial_run_id",
        "latest_valid": "latest_valid_run_id",
    }
    key = key_by_strategy.get(strategy, "latest_success_run_id")
    run = _find_run_record(record, record.get(key))
    if run:
        return run
    acceptable = _normalize_acceptable_statuses(requirement.get("acceptable_status"))
    for candidate in reversed(record.get("runs", []) or []):
        if not isinstance(candidate, dict):
            continue
        status_value = _canonical_run_status(candidate.get("status"))
        if status_value not in acceptable:
            continue
        if strategy == "latest_success" and status_value != "completed":
            continue
        if strategy == "latest_partial" and status_value != "partial":
            continue
        if not _run_is_input_usable(candidate):
            continue
        return candidate
    return None


def _input_requirement_diagnosis(requirement: Dict[str, object], records: Dict[str, object]) -> Dict[str, object]:
    dep = str(requirement.get("from_task") or requirement.get("task_id") or "")
    acceptable = _normalize_acceptable_statuses(requirement.get("acceptable_status"))
    required_artifacts = [str(item) for item in requirement.get("required_artifacts", []) or []] or _default_input_artifacts()
    partial_policy = str(requirement.get("partial_policy") or "forbid").strip().lower()
    if partial_policy not in {"forbid", "ask", "auto"}:
        partial_policy = "forbid"
    state: Dict[str, object] = {
        "from_task": dep,
        "resolve_strategy": str(requirement.get("resolve_strategy") or "latest_success"),
        "acceptable_status": sorted(acceptable),
        "required_artifacts": required_artifacts,
        "partial_policy": partial_policy,
        "ready": False,
        "reason": "",
        "candidate_actions": [],
    }
    record = records.get(dep) if dep else None
    if not isinstance(record, dict):
        state["reason"] = "dependency_record_missing"
        return state
    run = _select_parent_run(record, requirement)
    if not run:
        state["reason"] = "dependency_no_valid_run"
        return state
    run_id = str(run.get("run_id") or "")
    status_value = _canonical_run_status(run.get("status"))
    result_path = _run_result_path(run)
    state.update(
        {
            "candidate_run_id": run_id,
            "parent_status": status_value,
            "result_path": str(result_path) if result_path else None,
            "remote_task_dir": run.get("remote_task_dir"),
            "remote_out_dir": run.get("remote_out_dir"),
        }
    )
    if status_value not in acceptable:
        state["reason"] = "dependency_status_not_acceptable"
        return state
    if not result_path:
        state["reason"] = "dependency_result_path_missing"
        state["missing_artifacts"] = required_artifacts
        return state
    contract = _artifact_contract(result_path, required_artifacts)
    state.update(contract)
    if not contract.get("ok"):
        state["reason"] = "dependency_missing_artifact"
        return state
    if status_value == "partial":
        if partial_policy == "forbid":
            state["reason"] = "dependency_partial_forbidden"
            state["candidate_actions"] = [
                {
                    "candidate_id": f"ask__{dep}__partial_input",
                    "action_type": "ask_user",
                    "parent_task_id": dep,
                    "use_run_id": run_id,
                    "safe_level": "needs_confirmation",
                    "reason": "Parent has valid partial artifacts, but this task forbids partial inputs.",
                }
            ]
            return state
        if partial_policy == "ask":
            state["reason"] = "dependency_partial_needs_user_decision"
            state["candidate_actions"] = [
                {
                    "candidate_id": f"unblock__{dep}__use_partial_{run_id}",
                    "action_type": "use_partial_as_input",
                    "parent_task_id": dep,
                    "use_run_id": run_id,
                    "safe_level": "needs_confirmation",
                    "reason": "Parent has valid partial artifacts; user confirmation is required.",
                }
            ]
            return state
    if status_value == "failed" and not bool(requirement.get("allow_failed_artifacts")):
        state["reason"] = "dependency_failed_needs_user_decision"
        return state
    binding_reason = "dependency_reconciled_from_partial" if status_value == "partial" else "dependency_reconciled_from_success"
    state["ready"] = True
    state["reason"] = binding_reason
    caveats = list(run.get("caveats", []) or [])
    if status_value == "partial":
        caveats.append("Input came from a partial run; use for analysis only.")
    state["binding"] = {
        "run_id": run_id,
        "source_status": status_value,
        "artifact_root": str(result_path),
        "remote_artifact_root": run.get("remote_task_dir"),
        "remote_out_dir": run.get("remote_out_dir"),
        "artifacts": contract.get("available_artifacts", {}),
        "bound_at": now_iso(),
        "binding_reason": binding_reason,
        "mark_partial_input_in_report": bool(requirement.get("mark_partial_input_in_report", status_value == "partial")),
        "caveats": caveats,
    }
    state["candidate_actions"] = [
        {
            "candidate_id": f"unblock__{dep}__{run_id}",
            "action_type": "promote_blocked_to_ready_front",
            "parent_task_id": dep,
            "use_run_id": run_id,
            "safe_level": "safe_after_revalidation",
            "reason": "Parent artifacts satisfy the input requirement.",
        }
    ]
    return state


def dependency_state(task: TaskBook) -> Dict[str, object]:
    registry = load_registry()
    records = registry.get("tasks", {})
    depends_on = [str(item) for item in task.raw_front_matter.get("depends_on", []) or []]
    requires_artifacts_from = [str(item) for item in task.raw_front_matter.get("requires_artifacts_from", []) or []]
    allow_partial_dependencies = {str(item) for item in task.raw_front_matter.get("allow_partial_dependencies", []) or []}
    allow_partial_artifacts_from = {str(item) for item in task.raw_front_matter.get("allow_partial_artifacts_from", []) or []}
    input_requirements = task.raw_front_matter.get("input_requirements", []) or []
    allows_soft_continue = _task_allows_soft_dependency_continue(task)
    missing = []
    incomplete = []
    missing_artifacts = []
    soft_dependencies = []
    soft_missing_artifacts = []
    input_requirement_state: List[Dict[str, object]] = []
    for dep in depends_on:
        record = records.get(dep)
        if not record:
            if allows_soft_continue and dep in allow_partial_dependencies:
                soft_dependencies.append(
                    _soft_dependency_entry(dep, "missing", "dependency_missing_allowed_for_report")
                )
                continue
            missing.append(dep)
            continue
        effective_status = _dependency_effective_status(record, dep in allow_partial_dependencies)
        if effective_status is None:
            soft_status = _soft_dependency_status(record)
            if allows_soft_continue and dep in allow_partial_dependencies and soft_status in {"partial", "failed", "launch_failed", "launch_unknown"}:
                soft_dependencies.append(
                    _soft_dependency_entry(
                        dep,
                        soft_status,
                        "dependency_terminal_incomplete_allowed_for_report",
                        detail="parent did not provide a completed conclusion; child may still summarize or diagnose available state",
                    )
                )
                continue
            incomplete.append({"task_id": dep, "status": record.get("status")})
    for dep in requires_artifacts_from:
        record = records.get(dep)
        if not record:
            if allows_soft_continue and dep in allow_partial_artifacts_from:
                soft_missing_artifacts.append(
                    _soft_dependency_entry(dep, "missing", "artifact_source_missing_allowed_for_report")
                )
                continue
            missing_artifacts.append(dep)
            continue
        allowed_statuses = {"completed"}
        if dep in allow_partial_artifacts_from:
            allowed_statuses.add("partial")
        result_path = _existing_result_path_for_status(record, allowed_statuses, include_historical_runs=False)
        if not result_path:
            soft_status = _soft_dependency_status(record)
            if allows_soft_continue and dep in allow_partial_artifacts_from and soft_status in {"partial", "failed", "launch_failed", "launch_unknown"}:
                soft_missing_artifacts.append(
                    _soft_dependency_entry(
                        dep,
                        soft_status,
                        "artifact_missing_allowed_for_report",
                        detail="parent artifacts are incomplete or absent; child may report the missing evidence instead of blocking the whole batch",
                    )
                )
                continue
            missing_artifacts.append(dep)
    for requirement in input_requirements:
        if not isinstance(requirement, dict):
            continue
        state = _input_requirement_diagnosis(requirement, records)
        dep = str(state.get("from_task") or "")
        if not dep:
            continue
        reason = str(state.get("reason") or "")
        if reason == "dependency_record_missing":
            missing.append(dep)
        elif reason in {"dependency_no_valid_run", "dependency_result_path_missing", "dependency_missing_artifact"}:
            missing_artifacts.append(dep)
        elif not state.get("ready"):
            incomplete.append({"task_id": dep, "status": state.get("parent_status"), "reason": reason})
        input_requirement_state.append(state)
    explicit_override = bool(task.raw_front_matter.get("allow_artifact_dependency_override_depends_on", False))
    input_satisfied_deps = {str(state.get("from_task") or "") for state in input_requirement_state if isinstance(state, dict) and state.get("ready")}
    semantic_conflicts = sorted(input_satisfied_deps & set(depends_on))
    if semantic_conflicts and not explicit_override:
        for dep in semantic_conflicts:
            incomplete.append(
                {
                    "task_id": dep,
                    "status": records.get(dep, {}).get("status") if isinstance(records.get(dep), dict) else None,
                    "reason": "dependency_semantic_conflict",
                    "detail": "same parent appears in both depends_on and input_requirements",
                }
            )
    if input_satisfied_deps:
        overrideable = input_satisfied_deps if explicit_override else (input_satisfied_deps - set(depends_on))
        incomplete = [
            entry
            for entry in incomplete
            if (str(entry.get("task_id") or "") if isinstance(entry, dict) else str(entry)) not in overrideable
            or (isinstance(entry, dict) and entry.get("reason") == "dependency_semantic_conflict")
        ]
        missing_artifacts = [dep for dep in missing_artifacts if str(dep) not in overrideable]
    ready = not missing and not incomplete and not missing_artifacts
    return {
        "ready": ready,
        "depends_on": depends_on,
        "requires_artifacts_from": requires_artifacts_from,
        "allow_partial_dependencies": sorted(allow_partial_dependencies),
        "allow_partial_artifacts_from": sorted(allow_partial_artifacts_from),
        "input_requirements": input_requirement_state,
        "missing": missing,
        "incomplete": incomplete,
        "missing_artifacts": missing_artifacts,
        "soft_dependencies": soft_dependencies,
        "soft_missing_artifacts": soft_missing_artifacts,
    }


def _prepare_worktree(task_id: str) -> Path:
    target = worktree_dir(task_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def queue() -> List[Dict[str, object]]:
    ensure_orchestrator_dirs()
    items: List[Dict[str, object]] = []
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    registry = load_registry()
    records = registry.get("tasks", {}) if isinstance(registry, dict) else {}
    for task_path in sorted((root() / "tasks" / "pending").glob("*/task.md")):
        task = load_task(task_path)
        deps = dependency_state(task)
        record = records.get(task.task_id, {}) if isinstance(records.get(task.task_id), dict) else {}
        lane = str(record.get("queue_lane") or "normal")
        lane_rank = 0 if deps["ready"] and lane == "ready_front" else (1 if deps["ready"] else 2)
        items.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "priority": task.priority,
                "worker": task.worker,
                "created_at": task.created_at,
                "path": str(task_path),
                "ready": deps["ready"],
                "dependency_state": deps,
                "queue_lane": lane,
                "ready_reason": record.get("ready_reason"),
                "input_bindings": record.get("input_bindings") or {},
                "unblock_boost": record.get("unblock_boost", 0),
                "_lane_rank": lane_rank,
                "_rank": priority_rank.get(task.priority, 2),
            }
        )
    items.sort(key=lambda item: (item["_lane_rank"], item["_rank"], -int(item.get("unblock_boost") or 0), str(item["created_at"]), str(item["task_id"])))
    for item in items:
        item.pop("_rank", None)
        item.pop("_lane_rank", None)
    return items


def dag_plan(
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    from .dag_planner import dag_plan as build_dag_plan

    return build_dag_plan(line=line, batch=batch, dag_root=dag_root, prefix=prefix)


def _blocked_diagnosis_path() -> Path:
    return state_dir() / "blocked_diagnosis.json"


def _candidate_unblock_actions_path() -> Path:
    return state_dir() / "candidate_unblock_actions.json"


def _write_reconcile_state(report: Dict[str, object]) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    _blocked_diagnosis_path().write_text(json.dumps(report.get("blocked_diagnosis", []), indent=2, ensure_ascii=False), encoding="utf-8")
    _candidate_unblock_actions_path().write_text(json.dumps(report.get("candidate_actions", []), indent=2, ensure_ascii=False), encoding="utf-8")


def _input_bindings_from_dependency_state(deps: Dict[str, object]) -> Dict[str, object]:
    bindings: Dict[str, object] = {}
    for state in deps.get("input_requirements", []) or []:
        if not isinstance(state, dict) or not state.get("ready"):
            continue
        from_task = str(state.get("from_task") or "")
        binding = state.get("binding")
        if from_task and isinstance(binding, dict):
            bindings[from_task] = binding
    return bindings


def _soft_dependency_candidate_actions(task: TaskBook, deps: Dict[str, object]) -> List[Dict[str, object]]:
    actions: List[Dict[str, object]] = []
    soft_items = list(deps.get("soft_dependencies", []) or []) + list(deps.get("soft_missing_artifacts", []) or [])
    seen: set[str] = set()
    for item in soft_items:
        if not isinstance(item, dict):
            continue
        parent_id = str(item.get("task_id") or "")
        if not parent_id or parent_id in seen:
            continue
        seen.add(parent_id)
        parent_status = str(item.get("status") or "")
        reason = str(item.get("reason") or "")
        actions.append(
            {
                "candidate_id": f"suggest_followup_probe__{task.task_id}__{parent_id}",
                "action_type": "suggest_followup_probe",
                "task_id": task.task_id,
                "parent_task_id": parent_id,
                "safe_level": "diagnostic",
                "requires_user_approval": True,
                "reason": "A parent branch is incomplete, but this analysis/report task can continue and should propose a bounded follow-up instead of stalling the batch.",
                "parent_status": parent_status,
                "block_reason": reason,
                "suggested_actions": [
                    "summarize the incomplete evidence in the current report",
                    "if execution succeeded but evidence is weak, propose a bounded exploratory probe",
                    "if evidence logging is missing, propose a logging/collector fix before scale-up",
                ],
            }
        )
    return actions


def dependency_reconcile(task_id: Optional[str] = None, apply_safe: bool = False) -> Dict[str, object]:
    """Re-check artifact dependencies and promote safe tasks to ready_front.

    This only unblocks tasks whose task book explicitly declares input_requirements.
    It never marks partial parents completed and never reruns GPU work.
    """
    ensure_orchestrator_dirs()
    task_paths = []
    if task_id:
        try:
            _, path = find_task(task_id)
            if "pending" in path.parts:
                task_paths.append(path)
        except FileNotFoundError:
            task_paths = []
    else:
        task_paths = sorted((root() / "tasks" / "pending").glob("*/task.md"))

    promoted: List[Dict[str, object]] = []
    newly_unblockable: List[Dict[str, object]] = []
    needs_user_decision: List[Dict[str, object]] = []
    still_blocked: List[Dict[str, object]] = []
    blocked_diagnosis: List[Dict[str, object]] = []
    candidate_actions: List[Dict[str, object]] = []

    for path in task_paths:
        task = load_task(path)
        if not (task.raw_front_matter.get("input_requirements") or []):
            continue
        deps = dependency_state(task)
        input_states = [state for state in deps.get("input_requirements", []) or [] if isinstance(state, dict)]
        for state in input_states:
            for action in state.get("candidate_actions", []) or []:
                if isinstance(action, dict):
                    enriched = dict(action)
                    enriched.setdefault("task_id", task.task_id)
                    enriched.setdefault("candidate_id", f"{enriched.get('action_type', 'action')}__{task.task_id}")
                    candidate_actions.append(enriched)
        bindings = _input_bindings_from_dependency_state(deps)
        diagnosis = {
            "task_id": task.task_id,
            "title": task.title,
            "ready": deps.get("ready"),
            "input_requirements": input_states,
            "missing": deps.get("missing", []),
            "incomplete": deps.get("incomplete", []),
            "missing_artifacts": deps.get("missing_artifacts", []),
            "soft_dependencies": deps.get("soft_dependencies", []),
            "soft_missing_artifacts": deps.get("soft_missing_artifacts", []),
            "candidate_actions": [action for action in candidate_actions if action.get("task_id") == task.task_id],
        }
        for action in _soft_dependency_candidate_actions(task, deps):
            candidate_actions.append(action)
            diagnosis["candidate_actions"].append(action)
        if deps.get("ready") and bindings:
            row = {"task_id": task.task_id, "bindings": bindings, "reason": "dependency_reconciled"}
            newly_unblockable.append(row)
            if apply_safe:
                existing = load_registry().get("tasks", {}).get(task.task_id, {})
                existing_bindings = existing.get("input_bindings") if isinstance(existing, dict) else {}
                merged_bindings = dict(existing_bindings or {})
                merged_bindings.update(bindings)
                partial_used = any(
                    isinstance(binding, dict) and str(binding.get("source_status") or "") == "partial"
                    for binding in bindings.values()
                )
                ready_reason = "dependency_reconciled_from_partial" if partial_used else "dependency_reconciled_from_success"
                record = update_task_record(
                    task.task_id,
                    queue_lane="ready_front",
                    ready_reason=ready_reason,
                    input_bindings=merged_bindings,
                    dependency_diagnosis=diagnosis,
                    was_blocked=True,
                    unblocked_at=now_iso(),
                    unblock_boost=max(int(existing.get("unblock_boost") or 0) if isinstance(existing, dict) else 0, 60),
                )
                promoted.append({"task_id": task.task_id, "ready_reason": ready_reason, "input_bindings": merged_bindings, "record_status": record.get("status")})
        else:
            blocked_diagnosis.append(diagnosis)
            if any(str(state.get("reason") or "").endswith("needs_user_decision") or str(state.get("reason") or "") == "dependency_partial_needs_user_decision" for state in input_states):
                needs_user_decision.append(diagnosis)
            else:
                still_blocked.append(diagnosis)
            if apply_safe:
                update_task_record(task.task_id, dependency_diagnosis=diagnosis)

    report = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "apply_safe": apply_safe,
        "task_id": task_id,
        "newly_unblockable": newly_unblockable,
        "promoted": promoted,
        "needs_user_decision": needs_user_decision,
        "still_blocked": still_blocked,
        "blocked_diagnosis": blocked_diagnosis,
        "candidate_actions": candidate_actions,
        "blocked_diagnosis_path": str(_blocked_diagnosis_path()),
        "candidate_actions_path": str(_candidate_unblock_actions_path()),
    }
    _write_reconcile_state(report)
    return report


def bind_input(task_id: str, from_task: str, run_id: str) -> Dict[str, object]:
    _, child_path = find_task(task_id)
    child = load_task(child_path)
    registry = load_registry()
    records = registry.get("tasks", {}) if isinstance(registry, dict) else {}
    parent = records.get(from_task)
    if not isinstance(parent, dict):
        raise FileNotFoundError(f"parent task not found in registry: {from_task}")
    run = _find_run_record(parent, run_id)
    if not run:
        raise FileNotFoundError(f"run not found for {from_task}: {run_id}")
    matching_req: Dict[str, object] = {
        "from_task": from_task,
        "resolve_strategy": "pinned_run",
        "run_id": run_id,
        "acceptable_status": ["success", "partial"],
        "partial_policy": "auto",
        "required_artifacts": _default_input_artifacts(),
    }
    for requirement in child.raw_front_matter.get("input_requirements", []) or []:
        if isinstance(requirement, dict) and str(requirement.get("from_task") or requirement.get("task_id") or "") == from_task:
            matching_req.update(requirement)
            matching_req["resolve_strategy"] = "pinned_run"
            matching_req["run_id"] = run_id
            matching_req["partial_policy"] = "auto"
            break
    state = _input_requirement_diagnosis(matching_req, records)
    if not state.get("ready") or not isinstance(state.get("binding"), dict):
        raise ValueError(f"input binding is not valid: {state}")
    existing = records.get(task_id, {}) if isinstance(records.get(task_id), dict) else {}
    bindings = dict(existing.get("input_bindings") or {})
    bindings[from_task] = state["binding"]
    record = update_task_record(
        task_id,
        queue_lane="ready_front",
        ready_reason="manual_input_binding",
        input_bindings=bindings,
        was_blocked=True,
        unblocked_at=now_iso(),
        unblock_boost=max(int(existing.get("unblock_boost") or 0), 60),
    )
    return {"task_id": task_id, "from_task": from_task, "run_id": run_id, "binding": state["binding"], "record": record}


def _task_resource_hint(task_id: str) -> str:
    try:
        _, path = find_task(task_id)
        task = load_task(path)
        return str(task.raw_front_matter.get("resource_hint") or "any").lower()
    except FileNotFoundError:
        return "any"


def _latest_run_status(record: Dict[str, object]) -> str:
    latest_run_id = str(record.get("latest_run_id") or "")
    for run in reversed(record.get("runs", []) or []):
        if not isinstance(run, dict):
            continue
        if latest_run_id and str(run.get("run_id") or "") != latest_run_id:
            continue
        return str(run.get("status") or "")
    return str(record.get("status") or "")


def _is_record_actively_running(record: Dict[str, object]) -> bool:
    return str(record.get("status") or "") == "running" and _latest_run_status(record) == "running"


def _task_execution_stage(task: TaskBook) -> str:
    return str(task.raw_front_matter.get("execution_stage") or "").strip().lower()


def _task_code_change_scope(task: TaskBook) -> str:
    raw = task.raw_front_matter
    if raw.get("allow_code_changes") is not True:
        return "none"
    return str(raw.get("code_change_scope") or "source").strip().lower()


def _is_source_code_change_scope(code_scope: str) -> bool:
    return code_scope not in {"none", "config", "config_only", "configs", "artifacts_only"}


def _task_prepare_run_risk(task: TaskBook, hint: str, code_scope: str) -> List[str]:
    """Flag tasks that mix source edits and GPU execution in one worker run."""
    raw = task.raw_front_matter
    if hint != "gpu":
        return []
    if not _is_source_code_change_scope(code_scope):
        return []
    if raw.get("code_and_run_together") is True:
        return []
    return ["code_and_run_should_split"]


def _running_source_change_task_ids() -> List[str]:
    tasks = load_registry().get("tasks", {})
    running: List[str] = []
    for task_id, record in tasks.items():
        if not isinstance(record, dict) or not _is_record_actively_running(record):
            continue
        try:
            _, path = find_task(str(task_id))
            task = load_task(path)
        except FileNotFoundError:
            continue
        scope = _task_code_change_scope(task)
        if _is_source_code_change_scope(scope):
            running.append(str(task_id))
    return running


def _has_ready_prepare_task(
    *,
    worker: Optional[str] = None,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> bool:
    for item in queue():
        if not item.get("ready"):
            continue
        try:
            _, path = find_task(str(item["task_id"]))
            task = load_task(path)
        except FileNotFoundError:
            continue
        if worker and _normalize_selector_value(worker) not in _task_allowed_workers(task):
            continue
        scope_matches, _ = _task_matches_scope_filters(task, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
        if not scope_matches:
            continue
        if _task_execution_stage(task) == "prepare":
            return True
    return False


def _running_capacity() -> Dict[str, int]:
    tasks = load_registry().get("tasks", {})
    total = 0
    gpu = 0
    cpu = 0
    for task_id, record in tasks.items():
        if not isinstance(record, dict) or not _is_record_actively_running(record):
            continue
        total += 1
        hint = _task_resource_hint(str(task_id))
        if hint == "gpu":
            gpu += 1
        elif hint == "cpu":
            cpu += 1
    return {"total": total, "gpu": gpu, "cpu": cpu}


def _running_gpu_ids() -> List[str]:
    ids: List[str] = []
    tasks = load_registry().get("tasks", {})
    for record in tasks.values():
        if not isinstance(record, dict) or not _is_record_actively_running(record):
            continue
        remote = record.get("remote") or {}
        if isinstance(remote, dict):
            gpu_candidates = remote.get("gpu_candidates")
            if gpu_candidates:
                ids.extend([item.strip() for item in str(gpu_candidates).split(",") if item.strip()])
        for run in record.get("runs", []) or []:
            if not isinstance(run, dict) or run.get("status") != "running":
                continue
            gpu = run.get("gpu") or {}
            if isinstance(gpu, dict):
                gpu_ids = gpu.get("requested_nvidia_smi_ids") or gpu.get("requested_physical_ids")
                if isinstance(gpu_ids, list):
                    ids.extend(str(item) for item in gpu_ids if item is not None)
                elif gpu_ids:
                    ids.extend([item.strip() for item in str(gpu_ids).split(",") if item.strip()])
    return sorted(set(ids))


def _running_gpu_counts(host: Optional[str] = None) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    tasks = load_registry().get("tasks", {})
    for record in tasks.values():
        if not isinstance(record, dict) or not _is_record_actively_running(record):
            continue
        seen_for_task: List[str] = []
        remote = record.get("remote") or {}
        if host and isinstance(remote, dict) and str(remote.get("host") or "") and str(remote.get("host") or "") != str(host):
            continue
        if isinstance(remote, dict):
            gpu_candidates = remote.get("gpu_candidates")
            if gpu_candidates:
                seen_for_task.extend([item.strip() for item in str(gpu_candidates).split(",") if item.strip()])
        for run in record.get("runs", []) or []:
            if not isinstance(run, dict) or run.get("status") != "running":
                continue
            gpu = run.get("gpu") or {}
            if isinstance(gpu, dict):
                gpu_ids = gpu.get("requested_nvidia_smi_ids") or gpu.get("requested_physical_ids")
                if isinstance(gpu_ids, list):
                    seen_for_task.extend(str(item) for item in gpu_ids if item is not None)
                elif gpu_ids:
                    seen_for_task.extend([item.strip() for item in str(gpu_ids).split(",") if item.strip()])
        for gpu_id in sorted(set(seen_for_task)):
            counts[gpu_id] = counts.get(gpu_id, 0) + 1
    return counts


def _gpu_snapshot_cache_path(host: str, allowed_gpu_ids: Optional[List[str]]) -> Path:
    allowed_key = ",".join(sorted(str(item) for item in (allowed_gpu_ids or [])))
    digest = hashlib.sha1(f"{host}|{allowed_key}".encode("utf-8")).hexdigest()[:16]
    return state_dir() / "remote_gpu_cache" / f"{digest}.json"


def _read_gpu_snapshot_cache(host: str, allowed_gpu_ids: Optional[List[str]], max_age_seconds: int) -> Optional[Dict[str, object]]:
    if max_age_seconds <= 0:
        return None
    path = _gpu_snapshot_cache_path(host, allowed_gpu_ids)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        payload["cache"] = {"hit": True, "age_seconds": int(age), "path": str(path)}
        return payload
    return None


def _write_gpu_snapshot_cache(host: str, allowed_gpu_ids: Optional[List[str]], payload: Dict[str, object]) -> None:
    path = _gpu_snapshot_cache_path(host, allowed_gpu_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = dict(payload)
    serializable["cache"] = {"hit": False, "stored_at": now_iso(), "path": str(path)}
    path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")


def _gpu_snapshot_command() -> str:
    return (
        "if ! command -v nvidia-smi >/dev/null 2>&1; then echo 'NO_NVIDIA_SMI'; exit 0; fi; "
        "echo '__WHOAMI__'; whoami; "
        "echo '__GPU__'; nvidia-smi --query-gpu=index,uuid,memory.used,memory.total,memory.free,utilization.gpu,name --format=csv,noheader,nounits; "
        "echo '__APPS__'; "
        "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null | "
        "while IFS=, read -r uuid pid mem; do "
        "uuid=$(echo \"$uuid\" | xargs); pid=$(echo \"$pid\" | xargs); mem=$(echo \"$mem\" | xargs); "
        "user=$(ps -o user= -p \"$pid\" 2>/dev/null | xargs); "
        "printf '%s,%s,%s,%s\\n' \"$uuid\" \"$pid\" \"$mem\" \"$user\"; "
        "done"
    )


def _parse_gpu_snapshot_payload(host: str, allowed_gpu_ids: Optional[List[str]], stdout: str, raw: Dict[str, object]) -> Dict[str, object]:
    allowed = set(str(item) for item in (allowed_gpu_ids or []))
    gpus: List[Dict[str, object]] = []
    current_user = ""
    apps_by_uuid: Dict[str, List[Dict[str, object]]] = {}
    section = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "NO_NVIDIA_SMI" or stripped.startswith("__FRAME_"):
            continue
        if stripped in {"__WHOAMI__", "__GPU__", "__APPS__"}:
            section = stripped
            continue
        if section == "__WHOAMI__":
            current_user = stripped
        elif section == "__APPS__":
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            uuid, pid, mem, user = parts[:4]
            apps_by_uuid.setdefault(uuid, []).append({"pid": pid, "used_memory_mb": mem, "user": user})
        elif section == "__GPU__":
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 7:
                continue
            index, uuid, used, total, free, util, name = parts[:7]
            if allowed and index not in allowed:
                continue
            try:
                used_mb = float(used)
                total_mb = float(total)
                free_mb = float(free)
                util_pct = float(util)
            except ValueError:
                continue
            gpus.append(
                {
                    "index": index,
                    "uuid": uuid,
                    "name": name,
                    "used_mb": used_mb,
                    "total_mb": total_mb,
                    "free_mb": free_mb,
                    "free_gb": round(free_mb / 1024, 2),
                    "utilization_pct": util_pct,
                }
            )
    for gpu in gpus:
        apps = apps_by_uuid.get(str(gpu["uuid"]), [])
        external_apps = [app for app in apps if current_user and app.get("user") not in {"", current_user}]
        own_apps = [app for app in apps if not current_user or app.get("user") == current_user]
        gpu["compute_apps"] = apps
        gpu["own_compute_app_count"] = len(own_apps)
        gpu["external_compute_app_count"] = len(external_apps)
        gpu["has_external_compute_apps"] = bool(external_apps)
    gpus.sort(key=lambda item: (float(item["free_gb"]) * -1, float(item["utilization_pct"])))
    return {"host": host, "remote_user": current_user, "allowed_gpu_ids": sorted(allowed), "gpus": gpus, "raw": raw, "cache": {"hit": False, "stored_at": now_iso()}}


def remote_gpu_snapshot(
    host: str,
    allowed_gpu_ids: Optional[List[str]] = None,
    max_age_seconds: int = 0,
    force_refresh: bool = False,
    cache_only: bool = False,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, object]:
    allowed = set(str(item) for item in (allowed_gpu_ids or []))
    if not force_refresh:
        cached = _read_gpu_snapshot_cache(host, allowed_gpu_ids, max_age_seconds)
        if cached is not None:
            return cached
    if cache_only:
        return {
            "host": host,
            "allowed_gpu_ids": sorted(allowed),
            "gpus": [],
            "cache": {"hit": False, "missing": True, "cache_only": True, "checked_at": now_iso()},
        }
    command = _gpu_snapshot_command()
    result = run_ssh(host, command, timeout=timeout_seconds)
    payload = _parse_gpu_snapshot_payload(host, allowed_gpu_ids, str(result.get("stdout", "")), result)
    if int(result.get("returncode", 1) or 0) == 0:
        _write_gpu_snapshot_cache(host, allowed_gpu_ids, payload)
    elif max_age_seconds > 0:
        stale_path = _gpu_snapshot_cache_path(host, allowed_gpu_ids)
        if stale_path.exists():
            try:
                stale = json.loads(stale_path.read_text(encoding="utf-8"))
                if isinstance(stale, dict):
                    stale["cache"] = {
                        "hit": True,
                        "stale": True,
                        "age_seconds": int(time.time() - stale_path.stat().st_mtime),
                        "path": str(stale_path),
                        "refresh_error": result.get("stderr") or result.get("stdout"),
                    }
                    return stale
            except Exception:
                pass
    return payload


def _gpu_snapshot_stream_command(interval_seconds: int) -> str:
    interval = max(5, int(interval_seconds))
    command = _gpu_snapshot_command()
    return f'while true; do echo "__FRAME_START__"; date -Is; ({command}); echo "__FRAME_END__"; sleep {interval}; done'


def remote_watch_loop(
    host: str,
    allowed_gpu_ids: Optional[List[str]] = None,
    interval_seconds: int = 30,
    cooldown_seconds: int = 600,
    max_failures: int = 3,
    max_cycles: int = 0,
) -> Dict[str, object]:
    lock = _acquire_remote_watch_lock("remote-watch-loop")
    if not lock.get("acquired"):
        return {"started": False, "reason": "remote watcher already running", "lock": lock}
    failures = 0
    frames = 0
    command = _gpu_snapshot_stream_command(interval_seconds)
    try:
        while True:
            _refresh_remote_watch_lock()
            _write_remote_watch_state(
                {
                    "state": "connecting",
                    "host": host,
                    "allowed_gpu_ids": allowed_gpu_ids or [],
                    "interval_seconds": interval_seconds,
                    "failures": failures,
                    "updated_at": now_iso(),
                }
            )
            try:
                process = open_ssh_stream(host, command)
            except Exception as exc:
                failures += 1
                _write_remote_watch_state(
                    {
                        "state": "connect_failed",
                        "host": host,
                        "error": str(exc),
                        "failures": failures,
                        "updated_at": now_iso(),
                    }
                )
                process = None
            if process is not None and process.stdout is not None:
                frame_lines: List[str] = []
                in_frame = False
                for raw_line in process.stdout:
                    _refresh_remote_watch_lock()
                    line = decode_output(raw_line)
                    stripped = line.rstrip("\n")
                    if stripped == "__FRAME_START__":
                        frame_lines = []
                        in_frame = True
                        continue
                    if stripped == "__FRAME_END__" and in_frame:
                        stdout = "\n".join(frame_lines)
                        raw = {"returncode": 0, "stdout": stdout, "stderr": "", "command": "ssh stream gpu snapshot", "host": host}
                        payload = _parse_gpu_snapshot_payload(host, allowed_gpu_ids, stdout, raw)
                        _write_gpu_snapshot_cache(host, allowed_gpu_ids, payload)
                        frames += 1
                        failures = 0
                        _write_remote_watch_state(
                            {
                                "state": "streaming",
                                "host": host,
                                "allowed_gpu_ids": allowed_gpu_ids or [],
                                "interval_seconds": interval_seconds,
                                "frames": frames,
                                "last_success_at": now_iso(),
                                "gpu_count": len(payload.get("gpus", []) or []),
                                "failures": failures,
                                "pid": process.pid,
                            }
                        )
                        if max_cycles and frames >= max_cycles:
                            process.terminate()
                            return {"finished": True, "frames": frames, "lock": lock}
                        in_frame = False
                        continue
                    if in_frame:
                        frame_lines.append(stripped)
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    returncode = 124
                failures += 1
                _write_remote_watch_state(
                    {
                        "state": "stream_ended",
                        "host": host,
                        "returncode": returncode,
                        "failures": failures,
                        "updated_at": now_iso(),
                    }
                )
            if max_failures > 0 and failures >= max_failures:
                cooldown_until = time.time() + max(30, cooldown_seconds)
                _write_remote_watch_state(
                    {
                        "state": "cooldown",
                        "host": host,
                        "failures": failures,
                        "cooldown_until_epoch": cooldown_until,
                        "cooldown_seconds": cooldown_seconds,
                        "updated_at": now_iso(),
                    }
                )
                while time.time() < cooldown_until:
                    _refresh_remote_watch_lock()
                    time.sleep(min(60, max(1, cooldown_until - time.time())))
                failures = 0
            else:
                time.sleep(min(30, max(5, interval_seconds)))
    finally:
        _release_remote_watch_lock()


def remote_watch_start(
    host: str,
    allowed_gpu_ids: str = "0,1,2,4",
    interval_seconds: int = 30,
    cooldown_seconds: int = 600,
    max_failures: int = 3,
) -> Dict[str, object]:
    current = remote_watch_status()
    if current.get("running"):
        return {"started": False, "reason": "remote watcher already running", "status": current}
    log_dir = state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "remote_watch_stdout.log"
    stderr_path = log_dir / "remote_watch_stderr.log"
    cli_path = root() / "cli.py"
    command = [
        sys.executable,
        str(cli_path),
        "remote-watch-loop",
        "--host",
        host,
        "--allowed-gpu-ids",
        allowed_gpu_ids,
        "--interval-seconds",
        str(interval_seconds),
        "--cooldown-seconds",
        str(cooldown_seconds),
        "--max-failures",
        str(max_failures),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(root()),
            stdout=stdout,
            stderr=stderr,
            env=env,
            stdin=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(new_process_group=True),
        )
    return {"started": True, "pid": process.pid, "command": command, "stdout": str(stdout_path), "stderr": str(stderr_path)}


def _task_allowed_gpu_ids(task: TaskBook, default_allowed_gpu_ids: Optional[List[str]]) -> List[str]:
    task_ids = _gpu_candidates(task)
    if task_ids:
        return task_ids
    return [str(item) for item in (default_allowed_gpu_ids or [])]


def _task_required_free_gpu_mem_gb(task: TaskBook, default_min_free_gpu_mem_gb: float) -> float:
    raw = task.raw_front_matter
    return float(raw.get("min_free_gpu_mem_gb", default_min_free_gpu_mem_gb) or default_min_free_gpu_mem_gb)


def _normalize_workplace_value(value: object) -> str:
    return str(value or "").strip().rstrip("/\\")


def _task_target_workplace(task: TaskBook) -> Dict[str, str]:
    raw = task.raw_front_matter or {}
    target = raw.get("target_workplace")
    if target is None:
        target = raw.get("workplace")
    if not isinstance(target, dict):
        return {}
    return {
        "host": _normalize_workplace_value(target.get("host")),
        "remote_root": _normalize_workplace_value(target.get("remote_root") or target.get("root")),
        "remote_workdir": _normalize_workplace_value(target.get("remote_workdir") or target.get("workdir")),
    }


def _target_workplace_mismatch(task: TaskBook, *, host: str, remote_root: str, remote_workdir: Optional[str]) -> Optional[Dict[str, object]]:
    target = _task_target_workplace(task)
    if not target:
        return None
    active = {
        "host": _normalize_workplace_value(host),
        "remote_root": _normalize_workplace_value(remote_root),
        "remote_workdir": _normalize_workplace_value(remote_workdir),
    }
    mismatches = []
    for key, expected in target.items():
        if not expected:
            continue
        if active.get(key) != expected:
            mismatches.append({"field": key, "expected": expected, "active": active.get(key) or ""})
    if not mismatches:
        return None
    return {
        "task_id": task.task_id,
        "reason": "target_workplace_mismatch",
        "target_workplace": target,
        "active_workplace": active,
        "mismatches": mismatches,
    }


def _normalize_selector_value(value: object) -> str:
    return str(value or "").strip()


def _task_declared_worker(task: TaskBook) -> str:
    return _normalize_selector_value(getattr(task, "worker", "") or (task.raw_front_matter or {}).get("worker") or "codex")


def _task_allowed_workers(task: TaskBook) -> set[str]:
    raw = task.raw_front_matter or {}
    declared = _task_declared_worker(task)
    allowed = raw.get("allowed_workers")
    if allowed is None:
        return {declared}
    if isinstance(allowed, str):
        values = [allowed]
    elif isinstance(allowed, (list, tuple, set)):
        values = list(allowed)
    else:
        values = []
    normalized = {_normalize_selector_value(item) for item in values if _normalize_selector_value(item)}
    return normalized or {declared}


def _assert_worker_admitted(
    task: TaskBook,
    requested_worker: str,
    *,
    force_worker_override: bool = False,
    override_reason: str = "",
) -> None:
    requested = _normalize_selector_value(requested_worker)
    declared = _task_declared_worker(task)
    allowed = _task_allowed_workers(task)
    if requested in allowed:
        return
    if force_worker_override and _normalize_selector_value(override_reason):
        return
    if force_worker_override:
        raise ValueError(
            "worker override requires --reason: "
            f"task.required_worker={declared} requested_worker={requested}"
        )
    raise ValueError(
        "worker mismatch: "
        f"task.required_worker={declared} requested_worker={requested}; "
        "use --force-worker-override --reason \"...\" if this is intentional"
    )


def _record_worker_override_event(
    task: TaskBook,
    requested_worker: str,
    reason: str,
    *,
    declared_worker: Optional[str] = None,
    allowed_workers: Optional[set[str]] = None,
) -> None:
    declared = declared_worker or _task_declared_worker(task)
    allowed = allowed_workers or _task_allowed_workers(task)
    event = {
        "ts": now_iso(),
        "event": "worker_override",
        "task_id": task.task_id,
        "declared_worker": declared,
        "allowed_workers": sorted(allowed),
        "requested_worker": _normalize_selector_value(requested_worker),
        "reason": _normalize_selector_value(reason),
        "approved_by": "user",
    }
    journal = registry_journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    history = list((load_registry().get("tasks", {}).get(task.task_id, {}) or {}).get("worker_overrides", []) or [])
    history.append(event)
    update_task_record(task.task_id, worker_overrides=history)


def _task_scope_value(task: TaskBook, key: str) -> str:
    raw = task.raw_front_matter or {}
    aliases = {
        "line": ("line", "task_line"),
        "batch": ("batch", "batch_id", "task_batch"),
        "dag_root": ("dag_root", "dag-root", "root_task", "task_root", "root", "dag"),
    }
    for name in aliases.get(key, (key,)):
        if _normalize_selector_value(raw.get(name)):
            return _normalize_selector_value(raw.get(name))
    tags = raw.get("tags")
    if isinstance(tags, dict):
        for name in aliases.get(key, (key,)):
            if _normalize_selector_value(tags.get(name)):
                return _normalize_selector_value(tags.get(name))
    return ""


def _task_continuation_spec(task: TaskBook) -> Optional[Dict[str, object]]:
    raw = task.raw_front_matter or {}
    continuation = raw.get("continuation")
    if isinstance(continuation, dict):
        return continuation
    return None


def _render_continuation_front_matter(parent_task: TaskBook, emit_task: Dict[str, object]) -> str:
    parent_raw = parent_task.raw_front_matter or {}
    task_id = _normalize_selector_value(emit_task.get("task_id") or emit_task.get("task") or "")
    title = str(emit_task.get("title") or parent_task.title)
    worker = _normalize_selector_value(emit_task.get("worker") or parent_task.worker)
    priority = str(emit_task.get("priority") or "normal")
    line = _normalize_selector_value(emit_task.get("line") or parent_raw.get("line"))
    batch = _normalize_selector_value(emit_task.get("batch") or parent_raw.get("batch"))
    dag_root = _normalize_selector_value(emit_task.get("dag_root") or parent_raw.get("dag_root"))
    prefix = _normalize_selector_value(emit_task.get("prefix") or parent_raw.get("prefix"))
    resource_hint = _normalize_selector_value(emit_task.get("resource_hint") or parent_raw.get("resource_hint") or "cpu")
    expected_outputs = [str(item) for item in (emit_task.get("expected_outputs") or emit_task.get("outputs") or []) or []]
    front_matter: Dict[str, object] = {
        "task_id": task_id,
        "title": title,
        "priority": priority,
        "worker": worker,
        "status": "pending",
        "created_at": now_iso(),
        "continuation_from_task": parent_task.task_id,
        "line": line or None,
        "batch": batch or None,
        "dag_root": dag_root or None,
        "prefix": prefix or None,
        "resource_hint": resource_hint or "cpu",
        "resource_lane": parent_raw.get("resource_lane"),
        "intent": emit_task.get("intent"),
    }
    if expected_outputs:
        front_matter["expected_outputs"] = expected_outputs
    if "allowed_workers" in emit_task:
        front_matter["allowed_workers"] = emit_task["allowed_workers"]
    if "task_group" in parent_raw:
        front_matter["task_group"] = parent_raw["task_group"]
    front_matter["objective"] = f"Continuation emitted from {parent_task.task_id}"
    front_matter["success_criteria"] = [f"Complete continuation from {parent_task.task_id}"]
    compact = {key: value for key, value in front_matter.items() if value not in (None, "", [])}
    compact_yaml = yaml.safe_dump(compact, sort_keys=False, allow_unicode=True).strip()
    return f"""---
{compact_yaml}
---

# Continuation

This task is auto-emitted by collect-time continuation chaining.
"""


def _emit_continuation_task(task: TaskBook, payload: Dict[str, object], result_status: str) -> Dict[str, object]:
    continuation = _task_continuation_spec(task) or {}
    when = str(continuation.get("when") or "success").strip().lower() or "success"
    if when not in {"success", "partial", "failed", "always"}:
        return {
            "emitted": False,
            "reason": "unsupported_continuation_when",
            "when": when,
            "allowed_when": ["success", "partial", "failed", "always"],
        }
    if when != "always" and result_status != when:
        return {"emitted": False, "reason": "continuation_condition_mismatch", "expected": when, "actual": result_status}
    emit = continuation.get("emit_task")
    if not isinstance(emit, dict):
        return {"emitted": False, "reason": "emit_task_missing_or_invalid", "continuation": continuation}
    continuation_task_id = _normalize_selector_value(emit.get("task_id") or emit.get("task") or emit.get("id"))
    if not continuation_task_id:
        action_request_payload = {
            "source_task_id": task.task_id,
            "result_status": result_status,
            "continuation_reason": "emit_task lacks task_id",
            "continuation": continuation,
        }
        existing = _find_action_request(
            "continuation_waiting",
            task_id=task.task_id,
            payload_match=action_request_payload,
        )
        if existing:
            return {
                "emitted": False,
                "reason": "continuation_emit_task_id_missing",
                "action_request": existing,
            }
        action_request = create_action_request(
            "continuation_waiting",
            task_id=task.task_id,
            payload=action_request_payload,
            reason="continuation chain missing emit_task.task_id",
            actor="system",
            source="collector",
            safe_level="diagnostic",
            expected_task_status="completed",
            requires_scheduler_validation=False,
        )
        return {"emitted": False, "reason": "continuation_emit_task_id_missing", "action_request": action_request}
    existing_status = _known_task_status(continuation_task_id)
    if existing_status:
        action_request_payload = {
            "source_task_id": task.task_id,
            "continuation_task_id": continuation_task_id,
            "continuation_task_status": existing_status,
            "result_status": result_status,
        }
        existing = _find_action_request(
            "continuation_waiting",
            task_id=task.task_id,
            payload_match=action_request_payload,
        )
        if existing:
            return {
                "emitted": False,
                "reason": "continuation_target_exists",
                "target_task_id": continuation_task_id,
                "target_status": existing_status,
                "action_request": existing,
            }
        action_request = create_action_request(
            "continuation_waiting",
            task_id=task.task_id,
            payload=action_request_payload,
            reason=f"continuation target already exists with status {existing_status}",
            actor="system",
            source="collector",
            safe_level="diagnostic",
            expected_task_status="completed",
            requires_scheduler_validation=False,
        )
        return {
            "emitted": False,
            "reason": "continuation_target_exists",
            "target_task_id": continuation_task_id,
            "target_status": existing_status,
            "action_request": action_request,
        }

    target_path = task_file("pending", continuation_task_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_content = _render_continuation_front_matter(task, emit)
    target_path.write_text(target_content, encoding="utf-8")
    continuation_task = load_task(target_path)
    upsert_task(continuation_task, status="pending")
    return {"emitted": True, "task_id": continuation_task_id, "status": "pending", "path": str(target_path)}


def _task_matches_scope_filters(
    task: TaskBook,
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> tuple[bool, Optional[Dict[str, object]]]:
    filters = {
        "line": _normalize_selector_value(line),
        "batch": _normalize_selector_value(batch),
        "dag_root": _normalize_selector_value(dag_root),
    }
    for key, expected in filters.items():
        if not expected:
            continue
        actual = _task_scope_value(task, key)
        if actual != expected:
            return False, {"task_id": task.task_id, "reason": f"{key}_scope_mismatch", "expected": expected, "actual": actual}
    wanted_prefix = _normalize_selector_value(prefix)
    if wanted_prefix:
        raw = task.raw_front_matter or {}
        task_prefix = _normalize_selector_value(
            raw.get("prefix") or raw.get("task_prefix") or raw.get("task-id-prefix")
        )
        tags = raw.get("tags")
        if not task_prefix and isinstance(tags, dict):
            task_prefix = _normalize_selector_value(tags.get("prefix") or tags.get("task_prefix"))
        if not task.task_id.startswith(wanted_prefix) and task_prefix != wanted_prefix:
            return False, {
                "task_id": task.task_id,
                "reason": "prefix_scope_mismatch",
                "expected_prefix": wanted_prefix,
                "actual_prefix": task_prefix or task.task_id[: len(wanted_prefix)],
            }
    return True, None


def next_pending_task_id(
    worker: Optional[str] = None,
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Optional[str]:
    items = queue()
    for item in items:
        if not item.get("ready"):
            continue
        task_id = str(item["task_id"])
        _, task_path = find_task(task_id)
        task = load_task(task_path)
        matches_scope, _ = _task_matches_scope_filters(task, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
        if not matches_scope:
            continue
        requested_worker = _normalize_selector_value(worker)
        if requested_worker and requested_worker not in _task_allowed_workers(task):
            continue
        return task_id
    return None


def _gpu_schedule_priority(gpu: Dict[str, object], base_count: int = 0, planned_count: int = 0) -> tuple[int, int, float, float, int, str]:
    has_external = bool(gpu.get("has_external_compute_apps"))
    external_count = int(gpu.get("external_compute_app_count") or 0)
    own_process_count = int(gpu.get("own_compute_app_count") or 0)
    current_load = max(0, base_count) + max(0, planned_count) + max(0, own_process_count)
    shared_tier = 1 if has_external else 0
    free_gb = float(gpu.get("free_gb", 0.0) or 0.0)
    util = float(gpu.get("utilization_pct", 0.0) or 0.0)
    return (shared_tier, current_load, -free_gb, util, external_count, str(gpu.get("index") or ""))


def _gpu_has_untracked_compute(gpu: Dict[str, object], tracked_solution_agent_tasks: int = 0) -> bool:
    """Return true when nvidia-smi sees work not accounted for by our registry.

    Shared remote accounts make same-user GPU processes ambiguous: a process
    launched by a remote Codex/Cursor session has the same Unix user as
    solution-agent, but is not owned by the scheduler. Treat those as blocking
    unless the registry already accounts for them as running solution-agent
    tasks on that GPU.
    """
    own_process_count = int(gpu.get("own_compute_app_count") or 0)
    external_process_count = int(gpu.get("external_compute_app_count") or 0)
    return external_process_count > 0 or own_process_count > max(0, tracked_solution_agent_tasks)


def schedule_remote(
    host: str,
    remote_root: str,
    worker: str = "codex",
    remote_workdir: Optional[str] = None,
    max_starts: int = 1,
    max_running_total: int = 2,
    max_running_gpu: int = 1,
    max_running_cpu: int = 2,
    min_free_gpu_mem_gb: float = 10.0,
    allowed_gpu_ids: Optional[List[str]] = None,
    max_tasks_per_gpu: int = 2,
    gpu_snapshot_max_age_seconds: int = 0,
    gpu_snapshot_cache_only: bool = False,
    dry_run: bool = True,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    maintenance = load_maintenance_mode()
    if not dry_run and maintenance.get("enabled") and not maintenance.get("allow_scheduler_launch"):
        return {
            "dry_run": False,
            "blocked": True,
            "reason": "maintenance_mode_disallows_scheduler_launch",
            "maintenance_mode": maintenance,
            "capacity": _running_capacity(),
            "plan": [],
            "started": [],
            "skipped": [],
        }
    drain = load_drain_mode()
    if not dry_run and drain.get("enabled") and not drain.get("allow_launch"):
        return {
            "dry_run": False,
            "blocked": True,
            "reason": "drain_mode_disallows_new_launch",
            "drain_mode": drain,
            "capacity": _running_capacity(),
            "plan": [],
            "started": [],
            "skipped": [],
        }
    dependency_reconcile_report = dependency_reconcile(apply_safe=not dry_run)
    capacity = _running_capacity()
    running_gpu_counts = _running_gpu_counts(host=host)
    gpu_snapshot = remote_gpu_snapshot(
        host,
        allowed_gpu_ids=allowed_gpu_ids,
        max_age_seconds=gpu_snapshot_max_age_seconds,
        force_refresh=bool(not dry_run and gpu_snapshot_max_age_seconds <= 0 and not gpu_snapshot_cache_only),
        cache_only=gpu_snapshot_cache_only,
    )
    gpu_by_id = {str(gpu.get("index")): gpu for gpu in gpu_snapshot.get("gpus", []) if isinstance(gpu, dict)}
    available_gpu_ids: List[str] = []
    for gpu in sorted(gpu_by_id.values(), key=lambda item: _gpu_schedule_priority(item, running_gpu_counts.get(str(item.get("index")), 0), 0)):
        gpu_id = str(gpu["index"])
        if float(gpu.get("free_gb", 0.0)) < min_free_gpu_mem_gb:
            continue
        current_own_tasks = running_gpu_counts.get(gpu_id, 0)
        has_external = _gpu_has_untracked_compute(gpu, current_own_tasks)
        if has_external:
            continue
        limit_for_gpu = 1 if has_external else max_tasks_per_gpu
        if current_own_tasks >= limit_for_gpu:
            continue
        available_gpu_ids.append(gpu_id)
    assigned_gpu_counts: Dict[str, int] = {}
    plan: List[Dict[str, object]] = []
    started: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    ready_prepare_exists = _has_ready_prepare_task(worker=worker, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    running_source_change_tasks = _running_source_change_task_ids()
    planned_source_change_task: Optional[str] = None
    total_slots = max(0, max_running_total - capacity["total"])
    gpu_slots = max(0, max_running_gpu - capacity["gpu"])
    cpu_slots = max(0, max_running_cpu - capacity["cpu"])
    start_budget = min(max_starts, total_slots)

    for item in queue():
        if not item.get("ready"):
            skipped.append({"task_id": item.get("task_id"), "reason": "dependencies_not_ready"})
            continue
        task_id = str(item["task_id"])
        _, task_path = find_task(task_id)
        task = load_task(task_path)
        scope_matches, scope_skip = _task_matches_scope_filters(task, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
        if not scope_matches:
            skipped.append(scope_skip or {"task_id": task_id, "reason": "scope_mismatch"})
            continue
        task_worker = _task_declared_worker(task)
        allowed_workers = _task_allowed_workers(task)
        if _normalize_selector_value(worker) not in allowed_workers:
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": "worker_mismatch",
                    "task_worker": task_worker,
                    "selected_worker": worker,
                }
            )
            continue
        workplace_mismatch = _target_workplace_mismatch(task, host=host, remote_root=remote_root, remote_workdir=remote_workdir)
        if workplace_mismatch:
            skipped.append(workplace_mismatch)
            continue
        if len(plan) >= start_budget:
            reason = "total_capacity_full" if total_slots <= 0 else "start_budget_full"
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": reason,
                    "start_budget": start_budget,
                    "max_starts": max_starts,
                    "running_total": capacity.get("total"),
                    "max_running_total": max_running_total,
                }
            )
            continue
        hint = str(task.raw_front_matter.get("resource_hint") or "any").lower()
        lane_hint = "cpu" if hint == "any" else hint
        if hint not in {"cpu", "gpu", "any"}:
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": "unsupported_resource_hint",
                    "resource_hint": hint,
                }
            )
            continue
        stage = _task_execution_stage(task)
        code_scope = _task_code_change_scope(task)
        is_source_change = _is_source_code_change_scope(code_scope)
        risk_flags = _task_prepare_run_risk(task, hint, code_scope)
        selected_gpu_id = None
        selected_gpu_uuid = None
        required_free_gpu_mem_gb = _task_required_free_gpu_mem_gb(task, min_free_gpu_mem_gb) if hint == "gpu" else None
        if lane_hint == "gpu" and ready_prepare_exists:
            skipped.append({"task_id": task_id, "reason": "prepare_wave_not_complete"})
            continue
        if is_source_change and (running_source_change_tasks or planned_source_change_task):
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": "source_code_change_serialized",
                    "running_source_change_tasks": running_source_change_tasks,
                    "planned_source_change_task": planned_source_change_task,
                }
            )
            continue
        if lane_hint == "gpu" and gpu_slots <= 0:
            skipped.append({"task_id": task_id, "reason": "gpu_capacity_full"})
            continue
        if lane_hint == "gpu":
            allowed_for_task = set(_task_allowed_gpu_ids(task, allowed_gpu_ids))
            candidates = []
            for gpu_id in available_gpu_ids:
                snapshot = gpu_by_id.get(gpu_id, {})
                if float(snapshot.get("free_gb", 0.0)) < float(required_free_gpu_mem_gb or min_free_gpu_mem_gb):
                    continue
                base_count = running_gpu_counts.get(gpu_id, 0)
                planned_count = assigned_gpu_counts.get(gpu_id, 0)
                has_external = _gpu_has_untracked_compute(snapshot, base_count)
                if has_external:
                    continue
                limit_for_gpu = 1 if has_external else max_tasks_per_gpu
                if base_count + planned_count < limit_for_gpu:
                    candidates.append(gpu_id)
            if allowed_for_task:
                candidates = [gpu_id for gpu_id in candidates if gpu_id in allowed_for_task]
            candidates.sort(key=lambda gpu_id: _gpu_schedule_priority(gpu_by_id.get(gpu_id, {}), running_gpu_counts.get(gpu_id, 0), assigned_gpu_counts.get(gpu_id, 0)))
            if not candidates:
                skipped.append(
                    {
                        "task_id": task_id,
                        "reason": "no_gpu_with_required_free_memory",
                        "min_free_gpu_mem_gb": required_free_gpu_mem_gb,
                        "allowed_gpu_ids": sorted(allowed_for_task),
                    }
                )
                continue
            selected_gpu_id = candidates[0]
            selected_snapshot = gpu_by_id.get(selected_gpu_id, {})
            selected_gpu_uuid = selected_snapshot.get("uuid")
        if lane_hint == "cpu" and cpu_slots <= 0:
            skipped.append({"task_id": task_id, "reason": "cpu_capacity_full"})
            continue
        plan_item = {
            "task_id": task_id,
            "title": task.title,
            "target_workplace": _task_target_workplace(task) or None,
            "active_workplace": {
                "host": _normalize_workplace_value(host),
                "remote_root": _normalize_workplace_value(remote_root),
                "remote_workdir": _normalize_workplace_value(remote_workdir),
            },
            "resource_hint": hint,
            "execution_stage": stage,
            "code_change_scope": code_scope,
            "priority": task.priority,
            "runtime_budget_hours": task.raw_front_matter.get("runtime_budget_hours"),
            "selected_gpu_id": selected_gpu_id,
            "selected_gpu_uuid": selected_gpu_uuid,
            "min_free_gpu_mem_gb": required_free_gpu_mem_gb,
            "risk_flags": risk_flags,
        }
        plan.append(plan_item)
        if is_source_change:
            planned_source_change_task = task_id
        if lane_hint == "gpu":
            gpu_slots -= 1
            if selected_gpu_id:
                assigned_gpu_counts[selected_gpu_id] = assigned_gpu_counts.get(selected_gpu_id, 0) + 1
        elif lane_hint == "cpu":
            cpu_slots -= 1

    if not dry_run:
        launch_gpu_snapshot: Optional[Dict[str, object]] = None
        for item in plan:
            if item.get("resource_hint") == "gpu":
                if launch_gpu_snapshot is None:
                    launch_cache_seconds = int(gpu_snapshot_max_age_seconds or 0)
                    if launch_cache_seconds > 60:
                        launch_cache_seconds = 60
                    launch_gpu_snapshot = remote_gpu_snapshot(
                        host,
                        allowed_gpu_ids=allowed_gpu_ids,
                        max_age_seconds=launch_cache_seconds,
                        force_refresh=launch_cache_seconds <= 0,
                    )
                launch_snapshot = launch_gpu_snapshot
                selected_gpu_id = str(item.get("selected_gpu_id") or "")
                selected_gpu = next((gpu for gpu in launch_snapshot.get("gpus", []) if str(gpu.get("index")) == selected_gpu_id), {})
                required_free = float(item.get("min_free_gpu_mem_gb") or min_free_gpu_mem_gb)
                if not selected_gpu:
                    skipped.append(
                        {
                            "task_id": item["task_id"],
                            "reason": "launch_precheck_gpu_missing",
                            "selected_gpu_id": selected_gpu_id,
                        }
                    )
                    continue
                if float(selected_gpu.get("free_gb", 0.0)) < required_free:
                    skipped.append(
                        {
                            "task_id": item["task_id"],
                            "reason": "launch_precheck_free_memory_changed",
                            "selected_gpu_id": selected_gpu_id,
                            "free_gb": selected_gpu.get("free_gb"),
                            "required_free_gb": required_free,
                        }
                    )
                    continue
                refreshed_running_gpu_counts = _running_gpu_counts(host=host)
                current_own_tasks = refreshed_running_gpu_counts.get(selected_gpu_id, 0)
                has_external = _gpu_has_untracked_compute(selected_gpu, current_own_tasks)
                limit_for_gpu = 1 if has_external else max_tasks_per_gpu
                if has_external or current_own_tasks >= limit_for_gpu:
                    skipped.append(
                        {
                            "task_id": item["task_id"],
                            "reason": "launch_precheck_gpu_capacity_changed",
                            "selected_gpu_id": selected_gpu_id,
                            "own_tasks": current_own_tasks,
                            "own_compute_app_count": selected_gpu.get("own_compute_app_count"),
                            "external_compute_app_count": selected_gpu.get("external_compute_app_count"),
                            "limit_for_gpu": limit_for_gpu,
                            "has_external_compute_apps": has_external,
                        }
                    )
                    continue
            result = run_remote(
                str(item["task_id"]),
                host=host,
                remote_root=remote_root,
                worker=worker,
                remote_workdir=remote_workdir,
                detach=True,
                gpu_candidates_override=[str(item["selected_gpu_id"])] if item.get("selected_gpu_id") else None,
                min_free_gpu_mem_gb_override=float(item["min_free_gpu_mem_gb"]) if item.get("resource_hint") == "gpu" and item.get("min_free_gpu_mem_gb") is not None else None,
            )
            started.append({"task_id": item["task_id"], "run_id": result.get("run_id"), "attempt": result.get("attempt"), "run": result.get("run")})

    return {
        "dry_run": dry_run,
        "capacity": capacity,
        "limits": {
            "max_starts": max_starts,
            "max_running_total": max_running_total,
            "max_running_gpu": max_running_gpu,
            "max_running_cpu": max_running_cpu,
            "min_free_gpu_mem_gb": min_free_gpu_mem_gb,
            "allowed_gpu_ids": allowed_gpu_ids or [],
            "max_tasks_per_gpu": max_tasks_per_gpu,
            "external_process_policy": "allow_one_if_free_memory_threshold_is_met",
            "prepare_first_policy": "block_new_gpu_runs_while_ready_prepare_exists",
            "source_change_policy": "serialize_source_code_changes",
            "launch_gpu_precheck_max_age_seconds": min(int(gpu_snapshot_max_age_seconds or 0), 60) if int(gpu_snapshot_max_age_seconds or 0) > 0 else 0,
        },
        "selector": {
            "worker": worker,
            "line": line,
            "batch": batch,
            "dag_root": dag_root,
            "prefix": prefix,
        },
        "ready_prepare_exists": ready_prepare_exists,
        "dependency_reconcile": dependency_reconcile_report,
        "running_source_change_tasks": running_source_change_tasks,
        "running_gpu_counts": running_gpu_counts,
        "gpu_snapshot": {key: value for key, value in gpu_snapshot.items() if key != "raw"},
        "plan": plan,
        "started": started,
        "skipped": skipped,
    }


def _downstream_block_counts() -> Dict[str, int]:
    direct: Dict[str, set[str]] = {}
    for item in queue():
        task_id = str(item.get("task_id") or "")
        deps = item.get("dependency_state") if isinstance(item.get("dependency_state"), dict) else {}
        blockers: List[str] = []
        blockers.extend(str(dep) for dep in deps.get("missing", []) or [])
        for dep in deps.get("incomplete", []) or []:
            if isinstance(dep, dict):
                blockers.append(str(dep.get("task_id") or ""))
            else:
                blockers.append(str(dep))
        blockers.extend(str(dep) for dep in deps.get("missing_artifacts", []) or [])
        for blocker in blockers:
            if blocker:
                direct.setdefault(blocker, set()).add(task_id)
    return {task_id: len(blocked) for task_id, blocked in direct.items()}


def _has_active_downstream_block(task_id: str) -> bool:
    return _downstream_block_counts().get(task_id, 0) > 0


def _candidate_base_score(action: Dict[str, object]) -> int:
    score = 0
    action_type = str(action.get("action_type") or "")
    resource = str(action.get("resource") or "")
    stage = str(action.get("execution_stage") or "")
    if action_type == "start_task":
        score += 20
    if resource == "cpu" and stage == "analysis":
        score += 40
    if bool(action.get("uses_partial_artifacts")):
        score += 30
    if str(action.get("code_change_scope") or "") == "none":
        score += 20
    if resource == "gpu":
        score -= 30
    score += int(action.get("unblocks_downstream_count") or 0) * 10
    if action_type in {"verify_launch", "repair_launch_failed"}:
        score += 35
    if action_type == "soft_hold":
        score += 15
    if "code_and_run_should_split" in (action.get("risk_flags") or []):
        score -= 25
    return score


def scheduler_candidate_actions(plan: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    registry = load_registry()
    records = registry.get("tasks", {}) if isinstance(registry, dict) else {}
    downstream_counts = _downstream_block_counts()
    actions: List[Dict[str, object]] = []

    if isinstance(plan, dict):
        for item in plan.get("plan", []) or []:
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            task = None
            try:
                _, task_path = find_task(task_id)
                task = load_task(task_path)
            except FileNotFoundError:
                pass
            deps = dependency_state(task) if task else {}
            uses_partial = bool((deps.get("allow_partial_dependencies") or []) or (deps.get("allow_partial_artifacts_from") or []))
            risk_flags = [str(flag) for flag in item.get("risk_flags", []) or []]
            legal_status = "legal"
            reason_hint = ""
            if "code_and_run_should_split" in risk_flags:
                legal_status = "needs_confirmation"
                reason_hint = "Task mixes source-code changes with GPU execution. Split into prepare/code and run/GPU tasks, or explicitly set code_and_run_together: true."
            action = {
                "candidate_id": f"start__{task_id}",
                "action_type": "start_task",
                "task_id": task_id,
                "legal_status": legal_status,
                "resource": item.get("resource_hint"),
                "execution_stage": item.get("execution_stage"),
                "code_change_scope": item.get("code_change_scope"),
                "selected_gpu_id": item.get("selected_gpu_id"),
                "selected_gpu_uuid": item.get("selected_gpu_uuid"),
                "estimated_runtime_hours": item.get("runtime_budget_hours"),
                "uses_partial_artifacts": uses_partial,
                "unblocks_downstream_count": downstream_counts.get(task_id, 0),
                "hard_blockers": [],
                "risk_flags": risk_flags,
            }
            if reason_hint:
                action["reason_hint"] = reason_hint
            action["base_score"] = _candidate_base_score(action)
            actions.append(action)
            if "code_and_run_should_split" in risk_flags:
                split_action = {
                    "candidate_id": f"ask__{task_id}__split_prepare_run",
                    "action_type": "ask_user",
                    "task_id": task_id,
                    "legal_status": "needs_confirmation",
                    "question_type": "split_prepare_run_or_allow_combined",
                    "reason_hint": "This task should be split: first a prepare/code task, then a separate GPU run task. Combined execution needs explicit approval.",
                    "risk_flags": ["code_and_run_should_split"],
                    "unblocks_downstream_count": downstream_counts.get(task_id, 0),
                }
                split_action["base_score"] = _candidate_base_score(split_action)
                actions.append(split_action)

        for item in plan.get("skipped", []) or []:
            task_id = str(item.get("task_id") or "")
            reason = str(item.get("reason") or "")
            if not task_id:
                continue
            if reason == "source_code_change_serialized":
                action_type = "soft_hold"
                candidate_id = f"hold__{task_id}"
                legal_status = "advisory_only"
            elif reason in {"dependencies_not_ready", "no_gpu_with_required_free_memory", "gpu_capacity_full", "cpu_capacity_full"}:
                action_type = "warn_blocked"
                candidate_id = f"warn__{task_id}__{reason}"
                legal_status = "advisory_only"
            else:
                continue
            action = {
                "candidate_id": candidate_id,
                "action_type": action_type,
                "task_id": task_id,
                "legal_status": legal_status,
                "reason_code": reason,
                "reason_hint": reason.replace("_", " "),
                "unblocks_downstream_count": downstream_counts.get(task_id, 0),
                "hard_blockers": [item],
                "risk_flags": [reason],
            }
            action["base_score"] = _candidate_base_score(action)
            actions.append(action)

    for task_id, record in records.items():
        if not isinstance(record, dict):
            continue
        status_value = str(record.get("status") or "")
        latest_status = _latest_run_status(record)
        if status_value in {"launching", "launch_unknown"} or (status_value == "running" and latest_status == "launch_pending"):
            action = {
                "candidate_id": f"verify_launch__{task_id}",
                "action_type": "verify_launch",
                "task_id": str(task_id),
                "legal_status": "safe_after_revalidation",
                "reason_code": "launch_pending_needs_verification",
                "reason_hint": "Verify runner.pid / heartbeat before treating this as running.",
                "risk_flags": ["launch_pending"],
            }
            action["base_score"] = _candidate_base_score(action)
            actions.append(action)
        if status_value == "launch_failed":
            action = {
                "candidate_id": f"repair_launch_failed__{task_id}",
                "action_type": "repair_launch_failed",
                "task_id": str(task_id),
                "legal_status": "safe_after_revalidation",
                "reason_code": "remote_launch_failed",
                "reason_hint": "Reset to pending only if no remote runner heartbeat or process exists.",
                "risk_flags": ["launch_failed"],
            }
            action["base_score"] = _candidate_base_score(action)
            actions.append(action)
        if (
            status_value == "partial"
            and _existing_result_path_for_status(record, {"partial"})
            and _has_active_downstream_block(str(task_id))
        ):
            action = {
                "candidate_id": f"ask__{task_id}__partial_or_rerun",
                "action_type": "ask_user",
                "task_id": str(task_id),
                "legal_status": "needs_confirmation",
                "question_type": "keep_partial_or_rerun",
                "reason_hint": "Partial artifacts exist. Analyze first or spend GPU time on rerun?",
                "risk_flags": ["partial_result"],
            }
            action["base_score"] = _candidate_base_score(action)
            actions.append(action)

    actions.sort(key=lambda action: (-int(action.get("base_score") or 0), str(action.get("candidate_id") or "")))
    return {
        "context_version": 1,
        "registry_revision": now_iso(),
        "plan_epoch": _timestamp_id(),
        "candidate_actions": actions[:80],
    }


def scheduler_advisory(plan: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    candidates = scheduler_candidate_actions(plan)
    actions = candidates.get("candidate_actions", []) if isinstance(candidates, dict) else []
    recommended = []
    holds = []
    questions = []
    warnings = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "")
        if action_type == "start_task" and len(recommended) < 8:
            recommended.append(action)
        elif action_type == "soft_hold" and len(holds) < 8:
            holds.append(action)
        elif action_type == "ask_user" and len(questions) < 8:
            questions.append(action)
        elif len(warnings) < 8:
            warnings.append(action)
    payload = {
        **candidates,
        "advisor": "deterministic_candidate_ranker",
        "summary": "Candidate actions are generated by hard rules; this advisor only ranks and explains them.",
        "recommended_candidate_ids": [str(action.get("candidate_id")) for action in recommended],
        "holds": holds,
        "questions": questions,
        "warnings": warnings,
    }
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "scheduler_candidate_actions.json").write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")
    (state_dir() / "scheduler_advisory.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def dispatch(
    task_id: str,
    worker: str = "codex",
    submit: bool = True,
    *,
    force_worker_override: bool = False,
    override_reason: str = "",
) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    current_status, source_task = find_task(task_id)
    if current_status not in {"pending", "running"}:
        raise ValueError(f"cannot dispatch task in status {current_status}")
    task = load_task(source_task)
    _assert_worker_admitted(
        task,
        worker,
        force_worker_override=force_worker_override,
        override_reason=override_reason,
    )
    declared_worker = _task_declared_worker(task)
    allowed_workers = _task_allowed_workers(task)
    worker_override_needed = force_worker_override and _normalize_selector_value(worker) not in allowed_workers
    task.worker = worker
    task.validate()

    worktree = _prepare_worktree(task_id)
    sent = dispatch_dir(task_id)
    sent.mkdir(parents=True, exist_ok=True)
    task_markdown = source_task.read_text(encoding="utf-8")
    prompt_path = sent / "worker_prompt.md"
    prompt_path.write_text(build_worker_prompt(task, task_markdown), encoding="utf-8")
    metadata = {
        "task_id": task_id,
        "worker": worker,
        "created_at": now_iso(),
        "task_path": str(source_task),
        "worktree": str(worktree),
        "worker_prompt": str(prompt_path),
    }
    (sent / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    if current_status == "pending":
        running_dir = task_dir("running", task_id)
        if running_dir.exists():
            shutil.rmtree(running_dir)
        source_task.parent.rename(running_dir)

    record = upsert_task(task, status="running", worker=worker, started_at=now_iso())
    if worker_override_needed:
        _record_worker_override_event(
            task,
            worker,
            override_reason,
            declared_worker=declared_worker,
            allowed_workers=allowed_workers,
        )
        record = load_registry().get("tasks", {}).get(task_id, record)
    command = f'python cli.py run-worker {task_id} --worker {worker}'
    pueue = submit_to_pueue(command) if submit else {"submitted": False, "reason": "queue submission disabled", "command": command}
    record = update_task_record(task_id, dispatch_command=command, pueue=pueue)
    return {"record": record, "pueue": pueue, "worker_prompt": str(prompt_path), "worktree": str(worktree)}


_SECRET_PATTERN = re.compile(
    r"(?i)(api[-_]?key|token|bearer|authorization|password|secret)(\s*[:=]\s*)\S+"
)


def _scrub_secrets(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", text)


def _materializer_exception_result(task_id: str, exc: Exception) -> Dict[str, object]:
    from .schema import now_iso
    exc_type = type(exc).__name__
    safe_msg = _scrub_secrets(str(exc))
    return {
        "task_id": task_id,
        "status": "escalation_required",
        "summary_short": f"remote_materializer raised {exc_type} before producing result",
        "escalation_reason": f"provider_or_api_exception: {exc_type}",
        "validation_summary": {"status": "fail", "failed_validators": []},
        "validation_errors": [f"{exc_type}: {safe_msg}"],
        "generated_files": [],
        "retry_count": {"reader": 0, "qwen": 0},
        "created_at": now_iso(),
    }


def _resolve_provider_policy(task: "TaskBook", pkg: Path) -> Path:
    fm_policy = (task.raw_front_matter or {}).get("remote_materializer_policy")
    if fm_policy:
        text = str(fm_policy).strip()
        candidate = Path(text)
        if candidate.exists():
            return candidate
        config_dir = pkg / "remote_materializer" / "config"
        names = [text]
        if not text.endswith(".yaml"):
            names.append(f"provider_policy.{text}.yaml")
            names.append(f"{text}.yaml")
        for name in names:
            local = config_dir / Path(name).name
            if local.exists():
                return local
        return candidate
    env_policy = os.environ.get("REMOTE_MATERIALIZER_PROVIDER_POLICY")
    if env_policy:
        return Path(env_policy)
    return pkg / "remote_materializer" / "config" / "provider_policy.bailian.yaml"


def _run_worker_with_materializer_fallback(task_id: str) -> Dict[str, object]:
    """Run remote_materializer with bounded feedback retry and write _worker_out contract."""
    from .retry_controller import RetryController

    _, task_path = find_task(task_id)
    task = load_task(task_path)
    out_dir = worker_out_dir(task_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    _pkg = Path(__file__).resolve().parent.parent
    policy_path = _resolve_provider_policy(task, _pkg)
    env_file = Path.home() / ".remote_materializer" / ".env"
    mat_out = result_dir(task_id) / "materializer"

    retry_result = RetryController(
        task_id=task_id,
        task_path=task_path,
        mat_out=mat_out,
        policy_path=policy_path,
        env_file=env_file,
        stop_repeated_failure=True,
    ).run_attempts()

    mat_result = retry_result.mat_result
    summary = write_materializer_worker_out(
        task,
        out_dir,
        mat_result,
        mat_run_dir=retry_result.final_attempt_dir,
        allow_codex_fallback=bool(task.raw_front_matter.get("allow_codex_fallback", False)),
    )
    if retry_result.raw_exc_info:
        scrubbed = _scrub_secrets(retry_result.raw_exc_info + "\n")
        (out_dir / "stderr.log").write_text(scrubbed, encoding="utf-8")
    result: Dict[str, object] = {
        **mat_result,
        "returncode": 0 if summary.status == "success" else 1,
        "worker": "remote_materializer",
        "worker_status": summary.status,
        "result_yaml": str(summary.result_yaml),
        "fallback_executed": summary.fallback_executed,
        "retry_attempts": retry_result.attempts,
        "feedback_used": retry_result.feedback_used,
    }
    write_status_json(out_dir / "status.json", result)
    return result


def _copy_materializer_trace(mat_run_dir: Path, out_dir: Path) -> None:
    target = out_dir / "materializer"
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "result_package.yaml",
        "execution_plan.json",
        "commands.md",
        "validation_result.json",
        "routing_decisions.json",
        "model_usage.json",
        "run_manifest.json",
    ):
        source = mat_run_dir / name
        if source.exists() and source.is_file():
            _copy_file(source, target / name)
    for name in ("generated", "context_pack", "raw"):
        source = mat_run_dir / name
        if source.exists() and source.is_dir():
            _copy_dir(source, target / name)


def _run_execution_spec_for_worker(
    task_id: str,
    spec_path: Path,
    out_dir: Path,
    *,
    worker: str,
) -> Dict[str, object]:
    from solution_agent.execution_port.registry import register_execution
    from solution_agent.execution_port.safe_runner import run_execution
    from solution_agent.execution_port.monitor import monitor_loop
    from solution_agent.execution_port.collector import collect as ep_collect
    from solution_agent.execution_port.collector import to_worker_status as ep_to_worker_status
    from .paths import executions_dir

    ep_dir = executions_dir()
    ep_dir.mkdir(parents=True, exist_ok=True)

    execution_out = out_dir / "execution"
    execution_out.mkdir(parents=True, exist_ok=True)
    _ensure_execution_spec_artifact_roots(spec_path, out_dir)
    try:
        job_id = register_execution(spec_path, executions_dir=ep_dir)
        job_dir = ep_dir / job_id
        run_execution(job_dir)
        monitor_loop(job_dir, interval=0.5)
        final = ep_collect(job_dir)
    except Exception as exc:
        execution_out.mkdir(parents=True, exist_ok=True)
        if spec_path.exists():
            target_spec = execution_out / "execution_spec.yaml"
            if spec_path.resolve() != target_spec.resolve():
                _copy_file(spec_path, target_spec)
        text = str(exc).lower()
        failure_type = "path_policy_violation" if "path" in text or "outside" in text else "arg_policy_violation"
        typed = _typed_failure_payload(failure_type, str(exc))
        final = {
            "schema_version": 1,
            "task_id": task_id,
            "job_id": None,
            "status": "validation_failed",
            "failure_type": failure_type,
            "recommended_action": typed.get("recommended_action"),
            "judge": typed,
            "exit_code": -1,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "artifacts_found": [],
            "artifacts_missing": [],
            "metrics_summary": {},
            "metrics_missing": [],
        }
        (execution_out / "stderr.log").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        (execution_out / "final_result.yaml").write_text(
            yaml.safe_dump(final, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        job_dir = None
    for name in (
        "execution_spec.yaml",
        "script_registry.yaml",
        "state.json",
        "heartbeat.json",
        "runner.pid",
        "exit_code.txt",
        "stdout.log",
        "stderr.log",
        "collect_report.yaml",
        "final_result.yaml",
    ):
        source = job_dir / name if job_dir is not None else execution_out / name
        if job_dir is not None and source.exists() and source.is_file():
            _copy_file(source, execution_out / name)

    worker_result = dict(final)
    worker_result["schema_version"] = 1
    if str(final.get("status") or "") == "validation_failed":
        worker_result["status"] = "failed"
    else:
        worker_result["status"] = ep_to_worker_status(str(final.get("status") or ""))
    worker_result["execution_status"] = final.get("status")
    worker_result["worker"] = worker
    worker_result["summary_short"] = (
        "Execution completed successfully through execution_port."
        if worker_result["status"] == "success"
        else f"Execution finished with status {final.get('status') or 'unknown'}."
    )
    worker_result.setdefault("changed_files", [])
    worker_result.setdefault("commands_run", [])
    worker_result["outputs"] = {
        "worker_notes": "worker_notes.md",
        "execution": "execution/",
        "final_result": "execution/final_result.yaml",
        "collect_report": "execution/collect_report.yaml",
        "stdout": "execution/stdout.log",
        "stderr": "execution/stderr.log",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "worker_notes.md").write_text(
        "# Materialized Execution\n\n"
        f"{worker_result['summary_short']}\n\n"
        f"- execution_status: {worker_result.get('execution_status')}\n"
        f"- job_id: {worker_result.get('job_id')}\n",
        encoding="utf-8",
    )
    (out_dir / "result.yaml").write_text(
        yaml.safe_dump(worker_result, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    write_status_json(out_dir / "status.json", worker_result)
    return worker_result


def _write_materialized_execution_failure(
    task: "TaskBook",
    out_dir: Path,
    *,
    reason: str,
    mat_result: Dict[str, Any],
    mat_run_dir: Path | None = None,
    failure_type: str | None = None,
) -> Dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if mat_run_dir is not None and mat_run_dir.exists():
        _copy_materializer_trace(mat_run_dir, out_dir)
    gate = mat_result.get("execution_plan_gate") if isinstance(mat_result.get("execution_plan_gate"), dict) else {}
    typed_failure = failure_type or str(gate.get("failure_type") or "") or None
    typed = _typed_failure_payload(typed_failure, reason)
    payload: Dict[str, object] = {
        "schema_version": 1,
        "task_id": task.task_id,
        "status": "partial",
        "worker": "materialized_execution",
        "failure_type": typed.get("failure_type"),
        "recommended_action": typed.get("recommended_action"),
        "failure_routing": typed,
        "materializer_status": mat_result.get("status"),
        "completion_scope": "materialized_execution",
        "requires_downstream_execution": True,
        "needs_escalation": True,
        "recommended_worker": "codex",
        "summary_short": reason,
        "changed_files": [],
        "commands_run": [],
        "outputs": {
            "worker_notes": "worker_notes.md",
            "stdout": "stdout.log",
            "stderr": "stderr.log",
            "materializer": "materializer/",
        },
        "risks": [reason],
        "next_actions": [
            "Inspect materializer artifacts.",
            "Provide a valid execution block or allow explicit Codex fallback.",
        ],
        "fallback": {
            "requested": True,
            "allowed": bool(task.raw_front_matter.get("allow_codex_fallback", False)),
            "executed": False,
            "recommended_worker": "codex",
        },
    }
    (out_dir / "worker_notes.md").write_text(f"# Materialized Execution\n\n{reason}\n", encoding="utf-8")
    (out_dir / "materializer_result.json").write_text(json.dumps(mat_result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "stdout.log").write_text(json.dumps(mat_result, ensure_ascii=False, indent=2), encoding="utf-8")
    stderr_path = out_dir / "stderr.log"
    if not stderr_path.exists():
        stderr_path.write_text("", encoding="utf-8")
    (out_dir / "result.yaml").write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    write_status_json(out_dir / "status.json", payload)
    return payload


def _apply_claim_limitation_notice(
    worker_result: Dict[str, object],
    out_dir: Path,
    notice: Optional[Dict[str, object]],
) -> Dict[str, object]:
    if not notice:
        return worker_result
    result = dict(worker_result)
    existing = result.get("claim_limitations")
    limitations = list(existing) if isinstance(existing, list) else []
    limitations.append(notice)
    result["claim_limitations"] = limitations
    result["claim_level"] = notice.get("claim_level")
    result["formal_claim_allowed"] = bool(notice.get("formal_claim_allowed"))
    result["execution_allowed_with_claim_limitation"] = True
    risks = list(result.get("risks", []) or [])
    risks.append(str(notice.get("reason") or "claim limitation attached"))
    result["risks"] = risks

    result_path = out_dir / "result.yaml"
    if result_path.exists():
        result_path.write_text(yaml.safe_dump(result, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    notes_path = out_dir / "worker_notes.md"
    if notes_path.exists():
        with notes_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n## Claim Limitation\n\n"
                f"- claim_level: {notice.get('claim_level')}\n"
                f"- formal_claim_allowed: {notice.get('formal_claim_allowed')}\n"
                f"- reason: {notice.get('reason')}\n"
            )
    (out_dir / "claim_limitations.yaml").write_text(
        yaml.safe_dump({"claim_limitations": limitations}, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    write_status_json(out_dir / "status.json", result)
    return result


def _run_worker_materialized_execution(task_id: str) -> Dict[str, object]:
    """Materialize a structured task, then execute its validated spec."""
    from solution_agent.utils.paths import project_root

    _, task_path = find_task(task_id)
    _pkg = Path(__file__).resolve().parent.parent
    task = load_task(task_path)
    return run_materialized_execution_task(
        task_path=task_path,
        out_dir=worker_out_dir(task_id),
        workspace_root=project_root(),
        materializer_root=result_dir(task_id) / "materializer",
        policy_path=_resolve_provider_policy(task, _pkg),
        env_file=Path.home() / ".remote_materializer" / ".env",
    )


def _ensure_execution_spec_artifact_roots(spec_path: Path, out_dir: Path) -> None:
    """Allow artifacts in the execution workspace and the run root.

    Remote runs keep task output directories under remote_root while the source
    workspace is a separate repo checkout. The validator should still fail
    closed, but the generated spec needs to declare that run root explicitly.
    """
    if not spec_path.exists():
        return
    try:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        return
    roots: List[str] = []
    for value in workspace.get("artifact_roots") or workspace.get("allowed_artifact_roots") or []:
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)
    for value in (workspace.get("root"), out_dir.parent.parent):
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)
    if roots == workspace.get("artifact_roots"):
        return
    workspace["artifact_roots"] = roots
    data["workspace"] = workspace
    spec_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _task_flag(task: TaskBook, name: str, default: bool = False) -> bool:
    value = (task.raw_front_matter or {}).get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _task_requires_execution_plan(task: TaskBook) -> bool:
    if task.worker != "materialized_execution":
        return False
    if _task_flag(task, "materialized_execution_bypass_materializer", False):
        return False
    return not _task_flag(task, "disable_execution_plan_gate", False)


def _default_execution_registry() -> dict:
    try:
        from ..execution_port.script_registry import load_registry

        return load_registry()
    except Exception:
        return {}


def _default_policy_registry() -> dict:
    path = Path(__file__).with_name("policy_registry.yaml")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _script_entry_from_registry(script_id: str) -> Dict[str, object]:
    try:
        from ..execution_port.script_registry import get_script_entry

        return get_script_entry(_default_execution_registry(), script_id)
    except Exception:
        return {}


def _policy_gate_config() -> Dict[str, object]:
    policy = _default_policy_registry()
    gate = policy.get("execution_plan_gate", {})
    return gate if isinstance(gate, dict) else {}


def _policy_numeric_config() -> Dict[str, object]:
    policy = _default_policy_registry()
    numeric = policy.get("numeric_policy", {})
    return numeric if isinstance(numeric, dict) else {}


def _policy_repair_config() -> Dict[str, object]:
    policy = _default_policy_registry()
    repair = policy.get("one_shot_repair", {})
    return repair if isinstance(repair, dict) else {}


def _policy_approval(gate: Dict[str, object], name: str, default: str) -> str:
    approvals = gate.get("approvals", {})
    if isinstance(approvals, dict):
        return str(approvals.get(name) or default)
    return default


def _policy_list(gate: Dict[str, object], section: str, name: str) -> set[str]:
    parent = gate.get(section, {})
    if not isinstance(parent, dict):
        return set()
    values = parent.get(name, [])
    if not isinstance(values, list):
        return set()
    return {str(item).strip().lower() for item in values if str(item).strip()}


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _typed_failure_payload(failure_type: str | None, message: str) -> Dict[str, object]:
    contract = failure_contract(failure_type)
    return {
        "failure_type": failure_type,
        "repairable": bool(contract.get("repairable", False)),
        "user_action_required": bool(contract.get("user_action_required", False)),
        "codex_recommended": bool(contract.get("codex_recommended", False)),
        "execution_success": bool(contract.get("execution_success", False)),
        "warn_only": bool(contract.get("warn_only", False)),
        "recommended_action": recommended_action_for_failure(failure_type, message=message),
    }


def _candidate_execution_plan_paths(mat_run_dir: Path) -> List[Path]:
    return [
        mat_run_dir / "execution_plan.json",
        mat_run_dir / "generated" / "execution_plan.json",
        mat_run_dir / "execution_plan.yaml",
        mat_run_dir / "generated" / "execution_plan.yaml",
    ]


def _registry_output_script_allowlist() -> Dict[str, List[str]]:
    try:
        from ..execution_port.script_registry import get_output_family_allowlist

        return get_output_family_allowlist(_default_execution_registry())
    except Exception:
        return {}


def _output_script_allowlist() -> Dict[str, List[str]]:
    return _registry_output_script_allowlist()


def _script_default_execution_kind(script_id: str) -> str:
    scripts = _default_execution_registry().get("scripts", {})
    entry = scripts.get(script_id, {}) if isinstance(scripts, dict) else {}
    if not isinstance(entry, dict):
        return ""
    default_execution = entry.get("default_execution", {})
    if not isinstance(default_execution, dict):
        return ""
    return str(default_execution.get("kind") or "").strip()


def _script_writes_artifact_family(script_id: str, family: str) -> bool:
    scripts = _default_execution_registry().get("scripts", {})
    entry = scripts.get(script_id, {}) if isinstance(scripts, dict) else {}
    families = entry.get("produces_artifact_families", []) if isinstance(entry, dict) else []
    if not isinstance(families, list):
        return False
    expected = str(family).strip()
    return any(isinstance(item, dict) and str(item.get("family") or "").strip() == expected for item in families)


def _closure_contract_files(registry_entry: Dict[str, object] | None = None) -> tuple[str, ...]:
    registry = _default_execution_registry()
    contract = registry.get("closure_contract", {}) if isinstance(registry.get("closure_contract"), dict) else {}
    merged = dict(contract)
    script_contract = registry_entry.get("closure_contract", {}) if isinstance(registry_entry, dict) else {}
    if isinstance(script_contract, dict):
        merged.update(script_contract)
    if merged.get("enabled") is False:
        return ()
    files = merged.get("required_files")
    if not isinstance(files, list) or not files:
        files = merged.get("default_required_files")
    return tuple(str(item).strip() for item in files if str(item).strip()) if isinstance(files, list) else ()


def _artifact_family_config(family: str) -> Dict[str, object]:
    try:
        from ..execution_port.script_registry import get_artifact_family

        return get_artifact_family(_default_execution_registry(), family)
    except Exception:
        return {}


def _list_of_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip().lower() for item in value if str(item).strip())


def _is_contract_artifact_output(name: str) -> bool:
    normalized = name.strip().strip("`").replace("\\", "/").split("/")[-1].lower()
    if not normalized:
        return False
    config = _artifact_family_config("contract_artifact")
    exact_names = _list_of_strings(config.get("exact_names"))
    suffixes = _list_of_strings(config.get("suffixes"))
    markers = _list_of_strings(config.get("markers"))
    return normalized in exact_names or normalized.endswith(suffixes) or any(marker in normalized for marker in markers)


def _is_file_output_name(name: str) -> bool:
    normalized = name.strip().strip("`").replace("\\", "/").split("/")[-1].lower()
    return bool(re.search(r"\.(?:csv|json|yaml|yml|md|txt|jpg|jpeg|png|ckpt|pt|pth|safetensors)$", normalized))


def _load_execution_plan(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("execution plan must be a JSON/YAML object")
    return data


def _extract_task_bullets(task: TaskBook, section_name: str) -> List[str]:
    bullets: List[str] = []
    in_section = False
    target = section_name.strip().lower()
    for raw in task.body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            if in_section and title != target:
                break
            in_section = title == target
            continue
        if in_section and line.startswith("- "):
            bullets.append(line[2:].strip())
        elif in_section and bullets and line:
            break
    return bullets


def _detect_metric_terms(text: str) -> List[str]:
    candidates = [
        "train_iou",
        "train_f1",
        "checkpoint_iou",
        "checkpoint_f1",
        "train_loss_final",
        "loss_curve_rows",
        "train_eval_rows",
    ]
    lowered = text.lower()
    found: List[str] = []
    for candidate in candidates:
        token = candidate.lower()
        loose = token.replace("_", " ")
        if token in lowered or loose in lowered:
            found.append(candidate)
    return found


def _load_upstream_artifact_evidence(mat_run_dir: Path) -> Dict[str, object]:
    summary_path = mat_run_dir / "context_pack" / "artifact_summary.md"
    evidence: Dict[str, object] = {"artifact_paths": []}
    if not summary_path.exists():
        return evidence
    paths: List[str] = []
    for raw in summary_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            paths.append(line[2:].strip())
    evidence["artifact_paths"] = paths
    metrics_path = next((Path(path) for path in paths if path.endswith("_metrics.json")), None)
    if metrics_path and metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(metrics, dict):
                evidence["metrics"] = metrics
        except Exception:
            evidence["metrics_error"] = f"could not parse {metrics_path}"
    decision_path = next((Path(path) for path in paths if path.endswith("_decision.json")), None)
    if decision_path and decision_path.exists():
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            if isinstance(decision, dict):
                evidence["decision"] = decision
        except Exception:
            evidence["decision_error"] = f"could not parse {decision_path}"
    for key, suffix in (
        ("loss_curve_rows", "_loss_curve.csv"),
        ("train_eval_rows", "_train_eval.csv"),
        ("sample_manifest_rows", "_sample_manifest.csv"),
    ):
        csv_path = next((Path(path) for path in paths if path.endswith(suffix)), None)
        if csv_path and csv_path.exists():
            try:
                lines = [line for line in csv_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
                evidence[key] = max(0, len(lines) - 1)
            except Exception:
                evidence[f"{key}_error"] = f"could not parse {csv_path}"
    evidence["visual_sheet_present"] = any(path.endswith((".jpg", ".jpeg", ".png")) for path in paths)
    return evidence


def _scale_gate_claim_limitation(task: TaskBook, mat_run_dir: Path) -> Optional[Dict[str, object]]:
    stage = str((task.raw_front_matter or {}).get("execution_stage") or "").strip().lower()
    if stage in {"analysis", "report", "diagnosis", "audit", "review", "decision", "contract", "prepare"}:
        return None
    body = task.body.lower()
    if "can_scale_to_100fake" not in body and "valid to scale" not in body:
        return None
    evidence = _load_upstream_artifact_evidence(mat_run_dir)
    decision = evidence.get("decision") if isinstance(evidence.get("decision"), dict) else {}
    if decision.get("can_scale_to_100fake") is True:
        return None
    blocking = decision.get("blocking_reason") or "missing_context"
    next_action = decision.get("required_next_action") or "inspect_parent_decision"
    reason = (
        "parent decision does not permit scaling: "
        f"can_scale_to_100fake={decision.get('can_scale_to_100fake')!r}, "
        f"blocking_reason={blocking}, required_next_action={next_action}"
    )
    return {
        "type": "parent_decision_claim_limitation",
        "execution_allowed": True,
        "formal_claim_allowed": False,
        "claim_level": "exploratory",
        "reason": reason,
        "blocking_reason": blocking,
        "required_next_action": next_action,
        "parent_decision": decision,
        "policy": "insufficient evidence downgrades claims; it does not veto a queued user-requested execution",
        "must_not_claim": [
            "formal scale-up approved",
            "parent science success proven",
            "this run is a validated continuation of a passed science gate",
        ],
    }


def _scale_gate_block_reason(task: TaskBook, mat_run_dir: Path) -> Optional[str]:
    """Deprecated compatibility shim.

    Parent evidence insufficiency is now a claim limitation, not an execution
    veto. Keep the public helper returning None so older callers/tests do not
    accidentally preserve the old blocking behavior.
    """
    return None


def _analysis_decision_from_evidence(task: TaskBook, evidence: Dict[str, object]) -> Dict[str, object]:
    metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else {}
    loss_rows = int(metrics.get("loss_curve_rows") or evidence.get("loss_curve_rows") or 0)
    train_rows = int(metrics.get("train_eval_rows") or evidence.get("train_eval_rows") or 0)
    visual_present = bool(metrics.get("visual_sheet_present") or evidence.get("visual_sheet_present"))
    contract_status = str(metrics.get("contract_status") or "unknown")
    science_status = str(metrics.get("science_status") or "unknown")
    failure = str(metrics.get("failure_taxonomy") or "").strip()
    blocking = failure or "none"
    if loss_rows <= 0 or train_rows <= 0:
        blocking = "missing_evidence"
        next_action = "fix_logging"
    elif not visual_present:
        blocking = "visual_failure"
        next_action = "inspect_mask_alignment"
    elif contract_status != "complete" or science_status != "pass":
        blocking = "missing_evidence"
        next_action = "rerun_micro"
    else:
        next_action = "scale_to_100fake"
    can_scale = blocking == "none" and next_action == "scale_to_100fake"
    if not can_scale and next_action == "scale_to_100fake":
        next_action = "rerun_micro"
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "parent_execution_status": str(metrics.get("execution_status") or metrics.get("status") or "unknown"),
        "parent_contract_status": contract_status,
        "parent_science_status": science_status,
        "can_scale_to_100fake": bool(can_scale),
        "blocking_reason": blocking,
        "required_next_action": next_action,
        "evidence": {
            "train_iou": metrics.get("train_iou"),
            "train_loss_final": metrics.get("train_loss_final"),
            "checkpoint_iou": metrics.get("checkpoint_iou"),
            "checkpoint_f1": metrics.get("checkpoint_f1"),
            "loss_curve_rows": loss_rows,
            "train_eval_rows": train_rows,
            "visual_sheet_present": visual_present,
        },
    }


def _failure_table_from_decision(decision: Dict[str, object]) -> str:
    evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
    rows = ["check,status,value,reason"]
    rows.append(f"loss_curve_rows,{ 'pass' if int(evidence.get('loss_curve_rows') or 0) > 0 else 'fail' },{evidence.get('loss_curve_rows')},trend evidence required")
    rows.append(f"train_eval_rows,{ 'pass' if int(evidence.get('train_eval_rows') or 0) > 0 else 'fail' },{evidence.get('train_eval_rows')},train metric table required")
    rows.append(f"visual_sheet_present,{ 'pass' if evidence.get('visual_sheet_present') else 'fail' },{evidence.get('visual_sheet_present')},visual evidence required")
    rows.append(f"science_status,{ 'pass' if decision.get('parent_science_status') == 'pass' else 'fail' },{decision.get('parent_science_status')},scientific pass required before scaling")
    return "\n".join(rows)


def _artifact_matrix_from_evidence(evidence: Dict[str, object]) -> str:
    paths = [str(path) for path in evidence.get("artifact_paths", []) or []]
    rows = ["artifact,status,path_or_value,reason"]
    rows.append(f"metrics_json,{ 'present' if evidence.get('metrics') else 'missing' },{next((p for p in paths if p.endswith('_metrics.json')), '')},metrics required for evidence judging")
    rows.append(f"decision_json,{ 'present' if evidence.get('decision') else 'missing' },{next((p for p in paths if p.endswith('_decision.json')), '')},decision artifact if parent analysis exists")
    rows.append(f"loss_curve_rows,{ 'present' if int(evidence.get('loss_curve_rows') or 0) > 0 else 'missing' },{evidence.get('loss_curve_rows', 0)},trend evidence")
    rows.append(f"train_eval_rows,{ 'present' if int(evidence.get('train_eval_rows') or 0) > 0 else 'missing' },{evidence.get('train_eval_rows', 0)},per-epoch/per-eval metric evidence")
    rows.append(f"visual_sheet,{ 'present' if evidence.get('visual_sheet_present') else 'missing' },{evidence.get('visual_sheet_present', False)},visual sanity evidence")
    rows.append(f"artifact_count,present,{len(paths)},available artifact pointers in context pack")
    return "\n".join(rows)


def _contract_artifact_request_from_task(
    task: TaskBook,
    outputs: List[str],
    evidence: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    success_bullets = _extract_task_bullets(task, "success criteria")
    constraints = _extract_task_bullets(task, "constraints")
    metric_terms = _detect_metric_terms(task.body)
    evidence = evidence or {}
    decision = _analysis_decision_from_evidence(task, evidence) if evidence.get("metrics") else None
    required_artifacts = [
        "metrics file with finite numeric metrics",
        "loss curve or train/eval table with rows",
        "visual evidence when the task asks for visual proof",
        "sample manifest or explicit sample source",
        "checkpoint path or checkpoint role summary when a checkpoint is produced",
        "summary markdown explaining execution_status, contract_status, and science_status",
    ]
    output_items: List[Dict[str, object]] = []
    for raw_name in outputs:
        name = raw_name.replace("\\", "/").split("/")[-1].strip()
        lowered = name.lower()
        if lowered.endswith(".json"):
            content = {
                "schema_version": 1,
                "task_id": task.task_id,
                "status_fields": ["execution_status", "contract_status", "science_status"],
                "required_metrics": metric_terms,
                "finite_numeric_metrics_required": True,
                "required_artifact_families": required_artifacts,
                "missing_evidence_if": [
                    "required metric key is missing, null, empty, NaN, inf, or non-numeric",
                    "loss curve or train/eval rows are absent when trend evidence is required",
                    "visual evidence is absent when visual proof is required",
                ],
            }
            if decision and "decision" in lowered:
                content = decision
            output_items.append(
                {
                    "path": name,
                    "kind": "json",
                    "content": content,
                }
            )
        elif lowered.endswith(".csv"):
            if decision and "failure_table" in lowered:
                content = _failure_table_from_decision(decision)
            else:
                content = _artifact_matrix_from_evidence(evidence)
            output_items.append({"path": name, "kind": "csv", "content": content})
        elif lowered.endswith((".yaml", ".yml")):
            output_items.append(
                {
                    "path": name,
                    "kind": "yaml",
                    "content": {
                        "schema_version": 1,
                        "task_id": task.task_id,
                        "rules": {
                            "execution_success": "command exit_code == 0 and required artifacts exist",
                            "science_status_pass": [
                                "loss curve rows are present",
                                "train IoU/F1 or equivalent task metrics are present and finite",
                                "metric/table values support the hypothesis rather than only file presence",
                            ],
                            "missing_evidence": [
                                "loss curve rows missing",
                                "train metric trend missing",
                                "required visual or table evidence missing",
                            ],
                            "weak_metric_result": "finite but weak values should warn unless the task declares a hard threshold",
                        },
                        "recommended_action": {
                            "missing_evidence": "repair_plan_or_request_context",
                            "weak_metric_result": "no_retry_without_new_hypothesis",
                        },
                    },
                }
            )
        else:
            sections = [
                {
                    "heading": "Purpose",
                    "body": (
                        "Define reusable evidence requirements so a task is not marked "
                        "scientifically successful only because execution finished or a checkpoint exists."
                    ),
                },
                {"heading": "Required Artifact Families", "bullets": required_artifacts},
                {"heading": "Success Criteria From Task", "bullets": success_bullets or ["No explicit success bullets were provided."]},
                {"heading": "Constraints", "bullets": constraints or ["Do not modify source data or existing artifacts."]},
                {
                    "heading": "Judging Rule",
                    "bullets": [
                        "Judge metric/table values, not only artifact presence.",
                        "Keep execution_status, contract_status, and science_status separate.",
                        "Use missing_evidence when execution completes but evidence is insufficient.",
                    ],
                },
            ]
            if decision:
                sections.insert(
                    1,
                    {
                        "heading": "Parent Evidence Values",
                        "body": yaml.safe_dump(decision, sort_keys=False, allow_unicode=False),
                    },
                )
            output_items.append({"path": name, "kind": "markdown", "title": name.rsplit(".", 1)[0].replace("_", " ").title(), "sections": sections})
    return {"task_id": task.task_id, "outputs": output_items}


def _try_synthesize_contract_writer_handoff(
    task: TaskBook,
    mat_run_dir: Path,
    out_dir: Path,
    workspace_root: Path,
    mat_result: Dict[str, object],
) -> Dict[str, object]:
    plan_path = next((path for path in _candidate_execution_plan_paths(mat_run_dir) if path.exists()), None)
    if plan_path is None:
        return mat_result
    try:
        plan = _load_execution_plan(plan_path)
    except Exception:
        return mat_result
    strategy = str(plan.get("execution_strategy") or "").lower()
    outputs = _execution_output_names_from_task(task)
    stage = str((task.raw_front_matter or {}).get("execution_stage") or "").strip().lower()
    archetype_match = re.search(r"archetype:\s*`?([A-Za-z0-9_-]+)`?", task.body, re.IGNORECASE)
    archetype = archetype_match.group(1).lower() if archetype_match else ""
    writer_like_task = stage in {"prepare", "analysis", "report", "contract"} or archetype in {
        "analysis",
        "report",
        "contract",
        "prepare",
    }
    if "contract_artifact_writer" not in strategy and not writer_like_task:
        return mat_result
    if not outputs or not all(_is_contract_artifact_output(name) for name in outputs):
        return mat_result

    hints_path = mat_run_dir / "context_pack" / "execution_hints.yaml"
    try:
        hints = yaml.safe_load(hints_path.read_text(encoding="utf-8")) or {}
    except Exception:
        hints = {}
    output_policy = hints.get("output_policy") if isinstance(hints.get("output_policy"), dict) else {}
    output_dir = str(out_dir / "artifacts").replace("\\", "/")
    artifact_roots = output_policy.get("allowed_artifact_roots")
    if not isinstance(artifact_roots, list):
        artifact_roots = [str(workspace_root).replace("\\", "/"), str(out_dir.parent).replace("\\", "/")]

    generated = mat_run_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    request_path = generated / "contract_artifact_request.yaml"
    evidence = _load_upstream_artifact_evidence(mat_run_dir)
    request = _contract_artifact_request_from_task(task, outputs, evidence)
    request_path.write_text(yaml.safe_dump(request, sort_keys=False, allow_unicode=False), encoding="utf-8")

    required_artifacts = []
    for name in outputs:
        artifact_name = name.replace("\\", "/").split("/")[-1]
        required_artifacts.append(f"{output_dir.rstrip('/')}/{artifact_name}")
    spec = {
        "schema_version": 1,
        "task_id": task.task_id,
        "job_type": "materialized_execution",
        "workspace": {
            "root": str(workspace_root).replace("\\", "/"),
            "cwd": str(workspace_root).replace("\\", "/"),
            "artifact_roots": [str(item).replace("\\", "/") for item in artifact_roots],
        },
        "command": {
            "script_id": "contract_artifact_writer",
            "args": {
                "config": str(request_path).replace("\\", "/"),
                "output_dir": output_dir,
            },
            "timeout_sec": 1800,
        },
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": required_artifacts},
        "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
        "solution_agent_normalizations": [
            {
                "type": "contract_writer_handoff_from_plan",
                "reason": "materializer selected contract_artifact_writer in execution_plan but omitted execution_spec.yaml",
            }
        ],
    }
    (mat_run_dir / "execution_spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    repaired = dict(mat_result)
    repaired["status"] = "materialized"
    repaired["contract_writer_handoff_synthesized"] = True
    repaired["generated_files"] = list(repaired.get("generated_files") or [])
    for name in ("generated/contract_artifact_request.yaml", "execution_spec.yaml"):
        if name not in repaired["generated_files"]:
            repaired["generated_files"].append(name)
    return repaired


def _nested_get(mapping: Dict[str, object], path: List[str]) -> object:
    current: object = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _try_synthesize_registry_handoff(
    task: TaskBook,
    mat_run_dir: Path,
    out_dir: Path,
    workspace_root: Path,
    mat_result: Dict[str, object],
) -> Dict[str, object]:
    plan_path = next((path for path in _candidate_execution_plan_paths(mat_run_dir) if path.exists()), None)
    if plan_path is None:
        return mat_result
    try:
        plan = _load_execution_plan(plan_path)
    except Exception:
        return mat_result
    registry = _default_execution_registry()
    scripts = registry.get("scripts") if isinstance(registry.get("scripts"), dict) else {}
    if not scripts:
        return mat_result
    strategy = str(plan.get("execution_strategy") or "").lower()
    outputs = [name for name in _execution_output_names_from_task(task) if _is_file_output_name(name)]
    for script_id, entry in scripts.items():
        if not isinstance(entry, dict):
            continue
        synthesis = entry.get("handoff_synthesis")
        if not isinstance(synthesis, dict):
            continue
        mentions = str(synthesis.get("when_strategy_mentions") or script_id).strip().lower()
        if mentions and mentions not in strategy:
            continue
        output_prefixes = [str(item).strip() for item in synthesis.get("output_prefixes") or [] if str(item).strip()]
        if output_prefixes and not any(name.startswith(tuple(output_prefixes)) for name in outputs):
            continue
        required_flags = [str(item).strip() for item in synthesis.get("required_task_flags") or [] if str(item).strip()]
        if any(not _task_flag(task, flag, False) for flag in required_flags):
            continue
        spec = _registry_handoff_spec_for_script(
            str(script_id),
            entry,
            synthesis,
            task,
            mat_run_dir,
            out_dir,
            workspace_root,
            outputs,
        )
        if spec is None:
            continue
        (mat_run_dir / "execution_spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
        repaired = dict(mat_result)
        repaired["status"] = "materialized"
        repaired["registry_handoff_synthesized"] = True
        repaired[f"{script_id}_handoff_synthesized"] = True
        repaired["generated_files"] = list(repaired.get("generated_files") or [])
        if "execution_spec.yaml" not in repaired["generated_files"]:
            repaired["generated_files"].append("execution_spec.yaml")
        return repaired
    return mat_result


def _registry_handoff_spec_for_script(
    script_id: str,
    registry_entry: Dict[str, object],
    synthesis: Dict[str, object],
    task: TaskBook,
    mat_run_dir: Path,
    out_dir: Path,
    workspace_root: Path,
    outputs: List[str],
) -> Dict[str, object] | None:
    workspace_context_path = mat_run_dir / "context_pack" / "workspace_context_excerpt.yaml"
    try:
        workspace_context = yaml.safe_load(workspace_context_path.read_text(encoding="utf-8")) or {}
    except Exception:
        workspace_context = {}
    if not isinstance(workspace_context, dict):
        workspace_context = {}
    output_dir = str(out_dir / "artifacts").replace("\\", "/")
    args: Dict[str, object] = {"output_dir": output_dir}

    asset_bindings = synthesis.get("project_asset_bindings")
    if isinstance(asset_bindings, dict):
        for arg_name, binding in asset_bindings.items():
            if not isinstance(binding, dict):
                continue
            path = binding.get("path")
            if not isinstance(path, list):
                continue
            value = _nested_get(workspace_context, [str(item) for item in path])
            if not value:
                return None
            args[str(arg_name)] = str(value)

    workspace_arg = str(synthesis.get("workspace_arg") or "").strip()
    if workspace_arg:
        args[workspace_arg] = str(workspace_root).replace("\\", "/")

    default_args = synthesis.get("default_args")
    if isinstance(default_args, dict):
        for key, value in default_args.items():
            args.setdefault(str(key), value)

    text = task.body.lower()
    text_int_args = synthesis.get("text_int_args")
    if isinstance(text_int_args, dict):
        for arg_name, raw_rule in text_int_args.items():
            if not isinstance(raw_rule, dict):
                continue
            value = _safe_int(raw_rule.get("default"), 0)
            overrides = raw_rule.get("phrase_overrides")
            if isinstance(overrides, list):
                for raw_override in overrides:
                    if not isinstance(raw_override, dict):
                        continue
                    phrase = str(raw_override.get("phrase") or "").lower()
                    required_flag = str(raw_override.get("requires_flag") or "").strip()
                    if phrase and phrase in text and (not required_flag or _task_flag(task, required_flag, False)):
                        value = _safe_int(raw_override.get("value"), value)
            pattern = str(raw_rule.get("pattern") or "").strip()
            if pattern:
                match = re.search(pattern, text)
                if match:
                    value = _safe_int(match.group(1), value)
            args[str(arg_name)] = value
            mirror_to = raw_rule.get("mirror_to")
            if isinstance(mirror_to, list):
                for mirror_name in mirror_to:
                    args[str(mirror_name)] = value

    artifact_prefix = str((synthesis.get("artifact_prefix") or {}).get("default") if isinstance(synthesis.get("artifact_prefix"), dict) else "").strip()
    artifact_prefix_config = synthesis.get("artifact_prefix")
    if isinstance(artifact_prefix_config, dict):
        prefix_map = artifact_prefix_config.get("output_prefix_map")
        if isinstance(prefix_map, dict):
            for prefix, value in prefix_map.items():
                if any(name.startswith(str(prefix)) for name in outputs):
                    artifact_prefix = str(value)
                    break
    if artifact_prefix:
        args["artifact_prefix"] = artifact_prefix

    output_base = output_dir.rstrip("/")
    required_artifacts = []
    for name in outputs:
        output_name = str(name).replace("\\", "/").split("/")[-1]
        required_artifacts.append(f"{output_base}/{output_name}")
    extra_by_prefix = synthesis.get("extra_required_artifacts_by_artifact_prefix")
    if artifact_prefix and isinstance(extra_by_prefix, dict):
        extras = extra_by_prefix.get(artifact_prefix)
        if isinstance(extras, list):
            for extra in extras:
                path = f"{output_dir.rstrip('/')}/{str(extra).strip()}"
                if str(extra).strip() and path not in required_artifacts:
                    required_artifacts.append(path)

    timeout_cfg = synthesis.get("timeout") if isinstance(synthesis.get("timeout"), dict) else {}
    timeout_sec = _safe_int(timeout_cfg.get("default_sec"), _safe_int(registry_entry.get("max_timeout_sec"), 3600))
    budget_field = str(timeout_cfg.get("task_runtime_budget_hours_field") or "").strip()
    if budget_field:
        timeout_sec = _safe_int((task.raw_front_matter or {}).get(budget_field), max(1, timeout_sec // 3600)) * 3600
    timeout_sec = max(_safe_int(timeout_cfg.get("min_sec"), 1), min(timeout_sec, _safe_int(timeout_cfg.get("max_sec"), timeout_sec)))

    resources_cfg = synthesis.get("resources") if isinstance(synthesis.get("resources"), dict) else {}
    gpu_required = bool(resources_cfg.get("gpu_required", registry_entry.get("gpu_allowed", False)))
    gpu_count = _safe_int(resources_cfg.get("gpu_count"), 1 if gpu_required else 0)

    success_cfg = synthesis.get("success_criteria") if isinstance(synthesis.get("success_criteria"), dict) else {}
    metrics_template = str(success_cfg.get("metrics_file_template") or "").strip()
    metrics_file = metrics_template.format(output_dir=output_dir.rstrip("/"), artifact_prefix=artifact_prefix) if metrics_template else ""
    metric_keys = [str(item) for item in success_cfg.get("metric_keys") or [] if str(item).strip()]

    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "job_type": "materialized_execution",
        "workspace": {
            "root": str(workspace_root).replace("\\", "/"),
            "cwd": str(workspace_root).replace("\\", "/"),
            "artifact_roots": [str(workspace_root).replace("\\", "/"), str(out_dir.parent).replace("\\", "/"), output_dir],
        },
        "command": {
            "script_id": script_id,
            "args": args,
            "timeout_sec": timeout_sec,
        },
        "resources": {"gpu_required": gpu_required, "gpu_count": gpu_count},
        "artifacts": {"required": required_artifacts},
        "success_criteria": {"exit_code": 0, "metrics_file": metrics_file, "metric_keys": metric_keys},
        "solution_agent_normalizations": [
            {
                "type": "registry_handoff_from_plan",
                "script_id": script_id,
                "reason": "materializer selected a registry-declared handoff script in execution_plan but omitted execution_spec.yaml",
            }
        ],
    }


def _repair_plan_json_syntax(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None
    try:
        json.loads(candidate)
        return candidate + "\n"
    except json.JSONDecodeError:
        pass
    stack: List[str] = []
    pairs = {"{": "}", "[": "]"}
    closers = set(pairs.values())
    for char in candidate:
        if char in pairs:
            stack.append(pairs[char])
        elif char in closers:
            if not stack or stack[-1] != char:
                return None
            stack.pop()
    if not stack or len(stack) > 3:
        return None
    suffix = "".join(reversed(stack))
    if suffix:
        for char in suffix:
            if char not in closers:
                return None
        if len(suffix) > 3:
            return None
    repaired = candidate + suffix
    try:
        json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return repaired + "\n"


def _repair_execution_plan_payload(payload: Dict[str, object]) -> tuple[Dict[str, object], List[str]]:
    repaired = dict(payload)
    changes: List[str] = []
    if "schema_version" not in repaired:
        repaired["schema_version"] = 1
        changes.append("added schema_version=1")
    if "read_paths" not in repaired or not isinstance(repaired.get("read_paths"), list):
        repaired["read_paths"] = []
        changes.append("added read_paths=[]")
    if "write_paths" not in repaired or not isinstance(repaired.get("write_paths"), list):
        repaired["write_paths"] = []
        changes.append("added write_paths=[]")
    access = repaired.get("data_access")
    if not isinstance(access, dict):
        access = {}
        repaired["data_access"] = access
        changes.append("added data_access={}")
    for key, default in {
        "uses_existing_indices": True,
        "opens_image_files": False,
        "opens_image_pixels": False,
        "full_recursive_dataset_scan": False,
        "bounded_raw_dataset_discovery": False,
        "copies_or_moves_data": False,
        "modifies_datasets": False,
    }.items():
        if key not in access:
            access[key] = default
            changes.append(f"added data_access.{key}={str(default).lower()}")
    if "execution_strategy" not in repaired:
        repaired["execution_strategy"] = "Use allowlisted script with bounded declared read/write scope."
        changes.append("added execution_strategy")
    return repaired, changes


def _one_shot_repair_execution_plan(
    task: TaskBook,
    mat_run_dir: Path,
    out_dir: Path,
    gate_report: Dict[str, object],
) -> Dict[str, object]:
    config = _policy_repair_config()
    if not config.get("enabled", True):
        return {"attempted": False, "reason": "disabled"}
    if int(config.get("max_repair_attempts") or 1) < 1:
        return {"attempted": False, "reason": "max_repair_attempts=0"}
    if str(config.get("repair_scope") or "") != "execution_plan":
        return {"attempted": False, "reason": "repair_scope_not_execution_plan"}
    failure_type = str(gate_report.get("failure_type") or "")
    repairable = set(str(item) for item in config.get("repairable_failure_types") or [])
    blocked = set(str(item) for item in config.get("non_repairable_failure_types") or [])
    if failure_type in blocked:
        return {"attempted": False, "reason": f"non_repairable:{failure_type}", "failure_type": failure_type}
    if failure_type not in repairable:
        return {"attempted": False, "reason": f"not_in_repairable_policy:{failure_type}", "failure_type": failure_type}
    contract = failure_contract(failure_type)
    if not contract.get("repairable", False) or contract.get("user_action_required") or contract.get("codex_recommended"):
        return {"attempted": False, "reason": f"taxonomy_blocks_repair:{failure_type}", "failure_type": failure_type}

    plan_path = Path(str(gate_report.get("plan_path") or ""))
    if not plan_path.exists():
        plan_path = next((path for path in _candidate_execution_plan_paths(mat_run_dir) if path.exists()), Path())
    if not plan_path or not plan_path.exists():
        return {"attempted": False, "reason": "execution_plan_not_found", "failure_type": failure_type}

    before = plan_path.read_text(encoding="utf-8-sig")
    changes: List[str] = []
    repaired_text: str | None = None
    try:
        payload = _load_execution_plan(plan_path)
    except Exception:
        if failure_type != "plan_parse_error":
            return {"attempted": False, "reason": "parse_failed_for_non_parse_failure", "failure_type": failure_type}
        repaired_text = _repair_plan_json_syntax(before)
        if repaired_text is None:
            return {"attempted": False, "reason": "syntax_repair_failed", "failure_type": failure_type}
        payload = json.loads(repaired_text)
        changes.append("repaired narrow JSON syntax")
    repaired_payload, payload_changes = _repair_execution_plan_payload(payload)
    changes.extend(payload_changes)
    if not changes:
        return {"attempted": False, "reason": "no_safe_plan_repair_available", "failure_type": failure_type}
    if any(str(error).lower().find("requires allow_") >= 0 or "without allow_" in str(error).lower() for error in gate_report.get("errors") or []):
        return {"attempted": False, "reason": "would_require_approval_change", "failure_type": failure_type}

    plan_path.write_text(json.dumps(repaired_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    repair_report = {
        "schema_version": 1,
        "attempted": True,
        "task_id": task.task_id,
        "repair_model": str(config.get("repair_model") or "qwen3-coder-next"),
        "repair_scope": "execution_plan",
        "failure_type": failure_type,
        "changes": changes,
        "input_plan": str(plan_path),
        "codex_fallback": False,
        "approval_changes": False,
        "real_world_state_changes": False,
        "repaired_at": now_iso(),
    }
    execution_dir = out_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    (execution_dir / "execution_plan_repair.json").write_text(
        json.dumps(repair_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return repair_report


def _execution_semantic_errors(task: TaskBook, script_id: str) -> List[str]:
    """Catch obvious task/script mismatches before execution_port runs.

    This gate is intentionally shallow and contract-based. It does not try to
    solve the task; it only blocks cases where the requested output family makes
    the chosen script impossible, such as using a metadata audit script for a
    metadata-only classifier task.
    """
    if not script_id:
        return []
    outputs = [item.strip().strip("`") for item in _execution_output_names_from_task(task) if _is_file_output_name(item)]
    entry = _script_entry_from_registry(script_id)
    expected = entry.get("expected_outputs", {})
    declared_files = expected.get("required_files", []) if isinstance(expected, dict) else []
    # Explicit file contracts also cover shared outputs such as worker_notes.md.
    declared_files = set(declared_files) if isinstance(declared_files, list) else set()
    errors: List[str] = []
    for prefix, allowed_scripts in sorted(_output_script_allowlist().items(), key=lambda item: len(item[0]), reverse=True):
        matched = [name for name in outputs if name.startswith(prefix) and name not in declared_files]
        if matched and script_id not in allowed_scripts:
            if _script_writes_artifact_family(script_id, "contract_artifact") and all(_is_contract_artifact_output(name) for name in matched):
                continue
            errors.append(
                f"script_id {script_id!r} cannot satisfy required output family {prefix!r}; "
                f"expected one of {allowed_scripts}; matched outputs: {', '.join(matched[:5])}"
            )
    return errors


def _execution_plan_gate(task: TaskBook, mat_run_dir: Path, out_dir: Path, spec_path: Path | None = None) -> Dict[str, object]:
    """Validate the model-proposed execution plan before execution_port runs it.

    The gate is intentionally conservative: the model can propose a bounded
    execution, but full recursive dataset scans, opening all images, or copying
    source data require explicit task-level approval.
    """
    if not _task_requires_execution_plan(task):
        return {"ok": True, "required": False, "errors": [], "plan_path": None}

    plan_path = next((path for path in _candidate_execution_plan_paths(mat_run_dir) if path.exists()), None)
    if plan_path is None:
        failure_type = "plan_schema_error"
        message = "missing execution_plan.json; materialized_execution requires a model-written plan before execution"
        return {
            "ok": False,
            "required": True,
            "errors": [message],
            "failure_types": [failure_type],
            **_typed_failure_payload(failure_type, message),
            "plan_path": None,
        }

    try:
        plan = _load_execution_plan(plan_path)
    except Exception as exc:
        failure_type = "plan_parse_error"
        message = f"invalid execution plan: {type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "required": True,
            "errors": [message],
            "failure_types": [failure_type],
            **_typed_failure_payload(failure_type, message),
            "plan_path": str(plan_path),
        }

    spec_script_id = ""
    if spec_path is not None and spec_path.exists():
        try:
            spec_probe = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
            spec_command = spec_probe.get("command") if isinstance(spec_probe, dict) and isinstance(spec_probe.get("command"), dict) else {}
            spec_script_id = str(spec_command.get("script_id") or "").strip()
        except Exception:
            spec_script_id = ""
    access = plan.get("data_access") if isinstance(plan.get("data_access"), dict) else {}
    read_paths = plan.get("read_paths") if isinstance(plan.get("read_paths"), list) else []
    if _script_default_execution_kind(spec_script_id) == "contract_artifact_writer":
        # The writer spec is a safe synthesized handoff: it writes a bounded
        # report from the contract_artifact_request created by trusted local
        # code. Do not let unsafe model-proposed read_paths in the raw plan
        # block this downgraded writer execution.
        access = {}
        read_paths = []
    errors: List[str] = []
    failure_types: List[str] = []

    def deny(failure_type: str, message: str) -> None:
        errors.append(message)
        if failure_type not in failure_types:
            failure_types.append(failure_type)

    for key, expected in (("read_paths", "list"), ("write_paths", "list"), ("data_access", "object")):
        value = plan.get(key)
        if (expected == "list" and not isinstance(value, list)) or (expected == "object" and not isinstance(value, dict)):
            deny("plan_schema_error", f"execution plan {key} must be a {expected}")

    gate_policy = _policy_gate_config()
    header_flag = _policy_approval(gate_policy, "image_header_scan", "allow_image_header_scan")
    pixel_flag = _policy_approval(gate_policy, "image_pixel_access", "allow_image_pixel_access")
    recursive_flag = _policy_approval(gate_policy, "recursive_dataset_scan", "allow_recursive_dataset_scan")
    bounded_flag = _policy_approval(gate_policy, "bounded_raw_dataset_discovery", "allow_bounded_raw_dataset_discovery")
    copy_flag = _policy_approval(gate_policy, "data_copy", "allow_data_copy")
    modify_flag = _policy_approval(gate_policy, "dataset_modification", "allow_dataset_modification")
    heavy_flag = _policy_approval(gate_policy, "heavy_data_scan", "allow_heavy_data_scan")
    allow_heavy = _task_flag(task, "allow_heavy_data_scan", False)
    allow_recursive = _task_flag(task, recursive_flag, False)
    allow_image_headers = _task_flag(task, header_flag, False)
    allow_image_pixels = _task_flag(task, pixel_flag, False)
    allow_bounded_discovery = _task_flag(task, bounded_flag, False)
    allow_data_copy = _task_flag(task, copy_flag, False)
    allow_dataset_modification = _task_flag(task, modify_flag, False)
    defaults = gate_policy.get("defaults", {}) if isinstance(gate_policy.get("defaults"), dict) else {}
    max_files_default = _safe_int(defaults.get("max_planned_read_files_without_approval"), 5000)
    max_files = _safe_int((task.raw_front_matter or {}).get("max_planned_read_files_without_approval"), max_files_default)
    access_fields = gate_policy.get("data_access_fields", {}) if isinstance(gate_policy.get("data_access_fields"), dict) else {}

    if bool(access.get("copies_or_moves_data")) and not allow_data_copy:
        deny("gate_denied_missing_approval", f"execution plan copies_or_moves_data=true without {copy_flag}=true")
    if bool(access.get("modifies_datasets")) and not allow_dataset_modification:
        deny("gate_denied_missing_approval", f"execution plan modifies_datasets=true without {modify_flag}=true")
    for field in _policy_list(gate_policy, "data_access_fields", "recursive_dataset_scan") or {"full_recursive_dataset_scan"}:
        if bool(access.get(field)) and not allow_recursive:
            deny("gate_denied_missing_approval", f"execution plan {field}=true without {recursive_flag}=true")
    for field in _policy_list(gate_policy, "data_access_fields", "image_header_scan") or {"opens_image_files"}:
        if bool(access.get(field)) and not allow_image_headers:
            deny("gate_denied_missing_approval", f"execution plan {field}=true without {header_flag}=true")
    for field in _policy_list(gate_policy, "data_access_fields", "image_pixel_access") or {"opens_image_pixels"}:
        if bool(access.get(field)) and not allow_image_pixels:
            deny("gate_denied_missing_approval", f"execution plan {field}=true without {pixel_flag}=true")
    for field in _policy_list(gate_policy, "data_access_fields", "bounded_raw_dataset_discovery") or {"bounded_raw_dataset_discovery"}:
        if bool(access.get(field)) and not allow_bounded_discovery:
            deny("gate_denied_missing_approval", f"execution plan {field}=true without {bounded_flag}=true")

    header_kinds = _policy_list(gate_policy, "read_path_kinds", "image_header_scan") or {"image_headers", "image_files"}
    pixel_kinds = _policy_list(gate_policy, "read_path_kinds", "image_pixel_access") or {"image_pixels", "pixel_data"}
    recursive_kinds = _policy_list(gate_policy, "read_path_kinds", "recursive_dataset_scan") or {"directory_scan", "image_headers", "image_files", "image_pixels", "dataset_root"}
    bounded_discovery_kinds = _policy_list(gate_policy, "read_path_kinds", "bounded_raw_dataset_discovery") or {"directory_scan", "dataset_root"}

    for index, item in enumerate(read_paths):
        if not isinstance(item, dict):
            deny("plan_schema_error", f"read_paths[{index}] must be an object")
            continue
        kind = str(item.get("kind") or "").strip().lower()
        recursive = bool(item.get("recursive", False))
        estimated_raw = item.get("estimated_files")
        try:
            estimated = int(estimated_raw) if estimated_raw not in ("", None) else None
        except (TypeError, ValueError):
            estimated = None
        if kind in pixel_kinds and not allow_image_pixels:
            deny("gate_denied_missing_approval", f"read_paths[{index}] requests image pixel access without {pixel_flag}=true")
        if kind in header_kinds and not allow_image_headers:
            deny("gate_denied_missing_approval", f"read_paths[{index}] requests image file/header access without {header_flag}=true")
        if recursive and kind in recursive_kinds and not allow_recursive:
            deny("gate_denied_missing_approval", f"read_paths[{index}] is recursive over {kind or 'data'} without {recursive_flag}=true")
        if recursive and kind in bounded_discovery_kinds and estimated is not None and estimated <= max_files and not allow_bounded_discovery:
            deny("gate_denied_missing_approval", f"read_paths[{index}] performs bounded raw dataset discovery without {bounded_flag}=true")
        if recursive and estimated is None and not allow_recursive:
            deny("plan_schema_error", f"read_paths[{index}] is recursive but missing estimated_files")
        if estimated is not None and estimated > max_files:
            if recursive and estimated_raw is not None and not allow_heavy:
                deny(
                    "gate_denied_heavy_read",
                    f"read_paths[{index}] estimates {estimated} files, above "
                    f"max_planned_read_files_without_approval={max_files}; raise the task field or use {heavy_flag}=true only for dangerous/unbounded scans"
                )

    if spec_path is not None and spec_path.exists():
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
            command = spec.get("command") if isinstance(spec, dict) and isinstance(spec.get("command"), dict) else {}
            script_id = str(command.get("script_id") or "").strip()
            args = command.get("args") if isinstance(command.get("args"), dict) else {}
            output_dir = str(args.get("output_dir") or "").strip()
            if output_dir:
                remote_task_dir = out_dir.parent
                try:
                    output_path = Path(output_dir)
                    if not output_path.is_absolute():
                        output_path = remote_task_dir / output_path
                    output_path.resolve().relative_to(remote_task_dir.resolve())
                except ValueError:
                    deny("path_policy_violation", "command.args.output_dir must be under remote_task_dir")
        except Exception as exc:
            deny("plan_schema_error", f"execution_spec.yaml could not be inspected by plan gate: {type(exc).__name__}: {exc}")
            script_id = ""
        registry_entry = _script_entry_from_registry(script_id)
        required_approvals = registry_entry.get("required_approvals", [])
        if not isinstance(required_approvals, list):
            required_approvals = []
        for flag in [str(item) for item in required_approvals]:
            if not _task_flag(task, flag, False):
                deny("gate_denied_missing_approval", f"script_id {script_id!r} requires {flag}=true")
        for error in _execution_semantic_errors(task, script_id):
            deny("arg_policy_violation", error)
    primary_failure = failure_types[0] if failure_types else None
    typed = _typed_failure_payload(primary_failure, errors[0] if errors else "")

    report = {
        "schema_version": 1,
        "task_id": task.task_id,
        "required": True,
        "ok": not errors,
        "plan_path": str(plan_path),
        "errors": errors,
        "failure_types": failure_types,
        **typed,
        "checked_at": now_iso(),
    }
    execution_dir = out_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    _copy_file(plan_path, execution_dir / "execution_plan.json")
    (execution_dir / "execution_plan_gate.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _replace_prefix(value: object, old_prefix: str, new_prefix: str) -> object:
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        old = old_prefix.replace("\\", "/").rstrip("/")
        if old and normalized.startswith(old + "/"):
            return new_prefix.rstrip("/") + normalized[len(old):]
        return value
    if isinstance(value, list):
        return [_replace_prefix(item, old_prefix, new_prefix) for item in value]
    if isinstance(value, dict):
        return {key: _replace_prefix(item, old_prefix, new_prefix) for key, item in value.items()}
    return value


def _normalize_materialized_execution_spec(spec_path: Path, workspace_root: Path, out_dir: Path, task: TaskBook) -> None:
    if not spec_path.exists():
        return
    try:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    run_dir = out_dir.parent
    default_output_dir = run_dir / "artifacts"
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    workspace["root"] = str(workspace_root)
    workspace["cwd"] = str(workspace_root)
    roots: List[str] = []
    for value in workspace.get("artifact_roots") or []:
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)
    for value in (workspace_root, run_dir.parent, run_dir, default_output_dir):
        text = str(value)
        if text and text not in roots:
            roots.append(text)
    workspace["artifact_roots"] = roots
    data["workspace"] = workspace

    command = data.get("command") if isinstance(data.get("command"), dict) else {}
    args = command.get("args") if isinstance(command.get("args"), dict) else {}
    script_id = str(command.get("script_id") or "")
    registry_entry = _script_entry_from_registry(script_id)
    normalizations = _resolve_workplace_asset_roles(command, workspace_root, task, registry_entry)
    if normalizations:
        data.setdefault("solution_agent_normalizations", [])
        if isinstance(data["solution_agent_normalizations"], list):
            data["solution_agent_normalizations"].extend(normalizations)
    existing_normalizations = data.get("solution_agent_normalizations")
    normalizer_synthesized = False
    if isinstance(existing_normalizations, list):
        normalizer_synthesized = any(
            isinstance(item, dict) and item.get("type") == "default_execution_spec_from_task_intent"
            for item in existing_normalizations
        )
    output_dir = str(args.get("output_dir") or "").replace("\\", "/")
    if not output_dir or "/materializer/attempts/" in output_dir:
        old_prefix = output_dir
        args["output_dir"] = str(default_output_dir)
        if old_prefix:
            data = _replace_prefix(data, old_prefix, str(default_output_dir))
            command = data.get("command") if isinstance(data.get("command"), dict) else command
            args = command.get("args") if isinstance(command.get("args"), dict) else args
            args["output_dir"] = str(default_output_dir)
    default_execution = registry_entry.get("default_execution", {}) if isinstance(registry_entry.get("default_execution"), dict) else {}
    if str(default_execution.get("kind") or "").strip() == "contract_artifact_writer":
        config_path = str(args.get("config") or "").replace("\\", "/")
        if not config_path or "/materializer/attempts/" in config_path or str(default_output_dir).replace("\\", "/") in config_path:
            generated_request = out_dir / "materializer" / "generated" / "contract_artifact_request.yaml"
            if generated_request.exists():
                args["config"] = str(generated_request)
    command["args"] = args
    data["command"] = command
    artifact_output_dir = Path(str(args.get("output_dir") or default_output_dir))

    output_names = [name for name in _execution_output_names_from_task(task) if _is_file_output_name(name)]
    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    if output_names and not (normalizer_synthesized and artifacts.get("required")):
        artifacts["required"] = [str(artifact_output_dir / name) for name in output_names]
        data["artifacts"] = artifacts
    expected_outputs = registry_entry.get("expected_outputs", {}) if isinstance(registry_entry, dict) else {}
    if isinstance(expected_outputs, dict) and not artifacts.get("required"):
        required_files = expected_outputs.get("required_files", [])
        if isinstance(required_files, list) and required_files:
            artifacts["required"] = [str(default_output_dir / str(name).strip()) for name in required_files if str(name).strip()]
            data["artifacts"] = artifacts
    closure_files = _closure_contract_files(registry_entry)
    if closure_files:
        required = list(artifacts.get("required") or [])
        base_dir = Path(str(args.get("output_dir") or default_output_dir))
        for filename in closure_files:
            path = str(base_dir / filename)
            if path not in required:
                required.append(path)
        artifacts["required"] = required
        data["artifacts"] = artifacts

    expected_metrics = registry_entry.get("expected_metrics", {}) if isinstance(registry_entry, dict) else {}
    if isinstance(expected_metrics, dict):
        success = data.get("success_criteria") if isinstance(data.get("success_criteria"), dict) else {}
        success["exit_code"] = int(success.get("exit_code") or 0)
        if not str(success.get("metrics_file") or "").strip():
            metrics_file = str(expected_metrics.get("file") or "").strip()
            if metrics_file:
                success["metrics_file"] = str(default_output_dir / metrics_file)
        keys = success.get("metric_keys")
        if not isinstance(keys, list) or not keys:
            required_keys = expected_metrics.get("required_keys", [])
            if isinstance(required_keys, list) and required_keys:
                success["metric_keys"] = [str(key) for key in required_keys if str(key).strip()]
        data["success_criteria"] = success

    spec_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _resolve_workplace_asset_roles(
    command: Dict[str, object],
    workspace_root: Path,
    task: TaskBook | None = None,
    registry_entry: Dict[str, object] | None = None,
) -> List[Dict[str, str]]:
    script_id = str(command.get("script_id") or "")
    args = command.get("args") if isinstance(command.get("args"), dict) else {}
    if not isinstance(args, dict):
        return []
    if registry_entry is None:
        registry_entry = _script_entry_from_registry(script_id)
    assets = _load_workplace_project_assets(workspace_root)
    known = assets.get("known_checkpoints") if isinstance(assets.get("known_checkpoints"), dict) else {}
    datasets = assets.get("datasets") if isinstance(assets.get("datasets"), dict) else {}
    replacements: List[Dict[str, str]] = []
    replacements.extend(_apply_numeric_execution_guards(args, task, registry_entry))
    replacements.extend(_apply_registry_asset_replacements(args, datasets, known, registry_entry))

    command["args"] = args
    return replacements


def _apply_registry_asset_replacements(
    args: Dict[str, object],
    datasets: Dict[str, object],
    known_checkpoints: Dict[str, object],
    registry_entry: Dict[str, object] | None,
) -> List[Dict[str, str]]:
    replacements: List[Dict[str, str]] = []
    if not isinstance(registry_entry, dict):
        return replacements
    policy = registry_entry.get("project_asset_replacements", {})
    if not isinstance(policy, dict):
        return replacements

    remove_args = policy.get("remove_args", {})
    if isinstance(remove_args, dict):
        for arg_name, reason in remove_args.items():
            key = str(arg_name)
            if key in args:
                removed = str(args.pop(key) or "")
                replacements.append({
                    "field": f"command.args.{key}",
                    "from": removed,
                    "to": "<removed>",
                    "reason": str(reason or "registry_project_asset_replacement"),
                })

    checkpoint_rules = policy.get("checkpoint_replacements", [])
    if isinstance(checkpoint_rules, list):
        for raw_rule in checkpoint_rules:
            if not isinstance(raw_rule, dict):
                continue
            arg = str(raw_rule.get("arg") or "").strip()
            target_role = str(raw_rule.get("replace_with_known_checkpoint") or "").strip()
            if not arg or not target_role:
                continue
            current = str(args.get(arg) or "").strip()
            target_value = str(known_checkpoints.get(target_role) or "").strip()
            if not current or not target_value or Path(current).exists():
                continue
            contains = tuple(token.lower() for token in _list_of_strings(raw_rule.get("when_missing_and_contains")))
            lowered = current.lower()
            if contains and not any(token in lowered for token in contains):
                continue
            args[arg] = target_value
            replacements.append({
                "field": f"command.args.{arg}",
                "from": current,
                "to": target_value,
                "reason": str(raw_rule.get("reason") or f"registry_known_checkpoint.{target_role}"),
            })

    dataset_rules = policy.get("dataset_replacements", [])
    if isinstance(dataset_rules, list):
        for raw_rule in dataset_rules:
            if not isinstance(raw_rule, dict):
                continue
            arg = str(raw_rule.get("arg") or "").strip()
            source_asset = str(raw_rule.get("when_arg_equals_project_asset") or "").strip()
            target_asset = str(raw_rule.get("replace_with_project_asset") or "").strip()
            if not arg or not target_asset:
                continue
            current = str(args.get(arg) or "").strip()
            target_value = str(datasets.get(target_asset) or "").strip()
            if not current or not target_value:
                continue
            source_value = str(datasets.get(source_asset) or "").strip()
            contains = tuple(token.lower() for token in _list_of_strings(raw_rule.get("when_missing_and_contains")))
            if source_value:
                if current.rstrip("/\\") != source_value.rstrip("/\\"):
                    continue
            elif contains:
                if Path(current).exists() or not any(token in current.lower() for token in contains):
                    continue
            else:
                continue
            min_checks = raw_rule.get("when_min_arg", {})
            if isinstance(min_checks, dict):
                passed = True
                for check_arg, minimum in min_checks.items():
                    try:
                        actual = int(args.get(str(check_arg)) or 0)
                        required = int(minimum)
                    except (TypeError, ValueError):
                        passed = False
                        break
                    if actual < required:
                        passed = False
                        break
                if not passed:
                    continue
            args[arg] = target_value
            replacements.append({
                "field": f"command.args.{arg}",
                "from": current,
                "to": target_value,
                "reason": str(raw_rule.get("reason") or "registry_project_asset_replacement"),
            })

    value_rules = policy.get("value_replacements", [])
    if isinstance(value_rules, list):
        for raw_rule in value_rules:
            if not isinstance(raw_rule, dict):
                continue
            arg = str(raw_rule.get("arg") or "").strip()
            replacement = raw_rule.get("replace_with")
            if not arg or replacement is None:
                continue
            current = str(args.get(arg) or "").strip()
            if not current:
                continue
            contains = tuple(token.lower() for token in _list_of_strings(raw_rule.get("when_missing_path_contains")))
            if contains:
                if Path(current).exists() or not any(token in current.lower() for token in contains):
                    continue
            args[arg] = replacement
            replacements.append({
                "field": f"command.args.{arg}",
                "from": current,
                "to": str(replacement),
                "reason": str(raw_rule.get("reason") or "registry_value_replacement"),
            })
    return replacements


def _apply_numeric_execution_guards(
    args: Dict[str, object],
    task: TaskBook | None,
    registry_entry: Dict[str, object] | None = None,
) -> List[Dict[str, str]]:
    replacements: List[Dict[str, str]] = []
    policy = dict(_policy_numeric_config())
    script_policy = registry_entry.get("numeric_policy", {}) if isinstance(registry_entry, dict) else {}
    if isinstance(script_policy, dict):
        policy.update(script_policy)
    if not policy:
        return replacements
    for arg_name, raw_rule in policy.items():
        key = str(arg_name)
        if key not in args:
            continue
        if not isinstance(raw_rule, dict):
            continue
        try:
            value = int(args.get(key) or 0)
        except (TypeError, ValueError):
            continue
        cap = raw_rule.get("cap_without_flag")
        fallback = raw_rule.get("fallback_cap", raw_rule.get("default", cap))
        override_flag = str(raw_rule.get("override_flag") or "").strip()
        try:
            cap_value = int(cap) if cap is not None else None
            fallback_value = int(fallback) if fallback is not None else None
        except (TypeError, ValueError):
            continue
        allowed = _task_flag(task, override_flag, False) if task is not None and override_flag else False
        if cap_value is not None and fallback_value is not None and value > cap_value and not allowed:
            args[key] = fallback_value
            replacements.append({
                "field": f"command.args.{key}",
                "from": str(value),
                "to": str(fallback_value),
                "reason": f"numeric_policy_cap_requires_{override_flag or 'explicit_override'}",
            })
    return replacements


def _load_workplace_project_assets(workspace_root: Path) -> Dict[str, object]:
    context_path = workspace_root / ".codex-orchestrator" / "agent_context.yaml"
    if not context_path.exists():
        return {}
    try:
        payload = yaml.safe_load(context_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    assets = payload.get("project_assets")
    return assets if isinstance(assets, dict) else {}


def _execution_output_names_from_task(task: TaskBook) -> List[str]:
    front = task.raw_front_matter or {}
    value = front.get("required_outputs") or front.get("expected_outputs") or front.get("outputs")
    if isinstance(value, list):
        return [str(item).strip().strip("`") for item in value if str(item).strip()]
    in_section = False
    saw_bullet = False
    outputs: List[str] = []
    for raw in task.body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            is_required_outputs_heading = title == "required outputs" or title.startswith("required outputs ")
            if in_section and not is_required_outputs_heading:
                break
            in_section = is_required_outputs_heading
            saw_bullet = False
            continue
        if in_section and line.startswith("- "):
            item = line[2:].strip().strip("`")
            backtick_match = re.search(r"`([^`]+)`", line)
            if backtick_match:
                item = backtick_match.group(1).strip()
            elif " or " in item.lower():
                match = re.search(r"([A-Za-z0-9_.-]+\.(?:csv|json|yaml|yml|md|txt|jpg|jpeg|png))", item)
                if match:
                    item = match.group(1)
            if item:
                outputs.append(item)
                saw_bullet = True
        elif in_section and line and line != "```":
            match = re.search(r"([A-Za-z0-9_.-]+\.(?:csv|jsonl|json|yaml|yml|md|txt|jpg|jpeg|png))", line)
            if match:
                outputs.append(match.group(1).strip())
                saw_bullet = True
        elif in_section and saw_bullet and line:
            break
    return outputs


def _apply_workplace_output_defaults(execution: Dict[str, object], task: TaskBook, out_dir: Path) -> Dict[str, object]:
    normalized: Dict[str, object] = dict(execution)
    args = dict(normalized.get("args") or {})
    default_output_dir = out_dir.parent / "artifacts"
    if not str(args.get("output_dir") or "").strip():
        args["output_dir"] = str(default_output_dir)
    normalized["args"] = args
    if normalized.get("required_artifacts"):
        normalized_required: List[str] = []
        for item in normalized.get("required_artifacts") or []:
            text = str(item).strip()
            if not text:
                continue
            path = Path(text)
            if path.is_absolute() or "/" in text or "\\" in text:
                normalized_required.append(text)
            else:
                normalized_required.append(str(default_output_dir / text))
        normalized["required_artifacts"] = normalized_required
    else:
        required: List[str] = []
        for item in _execution_output_names_from_task(task):
            if "/" in item or "\\" in item:
                required.append(item)
            elif "." in item:
                required.append(str(default_output_dir / item))
        if required:
            normalized["required_artifacts"] = required
    sc = dict(normalized.get("success_criteria") or {})
    sc.setdefault("exit_code", 0)
    normalized["success_criteria"] = sc
    return normalized


def run_materialized_execution_task(
    *,
    task_path: Path,
    out_dir: Path,
    workspace_root: Path,
    materializer_root: Path,
    policy_path: Path | None = None,
    env_file: Path | None = None,
) -> Dict[str, object]:
    """Run materialized execution from an explicit task path and output dir."""
    from .retry_controller import RetryController
    from solution_agent.context_gateway import build_context_pack
    from solution_agent.execution_port.spec_builder import write_execution_spec

    task = load_task(task_path)
    task_id = task.task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    build_context_pack(
        task_id=task_id,
        task_path=task_path,
        repo_root=workspace_root,
        out_dir=out_dir,
        remote_worker="materialized_execution",
        workspace_profile=str(task.raw_front_matter.get("workspace_profile") or task.raw_front_matter.get("remote_profile") or "local"),
        remote_conda_env=str(task.raw_front_matter.get("remote_conda_env") or task.raw_front_matter.get("conda_env") or "") or None,
    )
    _pkg = Path(__file__).resolve().parent.parent
    policy_path = policy_path or _resolve_provider_policy(task, _pkg)
    env_file = env_file or (Path.home() / ".remote_materializer" / ".env")
    execution_block = task.raw_front_matter.get("execution") or {}
    has_direct_execution_block = isinstance(execution_block, dict) and bool(execution_block.get("script_id"))
    if bool(task.raw_front_matter.get("materialized_execution_bypass_materializer", False)) or has_direct_execution_block:
        if not has_direct_execution_block:
            return _write_materialized_execution_failure(
                task,
            out_dir,
            reason="materialized_execution_bypass_materializer was set, but task has no execution.script_id",
            mat_result={"status": "bypassed_invalid_execution"},
            failure_type="arg_policy_violation",
        )
        execution = _apply_workplace_output_defaults(dict(execution_block), task, out_dir)
        execution_dir = out_dir / "execution"
        execution_dir.mkdir(parents=True, exist_ok=True)
        materializer_dir = out_dir / "materializer"
        materializer_dir.mkdir(parents=True, exist_ok=True)
        spec_path = execution_dir / "execution_spec.yaml"
        write_execution_spec(
            execution=execution,
            task_id=task_id,
            task_type=str(task.raw_front_matter.get("task_type", "")),
            worktree=workspace_root,
            dest=spec_path,
        )
        (materializer_dir / "result_package.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "status": "direct_execution_block",
                    "reason": "task supplied an execution.script_id block; materializer API was not required",
                    "execution_spec": "execution/execution_spec.yaml",
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        final = _run_execution_spec_for_worker(
            task_id,
            spec_path,
            out_dir,
            worker="materialized_execution",
        )
        result: Dict[str, object] = {
            **final,
            "worker": "materialized_execution",
            "materializer_status": "direct_execution_block",
            "retry_attempts": 0,
            "feedback_used": False,
            "result_yaml": str(out_dir / "result.yaml"),
        }
        write_status_json(out_dir / "status.json", result)
        return result
    retry_result = RetryController(
        task_id=task_id,
        task_path=task_path,
        mat_out=materializer_root,
        policy_path=policy_path,
        env_file=env_file,
        workspace_root=workspace_root,
        stop_repeated_failure=True,
    ).run_attempts()

    mat_result = retry_result.mat_result
    mat_run_dir = retry_result.final_attempt_dir
    if retry_result.raw_exc_info:
        scrubbed = _scrub_secrets(retry_result.raw_exc_info + "\n")
        (out_dir / "stderr.log").write_text(scrubbed, encoding="utf-8")
    if mat_run_dir.exists():
        _copy_materializer_trace(mat_run_dir, out_dir)

    mat_result = _try_synthesize_contract_writer_handoff(task, mat_run_dir, out_dir, workspace_root, mat_result)
    mat_result = _try_synthesize_registry_handoff(task, mat_run_dir, out_dir, workspace_root, mat_result)
    if mat_result.get("contract_writer_handoff_synthesized") and mat_run_dir.exists():
        _copy_materializer_trace(mat_run_dir, out_dir)
    if mat_result.get("registry_handoff_synthesized") and mat_run_dir.exists():
        _copy_materializer_trace(mat_run_dir, out_dir)

    claim_limitation = _scale_gate_claim_limitation(task, mat_run_dir)

    mat_status = str(mat_result.get("status") or "escalation_required")
    if mat_status != "materialized":
        return _write_materialized_execution_failure(
            task,
            out_dir,
            reason=f"materializer did not produce executable materials: {mat_status}",
            mat_result=mat_result,
            mat_run_dir=mat_run_dir,
        )

    spec_path = mat_run_dir / "execution_spec.yaml"
    if not spec_path.exists():
        return _write_materialized_execution_failure(
            task,
            out_dir,
            reason="materializer succeeded but did not produce execution_spec.yaml",
            mat_result=mat_result,
            mat_run_dir=mat_run_dir,
            failure_type="plan_schema_error",
        )

    execution_spec = out_dir / "execution" / "execution_spec.yaml"
    _copy_file(spec_path, execution_spec)
    _normalize_materialized_execution_spec(execution_spec, workspace_root, out_dir, task)

    plan_gate = _execution_plan_gate(task, mat_run_dir, out_dir, execution_spec)
    repair_report = _one_shot_repair_execution_plan(task, mat_run_dir, out_dir, plan_gate)
    if repair_report.get("attempted"):
        repaired_gate = _execution_plan_gate(task, mat_run_dir, out_dir, execution_spec)
        repaired_gate["one_shot_repair"] = repair_report
        plan_gate = repaired_gate
    if not plan_gate.get("ok", False):
        return _write_materialized_execution_failure(
            task,
            out_dir,
            reason="execution plan gate rejected materialized execution: " + "; ".join(str(item) for item in plan_gate.get("errors") or []),
            mat_result={
                **mat_result,
                "status": "execution_plan_rejected",
                "execution_plan_gate": plan_gate,
            },
            mat_run_dir=mat_run_dir,
        )

    final = _run_execution_spec_for_worker(
        task_id,
        execution_spec,
        out_dir,
        worker="materialized_execution",
    )
    result: Dict[str, object] = {
        **final,
        "worker": "materialized_execution",
        "materializer_status": mat_status,
        "retry_attempts": retry_result.attempts,
        "feedback_used": retry_result.feedback_used,
        "result_yaml": str(out_dir / "result.yaml"),
    }
    result = _apply_claim_limitation_notice(result, out_dir, claim_limitation)
    write_status_json(out_dir / "status.json", result)
    return result


def _run_worker_execution_port(task_id: str) -> Dict[str, object]:
    """
    Route a prepared run task through the deterministic execution port.

    Looks for execution_spec.yaml in (priority order):
      1. worker_out_dir(task_id)/execution_spec.yaml  鈫?written by small_task_worker
      2. worktree_dir(task_id)/execution_spec.yaml    鈫?written by materializer
      3. Generated on-the-fly from task.md execution: block + project_root worktree

    Runs register 鈫?run 鈫?monitor_loop 鈫?collect 鈫?writes _worker_out/result.yaml.
    """
    from solution_agent.execution_port.spec_builder import write_execution_spec
    from solution_agent.utils.paths import project_root

    # 鈹€鈹€ Locate or generate execution_spec.yaml 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    candidates = [
        worker_out_dir(task_id) / "execution_spec.yaml",
        worktree_dir(task_id) / "execution_spec.yaml",
    ]
    spec_path = next((p for p in candidates if p.exists()), None)

    if spec_path is None:
        # Try generating from task.md execution: block
        _, task_path = find_task(task_id)
        task = load_task(task_path)
        ex = task.raw_front_matter.get("execution") or {}
        if not ex.get("script_id"):
            raise ValueError(
                f"task {task_id!r}: no execution_spec.yaml found and no execution.script_id "
                "in task.md 鈥?cannot route to execution_port"
            )
        spec_path = worktree_dir(task_id) / "execution_spec.yaml"
        worktree_dir(task_id).mkdir(parents=True, exist_ok=True)
        write_execution_spec(
            execution=ex,
            task_id=task_id,
            task_type=str(task.raw_front_matter.get("task_type", "")),
            worktree=project_root(),
            dest=spec_path,
        )

    # 鈹€鈹€ Execute via execution_port pipeline 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    out_dir = worker_out_dir(task_id)
    return _run_execution_spec_for_worker(task_id, spec_path, out_dir, worker="execution_port")


def run_worker(task_id: str, worker: str = "codex") -> Dict[str, object]:
    if worker == "remote_materializer":
        return _run_worker_with_materializer_fallback(task_id)
    if worker == "materialized_execution":
        return _run_worker_materialized_execution(task_id)
    if worker == "small_task_worker":
        from solution_agent.small_task_worker import run_small_task_worker
        from solution_agent.utils.paths import project_root
        _, task_path = find_task(task_id)
        return run_small_task_worker(
            task_path=task_path,
            worktree=project_root(),
            out_dir=worker_out_dir(task_id),
        )
    if worker == "execution_port":
        return _run_worker_execution_port(task_id)
    if worker != "codex":
        raise ValueError(f"unsupported worker: {worker}")
    _, task_path = find_task(task_id)
    prompt_path = dispatch_dir(task_id) / "worker_prompt.md"
    if not prompt_path.exists():
        task = load_task(task_path)
        dispatch_dir(task_id).mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(build_worker_prompt(task, task_path.read_text(encoding="utf-8")), encoding="utf-8")
    else:
        task = load_task(task_path)
    out_dir = worker_out_dir(task_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_bytes = prompt.encode("utf-8")
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    budget_payload = task.raw_front_matter.get("model_budget")
    budget_payload = budget_payload if isinstance(budget_payload, dict) else {}
    budget = PhaseBudget(
        max_model_requests=int(budget_payload.get("max_model_requests", 1)),
        max_corrective_model_requests=int(budget_payload.get("max_corrective_model_requests", 0)),
        max_context_estimated_tokens_per_request=int(
            budget_payload.get("max_context_estimated_tokens_per_request", 24000)
        ),
    )
    context_estimate = conservative_token_estimate(prompt)
    fingerprint = model_request_fingerprint(
        provider="codex",
        model="codex-cli",
        phase="execute",
        payload_sha256=prompt_sha256,
    )
    try:
        permit = admit_model_dispatch(
            out_dir,
            run_id=task_id,
            provider="codex",
            model="codex-cli",
            phase="execute",
            fingerprint=fingerprint,
            budget=budget,
            context_estimated_tokens=context_estimate,
        )
    except ModelDispatchDenied as exc:
        blocked = {
            "status": "budget_review_required",
            "returncode": 75,
            "worker": "codex",
            "model_dispatched": False,
            "reason": exc.reason,
            "budget_decision": exc.decision,
            "prompt_sha256": prompt_sha256,
            "context_estimated_tokens": context_estimate,
            "next_action": "Revise the declared model budget or materially change the task/spec before another model request.",
        }
        write_status_json(out_dir / "status.json", blocked)
        return blocked

    try:
        UsageLedger(out_dir, run_id=task_id).append(
            UsageEvent(
                usage_event_id=f"model-dispatch:{permit.request_id}",
                run_id=task_id,
                side="local_controller",
                phase="execute",
                operation="model_request",
                provider="codex",
                requested_model="codex-cli",
                reasoning_class="unknown",
                usage_source="unavailable",
                model_turn_delta=1,
                context_estimated_tokens=context_estimate,
            )
        )
    except Exception:
        complete_model_dispatch(permit, status="failed", returncode=None)
        raise

    try:
        result = run_codex(prompt, worktree_dir(task_id))
    except Exception:
        complete_model_dispatch(permit, status="failed", returncode=None)
        raise
    returncode = int(result.get("returncode", 1) or 0)
    complete_model_dispatch(
        permit,
        status="succeeded" if returncode == 0 else "failed",
        returncode=returncode,
    )
    request_dir = out_dir / "model_requests" / permit.request_id
    request_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = request_dir / "stdout.log"
    stderr_path = request_dir / "stderr.log"
    stdout_path.write_text(str(result.get("stdout", "")), encoding="utf-8")
    stderr_path.write_text(str(result.get("stderr", "")), encoding="utf-8")
    canonical_stdout = out_dir / "stdout.log"
    canonical_stderr = out_dir / "stderr.log"
    # Preserve the historical first-attempt locations for existing collectors.
    # Explicit later requests retain separate immutable streams by request ID.
    if not canonical_stdout.exists():
        _copy_file(stdout_path, canonical_stdout)
    if not canonical_stderr.exists():
        _copy_file(stderr_path, canonical_stderr)
    compact_result = {
        "status": str(result.get("status") or ("success" if returncode == 0 else "failed")),
        "returncode": returncode,
        "worker": "codex",
        "model_dispatched": True,
        "model_request_id": permit.request_id,
        "prompt_sha256": prompt_sha256,
        "context_estimated_tokens": context_estimate,
        "command": str(result.get("command") or "codex exec --skip-git-repo-check <prompt>"),
        "stdout": {
            "path": str(stdout_path),
            "bytes": stdout_path.stat().st_size,
            "sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
        },
        "stderr": {
            "path": str(stderr_path),
            "bytes": stderr_path.stat().st_size,
            "sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
        },
        "output_omitted": True,
        "next_action": "Use the persisted streams or an exact bounded evidence query only if the result requires diagnosis.",
    }
    write_status_json(out_dir / "status.json", compact_result)
    return compact_result


def run_next(
    worker: str = "codex",
    collect_after: bool = True,
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    task_id = next_pending_task_id(worker=worker, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    if task_id is None:
        return {
            "ran": False,
            "reason": "no eligible ready pending tasks",
            "selector": {"worker": worker, "line": line, "batch": batch, "dag_root": dag_root, "prefix": prefix},
        }
    dispatched = dispatch(task_id, worker=worker, submit=False)
    worker_result = run_worker(task_id, worker=worker)
    collected = collect(task_id) if collect_after else None
    return {"ran": True, "task_id": task_id, "dispatch": dispatched, "worker": worker_result, "collect": collected}


def run_queue(
    worker: str = "codex",
    limit: Optional[int] = None,
    stop_on_fail: bool = False,
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    runs: List[Dict[str, object]] = []
    count = 0
    while True:
        if limit is not None and count >= limit:
            break
        task_id = next_pending_task_id(worker=worker, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
        if task_id is None:
            break
        result = run_next(worker=worker, collect_after=True, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
        runs.append(result)
        count += 1
        collected = result.get("collect") or {}
        if stop_on_fail and isinstance(collected, dict) and collected.get("status") in {"failed", "partial"}:
            break
    return {"ran": len(runs), "runs": runs, "selector": {"worker": worker, "line": line, "batch": batch, "dag_root": dag_root, "prefix": prefix}}


def remote_bootstrap_context(host: str, remote_workdir: str) -> Dict[str, object]:
    base = remote_workdir.rstrip("/\\") + "/.codex-orchestrator"
    project_root = remote_workdir.rstrip("/\\")
    runtime_files = _remote_runtime_files()
    runtime_signature = _remote_runtime_signature(runtime_files)
    shared = f"""# Shared Project Context

## Project Root

{remote_workdir}

## Source Policy Files

Read these existing project files when present:

- {project_root}/AGENTS.md
- {project_root}/CLAUDE.md
- {project_root}/COMMAND_POLICY.md

## Operating Rule

Tasks are executed one by one by the local orchestrator. Codex should work in the project root and write task outputs to the absolute `_worker_out` path supplied in each task prompt.

## Continuity

Before starting a task, read:

- {base}/codex_worker_profile.md
- {base}/workflow_profile.md
- {base}/maintenance_policy_v1.md
- {base}/scheduler_optimization_notes.md
- {base}/opencoder_task_execution_guide.md
- {base}/inbox_task_format.md
- {base}/memory.md
- {base}/last_summary.md
- {base}/task_history.md
- {base}/file_index.md
"""
    ensure_local_memory_files()
    context_paths = {
        "last_summary": root() / "state" / "last_summary.md",
        "memory": root() / "state" / "memory.md",
        "task_history": root() / "state" / "task_history.md",
        "file_index": root() / "state" / "file_index.md",
        "codex_worker_profile": codex_worker_profile_path(),
        "workflow_profile": workflow_profile_path(),
        "maintenance_policy_v1": root() / "docs" / "MAINTENANCE_POLICY_V1.md",
        "scheduler_optimization_notes": root() / "docs" / "SCHEDULER_OPTIMIZATION_NOTES.md",
        "opencoder_task_execution_guide": root() / "docs" / "OPENCODER_TASK_EXECUTION_GUIDE.md",
        "inbox_task_format": root() / "docs" / "INBOX_TASK_FORMAT.md",
    }
    missing_context_files = [
        {"name": name, "path": str(path)}
        for name, path in sorted(context_paths.items())
        if not path.exists()
    ]
    if missing_context_files:
        return {
            "blocked": True,
            "reason": "remote_context_required_files_missing",
            "remote_context_dir": base,
            "remote_runtime_dir": base + "/runtime",
            "missing_context_files": missing_context_files,
        }
    digest = hashlib.sha1()
    digest.update(host.encode("utf-8"))
    digest.update(b"\0")
    digest.update(remote_workdir.encode("utf-8"))
    digest.update(b"\0")
    digest.update(shared.encode("utf-8"))
    for name, path in sorted(context_paths.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.exists() else b"")
        digest.update(b"\0")
    digest.update(b"runtime")
    digest.update(b"\0")
    digest.update(runtime_signature.encode("utf-8"))
    digest.update(b"\0")
    signature = digest.hexdigest()
    cache_path = state_dir() / "remote_bootstrap_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cache = payload
        except Exception:
            cache = {}
    cache_key = hashlib.sha1(f"{host}|{remote_workdir}".encode("utf-8")).hexdigest()
    cached = cache.get(cache_key) if isinstance(cache.get(cache_key), dict) else {}
    if cached.get("success") is True and cached.get("signature") == signature:
        return {
            "skipped": True,
            "reason": "remote context unchanged",
            "remote_context_dir": base,
            "remote_runtime_dir": base + "/runtime",
            "signature": signature,
            "runtime_version": REMOTE_RUNTIME_VERSION,
            "runtime_signature": runtime_signature,
            "cached_at": cached.get("updated_at"),
        }
    unique_suffix = uuid.uuid4().hex[:12]
    local_tmp = state_dir() / f"remote_shared_context_{unique_suffix}.md"
    local_tmp.write_text(shared, encoding="utf-8")
    try:
        bundle_files = {
            "shared_context.md": local_tmp,
            "last_summary.md": context_paths["last_summary"],
            "memory.md": context_paths["memory"],
            "task_history.md": context_paths["task_history"],
            "file_index.md": context_paths["file_index"],
            "codex_worker_profile.md": context_paths["codex_worker_profile"],
            "workflow_profile.md": context_paths["workflow_profile"],
            "maintenance_policy_v1.md": context_paths["maintenance_policy_v1"],
            "scheduler_optimization_notes.md": context_paths["scheduler_optimization_notes"],
            "opencoder_task_execution_guide.md": context_paths["opencoder_task_execution_guide"],
            "inbox_task_format.md": context_paths["inbox_task_format"],
        }
        bundle_files.update(runtime_files)
        context_bundle_path = _write_tar_gz(
            state_dir() / f"remote_context_{cache_key}_{signature[:12]}.tar.gz",
            bundle_files,
        )
    finally:
        if local_tmp.exists():
            local_tmp.unlink()
    remote_bundle = f"/tmp/solution_agent_context_{cache_key}_{signature[:12]}_{unique_suffix}.tar.gz"
    copy_bundle = scp_to_remote(context_bundle_path, host, remote_bundle)
    extract_bundle = run_ssh(host, f'mkdir -p "{base}" && tar -xzf "{remote_bundle}" -C "{base}" && rm -f "{remote_bundle}"')
    bootstrap_success = int(copy_bundle.get("returncode", 1) or 0) == 0 and int(extract_bundle.get("returncode", 1) or 0) == 0
    if bootstrap_success:
        cache[cache_key] = {
            "signature": signature,
            "runtime_signature": runtime_signature,
            "updated_at": now_iso(),
            "host": host,
            "remote_workdir": remote_workdir,
            "success": True,
        }
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "context_bundle": {
            "local": str(context_bundle_path),
            "remote": remote_bundle,
            "copy": copy_bundle,
            "extract": extract_bundle,
            "contains": [
                "shared_context.md",
                "last_summary.md",
                "memory.md",
                "task_history.md",
                "file_index.md",
                "codex_worker_profile.md",
                "workflow_profile.md",
                "maintenance_policy_v1.md",
                "scheduler_optimization_notes.md",
                "opencoder_task_execution_guide.md",
                "inbox_task_format.md",
                *sorted(runtime_files.keys()),
            ],
        },
        "mkdir": {"skipped": True, "reason": "handled by context bundle extract"},
        "copy_shared": {"skipped": True, "reason": "included in context bundle"},
        "copied": {"skipped": True, "reason": "included in context bundle"},
        "remote_context_dir": base,
        "remote_runtime_dir": base + "/runtime",
        "signature": signature,
        "runtime_version": REMOTE_RUNTIME_VERSION,
        "runtime_signature": runtime_signature,
    }


def _gpu_candidates(task: TaskBook) -> list[str]:
    raw = task.raw_front_matter
    gpu_ids = raw.get("nvidia_smi_ids_allowed") or raw.get("gpu_ids_allowed") or raw.get("allowed_gpu_ids")
    if isinstance(gpu_ids, list) and gpu_ids:
        return [str(item) for item in gpu_ids]
    value = raw.get("cuda_visible_devices")
    if value is not None and str(value).strip():
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return []


def _gpu_setup_script(
    task: TaskBook,
    gpu_candidates_override: Optional[List[str]] = None,
    min_free_gpu_mem_gb_override: Optional[float] = None,
) -> tuple[list[str], Optional[str], str, str, float]:
    raw = task.raw_front_matter
    resource_hint = str(raw.get("resource_hint", "")).lower()
    gpu_policy = str(raw.get("gpu_policy", "") or "use_available_or_specified")
    gpu_id_semantics = str(raw.get("gpu_id_semantics", "") or "nvidia_smi_physical_ids")
    min_free_gb = float(min_free_gpu_mem_gb_override if min_free_gpu_mem_gb_override is not None else (raw.get("min_free_gpu_mem_gb", 0) or 0))
    candidates = [str(item) for item in gpu_candidates_override] if gpu_candidates_override else _gpu_candidates(task)
    if resource_hint != "gpu" and not candidates:
        return ["true"], None, gpu_policy, gpu_id_semantics, min_free_gb
    candidate_text = " ".join(candidates)
    lines = [
        f'GPU_POLICY="{gpu_policy}"',
        f'GPU_ID_SEMANTICS="{gpu_id_semantics}"',
        f'GPU_CANDIDATES="{candidate_text}"',
        f'MIN_FREE_GPU_MEM_GB="{min_free_gb}"',
        'select_gpu() {',
        '  if ! command -v nvidia-smi >/dev/null 2>&1; then return 1; fi',
        '  nvidia-smi --query-gpu=index,memory.used,memory.total,memory.free,utilization.gpu,name --format=csv,noheader,nounits | awk -F, -v candidates="$GPU_CANDIDATES" -v min_free_gb="$MIN_FREE_GPU_MEM_GB" \'',
        '    BEGIN { split(candidates, allow, " "); for (i in allow) if (allow[i] != "") allowed[allow[i]]=1; best=""; best_score=999999999; }',
        '    {',
        '      gsub(/ /, "", $1); idx=$1; mem=$2+0; total=$3+0; free_mb=$4+0; util=$5+0; free_gb=free_mb/1024;',
        '      if (candidates != "" && !(idx in allowed)) next;',
        '      if (free_gb < min_free_gb) next;',
        '      score=mem*100+util;',
        '      if (score < best_score) { best_score=score; best=idx; }',
        '    }',
        '    END { if (best != "") print best; }',
        "  '",
        '}',
        'if [ "$GPU_POLICY" = "use_specified" ] && [ -n "$GPU_CANDIDATES" ]; then',
        '  SELECTED_GPU="$(printf "%s" "$GPU_CANDIDATES" | awk \'{print $1}\')"',
        'else',
        '  SELECTED_GPU="$(select_gpu || true)"',
        'fi',
        'if [ -n "${SELECTED_GPU:-}" ]; then',
        '  SELECTED_GPU_UUID="$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i "$SELECTED_GPU" 2>/dev/null | head -1 || true)"',
        '  export ORCHESTRATOR_NVIDIA_SMI_ID="$SELECTED_GPU"',
        '  if [ -n "$SELECTED_GPU_UUID" ]; then export CUDA_VISIBLE_DEVICES="$SELECTED_GPU_UUID"; else export CUDA_VISIBLE_DEVICES="$SELECTED_GPU"; fi',
        'fi',
    ]
    return lines, ",".join(candidates) if candidates else None, gpu_policy, gpu_id_semantics, min_free_gb


def _codex_exec_args(task: TaskBook) -> tuple[str, str, bool]:
    raw = task.raw_front_matter
    resource_hint = str(raw.get("resource_hint", "")).lower()
    default_sandbox = "danger-full-access" if resource_hint == "gpu" else "workspace-write"
    sandbox_mode = str(raw.get("codex_sandbox_mode") or raw.get("codex_sandbox") or default_sandbox)
    bypass = bool(raw.get("codex_bypass_approvals_and_sandbox", False))
    args = ["exec", "--skip-git-repo-check"]
    if bypass:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args.extend(["--sandbox", sandbox_mode])
    return " ".join(shlex.quote(item) for item in args), sandbox_mode, bypass


def run_remote(
    task_id: str,
    host: str,
    remote_root: str,
    worker: str = "codex",
    remote_workdir: Optional[str] = None,
    detach: bool = False,
    gpu_candidates_override: Optional[List[str]] = None,
    min_free_gpu_mem_gb_override: Optional[float] = None,
    execution_phase: str = "full",
    force_worker_override: bool = False,
    override_reason: str = "",
) -> Dict[str, object]:
    if worker not in {"codex", "materialized_execution"}:
        raise ValueError(f"unsupported remote worker: {worker}")
    current_status, task_path = find_task(task_id)
    admission_task = load_task(task_path)
    _assert_worker_admitted(
        admission_task,
        worker,
        force_worker_override=force_worker_override,
        override_reason=override_reason,
    )
    if current_status != "pending" and force_worker_override and _normalize_selector_value(worker) not in _task_allowed_workers(admission_task):
        _record_worker_override_event(admission_task, worker, override_reason)
    if current_status == "pending":
        dispatched = dispatch(
            task_id,
            worker=worker,
            submit=False,
            force_worker_override=force_worker_override,
            override_reason=override_reason,
        )
        _, task_path = find_task(task_id)
    elif current_status in {"partial", "failed", "launch_failed", "launch_unknown"}:
        running_dir = task_dir("running", task_id)
        if running_dir.exists():
            shutil.rmtree(running_dir)
        task_path.parent.rename(running_dir)
        task_path = running_dir / "task.md"
        update_task_record(
            task_id,
            status="running",
            worker=worker,
            started_at=now_iso(),
            finished_at=None,
            summary_short=f"Rerunning from {current_status}.",
        )
        dispatched = {"status": current_status, "rerun": True}
    else:
        dispatched = {"status": current_status}
    task = load_task(task_path)
    workplace_mismatch = _target_workplace_mismatch(task, host=host, remote_root=remote_root, remote_workdir=remote_workdir)
    if workplace_mismatch:
        raise ValueError(f"target_workplace_mismatch: {json.dumps(workplace_mismatch, ensure_ascii=False)}")
    existing_record = load_registry().get("tasks", {}).get(task_id, {})
    input_bindings = existing_record.get("input_bindings") if isinstance(existing_record, dict) else {}
    existing_remote = existing_record.get("remote") or {}
    is_existing_running = current_status == "running" and isinstance(existing_remote, dict) and existing_remote.get("run_id")
    attempt = int(existing_remote.get("attempt") or _next_attempt(task_id)) if is_existing_running else _next_attempt(task_id)
    run_id = str(existing_remote.get("run_id") or _make_run_id(task_id, attempt)) if is_existing_running else _make_run_id(task_id, attempt)
    task_remote_root = str(remote_root)
    remote_task_dir = task_remote_root.rstrip("/\\") + f"/{run_id}"
    remote_lock_root = task_remote_root.rstrip("/\\") + "/.locks"
    remote_workdir = remote_workdir or remote_task_dir
    remote_out_dir = remote_task_dir + "/_worker_out"
    remote_prompt = remote_task_dir + "/worker_prompt.md"
    remote_prompt_path = dispatch_dir(task_id) / "worker_prompt.remote.md"
    input_bindings_path = dispatch_dir(task_id) / "input_bindings.yaml"
    input_bindings_path.write_text(yaml.safe_dump(input_bindings or {}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    prompt_text = build_worker_prompt(
        task,
        task_path.read_text(encoding="utf-8"),
        output_dir=remote_out_dir,
        context_files=remote_context_paths(remote_workdir),
        phase=execution_phase,
    )
    if input_bindings:
        prompt_text += (
            "\n\n## Bound input artifacts\n\n"
            "The local orchestrator has resolved upstream artifact dependencies for this run. "
            "Use the pinned run ids and paths below; do not switch to a parent task's latest run unless the task book explicitly says so.\n\n"
            "```yaml\n"
            + yaml.safe_dump(input_bindings, sort_keys=False, allow_unicode=True)
            + "```\n"
    ) 
    runtime_budget_hours = float(task.raw_front_matter.get("runtime_budget_hours", 2) or 2)
    timeout_seconds = max(300, int(runtime_budget_hours * 3600) + 900)
    remote_prompt_path.write_text(prompt_text, encoding="utf-8")
    bootstrap = remote_bootstrap_context(host, remote_workdir)
    if bootstrap.get("blocked"):
        bootstrap_reason = str(bootstrap.get("reason") or "bootstrap blocked")
        launch_blocked_reason = f"bootstrap_blocked: {bootstrap_reason}"
        launch_result = {
            "status": "blocked",
            "returncode": 1,
            "stdout": "",
            "stderr": bootstrap_reason,
            "command": "skipped due to bootstrap block",
            "host": host,
        }
        update_task_record(
            task_id,
            status="launching",
            task_status="launching",
            remote_health="degraded",
            run_phase="launching",
            remote_state="launching",
            last_remote_seen_at=now_iso(),
            remote={
                "host": host,
                "remote_root": task_remote_root,
                "remote_task_dir": remote_task_dir,
                "remote_workdir": remote_workdir,
                "run_id": run_id,
                "attempt": attempt,
                "timeout_seconds": timeout_seconds,
                "execution_phase": execution_phase,
                "input_bindings": input_bindings or {},
            },
            remote_run={"bootstrap": bootstrap, "launch_bundle": {"local": None, "remote": None, "copy": None, "contains": []}},
            latest_run_id=run_id,
        )
        _upsert_run_record(
            task_id,
            {
                "task_id": task_id,
                "run_id": run_id,
                "attempt": attempt,
                "status": "launching",
                "task_status": "launching",
                "remote_health": "degraded",
                "run_phase": "launching",
                "state": "launching",
                "host": host,
                "remote_task_dir": remote_task_dir,
                "remote_out_dir": remote_out_dir,
                "launched_at": now_iso(),
                "gpu": {
                    "requested_nvidia_smi_ids": [],
                    "requested_physical_ids": [],
                    "cuda_visible_devices": None,
                    "process_visible_index": None,
                    "min_free_mem_gb": 0,
                },
            },
        )
        blocked_remote = {
            "host": host,
            "remote_root": task_remote_root,
            "remote_task_dir": remote_task_dir,
            "remote_workdir": remote_workdir,
            "run_id": run_id,
            "attempt": attempt,
            "timeout_seconds": timeout_seconds,
            "execution_phase": execution_phase,
            "input_bindings": input_bindings or {},
        }
        launch_started_at = now_iso()
        update_task_record(
            task_id,
            status="launch_failed",
            task_status="launch_failed",
            remote_health="degraded",
            run_phase="launch_failed",
            remote_state="launch_failed",
            last_remote_seen_at=launch_started_at,
            remote=blocked_remote,
            remote_run={
                "bootstrap": bootstrap,
                "launch_bundle": {"local": None, "remote": None, "copy": None, "contains": []},
                "mkdir": {"skipped": True, "reason": "bootstrap blocked"},
                "copy_prompt": {"skipped": True, "reason": "bootstrap blocked"},
                "copy_script": {"skipped": True, "reason": "bootstrap blocked"},
                "detach": detach,
                "run": launch_result,
                "launch_reason": launch_blocked_reason,
            },
            latest_run_id=run_id,
        )
        _upsert_run_record(
            task_id,
            {
                "task_id": task_id,
                "run_id": run_id,
                "attempt": attempt,
                "status": "launch_failed",
                "task_status": "launch_failed",
                "remote_health": "degraded",
                "run_phase": "launch_failed",
                "state": "launch_failed",
                "launch_reason": launch_blocked_reason,
                "host": host,
                "remote_task_dir": remote_task_dir,
                "remote_out_dir": remote_out_dir,
                "launched_at": launch_started_at,
                "gpu": {
                    "requested_nvidia_smi_ids": [],
                    "requested_physical_ids": [],
                    "cuda_visible_devices": None,
                    "process_visible_index": None,
                    "min_free_mem_gb": 0,
                },
            },
        )
        _move_task_folder(task_id, "launch_failed")
        return {
            "dispatch": dispatched,
            "run_id": run_id,
            "attempt": attempt,
            "bootstrap": bootstrap,
            "launch_bundle": {"local": None, "remote": None, "copy": None, "contains": []},
            "remote": blocked_remote,
            "mkdir": {"skipped": True, "reason": "bootstrap blocked"},
            "copy_prompt": {"skipped": True, "reason": "bootstrap blocked"},
            "copy_script": {"skipped": True, "reason": "bootstrap blocked"},
            "detach": detach,
            "run": launch_result,
        }
    gpu_setup_lines, gpu_candidates, gpu_policy, gpu_id_semantics, min_free_gb = _gpu_setup_script(
        task,
        gpu_candidates_override=gpu_candidates_override,
        min_free_gpu_mem_gb_override=min_free_gpu_mem_gb_override,
    )
    remote_execution_env = _resolve_remote_execution_env(task, host, remote_workdir)
    remote_auth_token = _get_or_create_remote_auth_token(host, remote_workdir)
    remote_auth_signature = _remote_auth_signature(remote_auth_token, run_id)
    materialized_py_cmd = _shell_join(
        list(remote_execution_env["command_prefix"])
        + [
            "-m",
            "solution_agent.orchestrator.materialized_runner",
            "--task-path",
            f"{remote_task_dir}/task.md",
            "--out-dir",
            remote_out_dir,
            "--workspace-root",
            remote_workdir,
            "--materializer-root",
            f"{remote_task_dir}/materializer",
        ]
    )
    codex_exec_args, codex_sandbox_mode, codex_bypass_sandbox = _codex_exec_args(task)
    script_path = dispatch_dir(task_id) / "remote_runner.sh"
    script_text = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -u",
            f'cd "{remote_workdir}"',
            *gpu_setup_lines,
            f'mkdir -p "{remote_out_dir}"',
            f'echo "$$" > "{remote_out_dir}/runner.pid"',
            f'ps -o pgid= "$$" | tr -d " " > "{remote_out_dir}/process_group.pid" 2>/dev/null || true',
            f'echo "${{ORCHESTRATOR_NVIDIA_SMI_ID:-}}" > "{remote_out_dir}/nvidia_smi_id.txt"',
            f'echo "${{CUDA_VISIBLE_DEVICES:-}}" > "{remote_out_dir}/cuda_visible_devices.txt"',
            f'echo "{gpu_policy}" > "{remote_out_dir}/gpu_policy.txt"',
            f'echo "{gpu_id_semantics}" > "{remote_out_dir}/gpu_id_semantics.txt"',
            f'echo "{min_free_gb}" > "{remote_out_dir}/min_free_gpu_mem_gb.txt"',
            f'echo "{codex_sandbox_mode}" > "{remote_out_dir}/codex_sandbox_mode.txt"',
            f'echo "{str(codex_bypass_sandbox).lower()}" > "{remote_out_dir}/codex_bypass_sandbox.txt"',
            f'echo "{run_id}" > "{remote_out_dir}/run_id.txt"',
            f'echo "{attempt}" > "{remote_out_dir}/attempt.txt"',
            f'LOCK_ROOT="{remote_lock_root}"',
            'mkdir -p "$LOCK_ROOT"',
            'if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then',
            '  SELECTED_GPU_UUID="$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i "$CUDA_VISIBLE_DEVICES" 2>/dev/null | head -1 || true)"',
            '  LOCK_KEY="${SELECTED_GPU_UUID:-gpu-${CUDA_VISIBLE_DEVICES}}"',
            '  LOCK_DIR="$LOCK_ROOT/${LOCK_KEY}.lock"',
            '  acquire_gpu_lock() {',
            '    if mkdir "$LOCK_DIR" 2>/dev/null; then return 0; fi',
            '    old_pid="$(python3 - "$LOCK_DIR/lock.json" <<\'PY\' 2>/dev/null || true\nimport json,sys\ntry: print(json.load(open(sys.argv[1])).get("pid",""))\nexcept Exception: pass\nPY\n)"',
            '    if [ -n "$old_pid" ] && ps -p "$old_pid" >/dev/null 2>&1; then return 1; fi',
            '    rm -rf "$LOCK_DIR"',
            '    mkdir "$LOCK_DIR" 2>/dev/null',
            '  }',
            '  if acquire_gpu_lock; then',
            f'    printf \'{{"schema_version":1,"task_id":"{task_id}","run_id":"{run_id}","attempt":{attempt},"pid":%s,"gpu_uuid":"%s","cuda_visible_devices":"%s","created_at":"%s","heartbeat_at":"%s"}}\\n\' "$$" "$SELECTED_GPU_UUID" "$CUDA_VISIBLE_DEVICES" "$(date -Is)" "$(date -Is)" > "$LOCK_DIR/lock.json"',
            f'    echo "$LOCK_DIR" > "{remote_out_dir}/gpu_lock_dir.txt"',
            f'    echo "$SELECTED_GPU_UUID" > "{remote_out_dir}/gpu_uuid.txt"',
            '  else',
            f'    echo "GPU lock is busy for $LOCK_KEY." > "{remote_out_dir}/stderr.log"',
            '    unset CUDA_VISIBLE_DEVICES',
            '  fi',
            'fi',
            f'rm -f "{remote_out_dir}/exit_code.txt" "{remote_out_dir}/finished_at.txt"',
            f': > "{remote_out_dir}/exit_code.txt"',
            f'cat > "{remote_out_dir}/state.json" <<\'JSON\'',
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "run_id": run_id,
                    "attempt": attempt,
                    "state": "running",
                    "phase": "preparing",
                    "exit_code": None,
                    "reason": None,
                    "updated_at": now_iso(),
                },
                ensure_ascii=False,
            ),
            "JSON",
            f'heartbeat_loop() {{ while true; do ts="$(date -Is)"; stdout_bytes="$(if [ -f "{remote_out_dir}/codex_stdout.log" ]; then wc -c < "{remote_out_dir}/codex_stdout.log"; else echo 0; fi)"; stderr_bytes="$(if [ -f "{remote_out_dir}/codex_stderr.log" ]; then wc -c < "{remote_out_dir}/codex_stderr.log"; else echo 0; fi)"; gpu_uuid="$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i "${{CUDA_VISIBLE_DEVICES:-0}}" 2>/dev/null | head -1 || true)"; gpu_mem="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "${{CUDA_VISIBLE_DEVICES:-0}}" 2>/dev/null | head -1 || true)"; printf \'{{"schema_version":1,"task_id":"{task_id}","run_id":"{run_id}","attempt":{attempt},"pid":%s,"timestamp":"%s","last_stdout_bytes":%s,"last_stderr_bytes":%s,"gpu_uuid":"%s","gpu_mem_used_mb":"%s"}}\\n\' "$$" "$ts" "$stdout_bytes" "$stderr_bytes" "$gpu_uuid" "$gpu_mem" > "{remote_out_dir}/heartbeat.json"; sleep 60; done; }}',
            f'heartbeat_loop & echo $! > "{remote_out_dir}/heartbeat.pid"',
            'if [ -z "${CUDA_VISIBLE_DEVICES:-}" ] && [ "' + str(task.raw_front_matter.get("resource_hint", "")).lower() + '" = "gpu" ]; then',
            f'  echo "No A100 GPU met the free-memory threshold." > "{remote_out_dir}/stderr.log"',
            f'  cat > "{remote_out_dir}/result.yaml" <<\'YAML\'',
            'schema_version: 1',
            f'task_id: {task_id}',
            f'run_id: {run_id}',
            f'attempt: {attempt}',
            'status: partial',
            'worker: codex',
            f'started_at: {now_iso()}',
            f'finished_at: {now_iso()}',
            'summary_short: No A100 GPU met the free-memory threshold, so the task was not started.',
            'changed_files: []',
            'commands_run: []',
            'outputs:',
            '  worker_notes: worker_notes.md',
            '  stdout: stdout.log',
            '  stderr: stderr.log',
            'risks:',
            '  - GPU free-memory threshold was not met.',
            'next_actions:',
            '  - Retry when at least one allowed A100 has enough free memory.',
            'YAML',
            f'  echo "finished" > "{remote_out_dir}/run_state.txt"',
            f'  echo "No A100 GPU met the free-memory threshold." > "{remote_out_dir}/worker_notes.md"',
            f'  : > "{remote_out_dir}/stdout.log"',
            '  exit 0',
            'fi',
            f'echo "running" > "{remote_out_dir}/run_state.txt"',
            f'date -Is > "{remote_out_dir}/started_at.txt"',
            f'python3 - <<\'PY\'\nimport json\np="{remote_out_dir}/state.json"\nd=json.load(open(p)); d["phase"]="codex_running"; d["updated_at"]="{now_iso()}"; open(p,"w").write(json.dumps(d, ensure_ascii=False, indent=2))\nPY',
            f'timeout {timeout_seconds} "$HOME/.local/bin/codex" {codex_exec_args} "$(cat \'{remote_prompt}\')" '
            f'< /dev/null > "{remote_out_dir}/codex_stdout.log" 2> "{remote_out_dir}/codex_stderr.log" &',
            f'echo $! > "{remote_out_dir}/codex.pid"',
            f'wait "$(cat "{remote_out_dir}/codex.pid")"',
            f'echo $? > "{remote_out_dir}/exit_code.txt"',
            f'echo "finished" > "{remote_out_dir}/run_state.txt"',
            f'if [ -f "{remote_out_dir}/heartbeat.pid" ]; then kill "$(cat "{remote_out_dir}/heartbeat.pid")" 2>/dev/null || true; fi',
            f'if [ -f "{remote_out_dir}/gpu_lock_dir.txt" ]; then rm -rf "$(cat "{remote_out_dir}/gpu_lock_dir.txt")" 2>/dev/null || true; fi',
            f'python3 - <<\'PY\'\nimport json, pathlib\np=pathlib.Path("{remote_out_dir}/state.json")\nd=json.loads(p.read_text()) if p.exists() else {{}}\nd.update({{"state":"finished","phase":"done","exit_code":int(open("{remote_out_dir}/exit_code.txt").read().strip() or 0),"updated_at":"{now_iso()}"}})\np.write_text(json.dumps(d, ensure_ascii=False, indent=2))\nPY',
            f'date -Is > "{remote_out_dir}/finished_at.txt"',
        ]
    ) + "\n"
    if worker == "materialized_execution":
        script_text = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -u",
                f'cd "{remote_workdir}"',
                f'export PYTHONPATH="{remote_task_dir}:${{PYTHONPATH:-}}"',
                f'export SOLUTION_AGENT_TASK_ID="{task_id}"',
                f'export SOLUTION_AGENT_RUN_ID="{run_id}"',
                f'export SOLUTION_AGENT_RUN_AUTH_SIGNATURE="{remote_auth_signature}"',
                f'export SOLUTION_AGENT_REMOTE_TASK_DIR="{remote_task_dir}"',
                f'export SOLUTION_AGENT_REMOTE_OUT_DIR="{remote_out_dir}"',
                f'mkdir -p "{remote_task_dir}/.tmp" "{remote_task_dir}/.cache"',
                f'export TMPDIR="{remote_task_dir}/.tmp"',
                f'export XDG_CACHE_HOME="{remote_task_dir}/.cache"',
                f'export HF_HOME="{remote_task_dir}/.cache/huggingface"',
                f'export TORCH_HOME="{remote_task_dir}/.cache/torch"',
                f'export MPLCONFIGDIR="{remote_task_dir}/.cache/matplotlib"',
                *gpu_setup_lines,
                f'mkdir -p "{remote_out_dir}"',
                f'echo "$$" > "{remote_out_dir}/runner.pid"',
                f'ps -o pgid= "$$" | tr -d " " > "{remote_out_dir}/process_group.pid" 2>/dev/null || true',
                f'echo "${{ORCHESTRATOR_NVIDIA_SMI_ID:-}}" > "{remote_out_dir}/nvidia_smi_id.txt"',
                f'echo "${{CUDA_VISIBLE_DEVICES:-}}" > "{remote_out_dir}/cuda_visible_devices.txt"',
                f'echo "{gpu_policy}" > "{remote_out_dir}/gpu_policy.txt"',
                f'echo "{gpu_id_semantics}" > "{remote_out_dir}/gpu_id_semantics.txt"',
                f'echo "{min_free_gb}" > "{remote_out_dir}/min_free_gpu_mem_gb.txt"',
                f'echo "{run_id}" > "{remote_out_dir}/run_id.txt"',
                f'echo "{attempt}" > "{remote_out_dir}/attempt.txt"',
                f'echo "hmac-sha256" > "{remote_out_dir}/remote_auth_method.txt"',
                'AUTH_ENV="$HOME/.remote_materializer/.env"',
                'if [ ! -s "$AUTH_ENV" ]; then echo "remote materializer env missing" >&2; exit 42; fi',
                'EXPECTED_AUTH_SIG="$(python3 - <<\'PY\'\nimport hashlib,hmac,os,pathlib\nsecret=""\nfor line in pathlib.Path(os.path.expanduser("~/.remote_materializer/.env")).read_text(encoding="utf-8").splitlines():\n    line=line.strip()\n    if line.startswith("SOLUTION_AGENT_REMOTE_AUTH_TOKEN="):\n        secret=line.split("=",1)[1].strip().strip("\\"\\\'")\n        break\nif not secret:\n    raise SystemExit(43)\nrun_id=os.environ.get("SOLUTION_AGENT_RUN_ID","")\nprint(hmac.new(secret.encode(), run_id.encode(), hashlib.sha256).hexdigest())\nPY\n)"',
                'if [ $? -eq 43 ]; then echo "remote auth token missing" >&2; exit 43; fi',
                'if [ "$EXPECTED_AUTH_SIG" != "${SOLUTION_AGENT_RUN_AUTH_SIGNATURE:-}" ]; then echo "remote auth handshake failed" >&2; exit 44; fi',
                f'cat > "{remote_out_dir}/remote_execution_env.json" <<\'JSON\'',
                json.dumps({**remote_execution_env, "remote_auth": {"method": "hmac-sha256", "env_key": REMOTE_AUTH_ENV_KEY, "signature_present": True}}, ensure_ascii=False),
                "JSON",
                f'rm -f "{remote_out_dir}/exit_code.txt" "{remote_out_dir}/finished_at.txt"',
                f': > "{remote_out_dir}/exit_code.txt"',
                f': > "{remote_out_dir}/gpu_usage.jsonl"',
                f'cat > "{remote_out_dir}/state.json" <<\'JSON\'',
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_id": task_id,
                        "run_id": run_id,
                        "attempt": attempt,
                        "state": "running",
                        "phase": "preparing",
                        "worker": "materialized_execution",
                        "exit_code": None,
                        "reason": None,
                        "updated_at": now_iso(),
                    },
                    ensure_ascii=False,
                ),
                "JSON",
                'gpu_sample() { gpu_target="${ORCHESTRATOR_NVIDIA_SMI_ID:-${CUDA_VISIBLE_DEVICES:-}}"; if [ -n "$gpu_target" ] && command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=uuid,memory.used,memory.total,memory.free --format=csv,noheader,nounits -i "$gpu_target" 2>/dev/null | head -1 | awk -F, \'{for(i=1;i<=NF;i++){gsub(/^ +| +$/,"",$i)}; printf "%s\\t%s\\t%s\\t%s", $1,$2,$3,$4}\'; fi; }',
                f'heartbeat_loop() {{ while true; do ts="$(date -Is)"; stdout_bytes="$(if [ -f "{remote_out_dir}/stdout.log" ]; then wc -c < "{remote_out_dir}/stdout.log"; else echo 0; fi)"; stderr_bytes="$(if [ -f "{remote_out_dir}/stderr.log" ]; then wc -c < "{remote_out_dir}/stderr.log"; else echo 0; fi)"; gpu_stats="$(gpu_sample)"; IFS="$(printf \'\\t\')" read -r gpu_uuid gpu_mem gpu_total gpu_free <<EOF\n$gpu_stats\nEOF\nprintf \'{{"schema_version":1,"task_id":"{task_id}","run_id":"{run_id}","attempt":{attempt},"pid":%s,"timestamp":"%s","last_stdout_bytes":%s,"last_stderr_bytes":%s,"worker":"materialized_execution","gpu_uuid":"%s","gpu_mem_used_mb":"%s","gpu_mem_total_mb":"%s","gpu_mem_free_mb":"%s"}}\\n\' "$$" "$ts" "$stdout_bytes" "$stderr_bytes" "$gpu_uuid" "$gpu_mem" "$gpu_total" "$gpu_free" > "{remote_out_dir}/heartbeat.json"; printf \'{{"schema_version":1,"task_id":"{task_id}","run_id":"{run_id}","timestamp":"%s","gpu_uuid":"%s","gpu_mem_used_mb":"%s","gpu_mem_total_mb":"%s","gpu_mem_free_mb":"%s"}}\\n\' "$ts" "$gpu_uuid" "$gpu_mem" "$gpu_total" "$gpu_free" >> "{remote_out_dir}/gpu_usage.jsonl"; sleep 60; done; }}',
                f'heartbeat_loop & echo $! > "{remote_out_dir}/heartbeat.pid"',
                f'echo "running" > "{remote_out_dir}/run_state.txt"',
                f'date -Is > "{remote_out_dir}/started_at.txt"',
                f'python3 - <<\'PY\'\nimport json\np="{remote_out_dir}/state.json"\nd=json.load(open(p)); d["phase"]="materialized_execution_running"; d["updated_at"]="{now_iso()}"; open(p,"w").write(json.dumps(d, ensure_ascii=False, indent=2))\nPY',
                f'{materialized_py_cmd} > "{remote_out_dir}/stdout.log" 2> "{remote_out_dir}/stderr.log" &',
                f'echo $! > "{remote_out_dir}/execution.pid"',
                f'wait "$(cat "{remote_out_dir}/execution.pid")"',
                f'echo $? > "{remote_out_dir}/exit_code.txt"',
                f'echo "finished" > "{remote_out_dir}/run_state.txt"',
                f'if [ -f "{remote_out_dir}/heartbeat.pid" ]; then kill "$(cat "{remote_out_dir}/heartbeat.pid")" 2>/dev/null || true; fi',
                f'python3 - <<\'PY\'\nimport json, pathlib, statistics\nout=pathlib.Path("{remote_out_dir}")\nusage_path=out/"gpu_usage.jsonl"\nvalues=[]\nsamples=0\nfirst_ts=None\nlast_ts=None\ngpu_uuid=None\ngpu_total=None\nif usage_path.exists():\n    for line in usage_path.read_text(encoding="utf-8").splitlines():\n        if not line.strip():\n            continue\n        try:\n            sample=json.loads(line)\n        except json.JSONDecodeError:\n            continue\n        samples += 1\n        ts=sample.get("timestamp")\n        first_ts = first_ts or ts\n        last_ts = ts or last_ts\n        gpu_uuid = sample.get("gpu_uuid") or gpu_uuid\n        gpu_total = sample.get("gpu_mem_total_mb") or gpu_total\n        try:\n            values.append(int(str(sample.get("gpu_mem_used_mb") or "").strip()))\n        except ValueError:\n            pass\nsummary={{"schema_version":1,"task_id":"{task_id}","run_id":"{run_id}","worker":"materialized_execution","samples":samples,"first_timestamp":first_ts,"last_timestamp":last_ts,"gpu_uuid":gpu_uuid,"gpu_mem_total_mb":gpu_total,"gpu_mem_peak_mb":max(values) if values else None,"gpu_mem_last_mb":values[-1] if values else None,"gpu_mem_avg_mb":round(statistics.fmean(values), 2) if values else None}}\n(out/"gpu_usage_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")\np=out/"state.json"\nd=json.loads(p.read_text(encoding="utf-8")) if p.exists() else {{}}\nd.update({{"state":"finished","phase":"done","exit_code":int(open(out/"exit_code.txt").read().strip() or 0),"updated_at":"{now_iso()}","gpu_usage":summary}})\np.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")\nPY',
                f'date -Is > "{remote_out_dir}/finished_at.txt"',
            ]
        ) + "\n"
    with script_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(script_text)
    remote_script = remote_task_dir + "/remote_runner.sh"
    bundle_files: Dict[str, Path] = {
        "task.md": task_path,
        "worker_prompt.md": remote_prompt_path,
        "remote_runner.sh": script_path,
        "input_bindings.yaml": input_bindings_path,
    }
    if worker == "materialized_execution":
        bundle_files["solution_agent"] = Path(__file__).resolve().parent.parent
    launch_bundle_path = _write_tar_gz(
        dispatch_dir(task_id) / "launch_bundle.tar.gz",
        bundle_files,
    )
    remote_bundle = f"/tmp/solution_agent_launch_{run_id}.tar.gz"
    copy_bundle = scp_to_remote(launch_bundle_path, host, remote_bundle)
    if detach:
        command = (
            f'mkdir -p "{remote_task_dir}" "{remote_out_dir}" && '
            f'tar -xzf "{remote_bundle}" -C "{remote_task_dir}" && '
            f'rm -f "{remote_bundle}" && '
            f'cd "{remote_task_dir}" && '
            f'(nohup bash "{remote_script}" > "{remote_out_dir}/runner_stdout.log" 2> "{remote_out_dir}/runner_stderr.log" '
            f'< /dev/null & echo $! > "{remote_out_dir}/runner.pid")'
        )
    else:
        command = (
            f'mkdir -p "{remote_task_dir}" "{remote_out_dir}" && '
            f'tar -xzf "{remote_bundle}" -C "{remote_task_dir}" && '
            f'rm -f "{remote_bundle}" && '
            f'bash "{remote_script}"'
        )
    remote_metadata = {
        "host": host,
        "remote_root": task_remote_root,
        "remote_task_dir": remote_task_dir,
        "remote_workdir": remote_workdir,
        "remote_out_dir": remote_out_dir,
        "run_id": run_id,
        "attempt": attempt,
        "timeout_seconds": timeout_seconds,
        "gpu_policy": gpu_policy,
        "gpu_id_semantics": gpu_id_semantics,
        "gpu_candidates": gpu_candidates,
        "min_free_gpu_mem_gb": min_free_gb,
        "codex_sandbox_mode": codex_sandbox_mode,
        "codex_bypass_sandbox": codex_bypass_sandbox,
        "remote_execution_env": remote_execution_env if worker == "materialized_execution" else None,
        "execution_phase": execution_phase,
        "input_bindings": input_bindings or {},
    }
    launch_started_at = now_iso()
    remote_run_metadata = {
        "bootstrap": bootstrap,
        "launch_bundle": {
            "local": str(launch_bundle_path),
            "remote": remote_bundle,
            "copy": copy_bundle,
            "contains": list(bundle_files.keys()),
        },
        "mkdir": {"skipped": True, "reason": "handled by launch bundle command"},
        "copy_prompt": {"skipped": True, "reason": "included in launch bundle"},
        "copy_script": {"skipped": True, "reason": "included in launch bundle"},
        "detach": detach,
        "run": {"status": "launch_pending", "command": command, "host": host, "launch_started_at": launch_started_at},
    }
    update_task_record(
        task_id,
        status="launching",
        task_status="launching",
        remote_health="unknown",
        run_phase="launching",
        worker=worker,
        remote=remote_metadata,
        remote_run=remote_run_metadata,
        latest_run_id=run_id,
    )
    _upsert_run_record(
        task_id,
        {
            "task_id": task_id,
            "run_id": run_id,
            "attempt": attempt,
            "status": "launch_pending",
            "task_status": "launching",
            "remote_health": "unknown",
            "run_phase": "launching",
            "host": host,
            "remote_task_dir": remote_task_dir,
            "remote_out_dir": remote_out_dir,
            "created_at": launch_started_at,
            "gpu": {
                "requested_nvidia_smi_ids": gpu_candidates,
                "requested_physical_ids": gpu_candidates,
                "cuda_visible_devices": "runtime_gpu_uuid" if gpu_candidates else None,
                "process_visible_index": 0 if gpu_candidates else None,
                "min_free_mem_gb": min_free_gb,
            },
        },
    )
    run_result = run_ssh(host, command, timeout=20 if detach else None)
    launch_ok = int(run_result.get("returncode", 1) or 0) == 0
    launch_timed_out = bool(run_result.get("timed_out"))
    launch_status = "running" if launch_ok else ("launch_unknown" if launch_timed_out else "launch_failed")
    launch_returncode = int(run_result.get("returncode", 1) or 0)
    launch_remote_health = "reachable" if launch_ok else ("degraded" if launch_timed_out else "unreachable" if launch_returncode == 255 else "reachable")
    launch_run_phase = "execute_run" if launch_ok else ("launching" if launch_timed_out else "launch_failed")
    launch_reason = None
    if launch_timed_out:
        launch_reason = "launch_ssh_timeout_needs_remote_verify"
    elif not launch_ok:
        launch_reason = "ssh_unreachable" if launch_returncode == 255 else "launch_command_failed"
    update_task_record(
        task_id,
        status=launch_status,
        task_status=launch_status,
        remote_health=launch_remote_health,
        run_phase=launch_run_phase,
        remote_state=launch_status,
        last_remote_seen_at=now_iso(),
        remote=remote_metadata,
        remote_run={**remote_run_metadata, "run": run_result, "launch_reason": launch_reason},
    )
    _upsert_run_record(
        task_id,
        {
            "task_id": task_id,
            "run_id": run_id,
            "attempt": attempt,
            "status": launch_status,
            "task_status": launch_status,
            "remote_health": launch_remote_health,
            "run_phase": launch_run_phase,
            "state": launch_status,
            "launch_reason": launch_reason,
            "host": host,
            "remote_task_dir": remote_task_dir,
            "remote_out_dir": remote_out_dir,
            "launched_at": launch_started_at,
            "gpu": {
                "requested_nvidia_smi_ids": gpu_candidates,
                "requested_physical_ids": gpu_candidates,
                "cuda_visible_devices": "runtime_gpu_uuid" if gpu_candidates else None,
                "process_visible_index": 0 if gpu_candidates else None,
                "min_free_mem_gb": min_free_gb,
            },
        },
    )
    if launch_status in {"launch_failed", "launch_unknown"}:
        _move_task_folder(task_id, launch_status)
    return {
        "dispatch": dispatched,
        "run_id": run_id,
        "attempt": attempt,
        "bootstrap": bootstrap,
        "launch_bundle": remote_run_metadata["launch_bundle"],
        "remote": remote_metadata,
        "mkdir": remote_run_metadata["mkdir"],
        "copy_prompt": remote_run_metadata["copy_prompt"],
        "copy_script": remote_run_metadata["copy_script"],
        "detach": detach,
        "run": run_result,
    }


def run_next_remote(
    host: str,
    remote_root: str,
    worker: str = "codex",
    remote_workdir: Optional[str] = None,
    detach: bool = True,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    task_id = next_pending_task_id(worker=worker, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    if task_id is None:
        return {
            "ran": False,
            "reason": "no eligible ready pending tasks",
            "selector": {"worker": worker, "line": line, "batch": batch, "dag_root": dag_root, "prefix": prefix},
        }
    result = run_remote(
        task_id,
        host=host,
        remote_root=remote_root,
        worker=worker,
        remote_workdir=remote_workdir,
        detach=detach,
    )
    return {"ran": True, "task_id": task_id, "remote": result}


def _remote_status_cache_path(task_id: str, host: str, remote_root: str) -> Path:
    digest = hashlib.sha1(f"{task_id}|{host}|{remote_root}".encode("utf-8")).hexdigest()[:16]
    return state_dir() / "remote_status_cache" / f"{digest}.json"


@contextmanager
def _remote_sync_task_lock(task_id: str):
    lock_dir = state_dir() / "remote_sync_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._") or hashlib.sha1(task_id.encode("utf-8")).hexdigest()
    lock_path = lock_dir / f"{lock_name}.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_remote_status_cache(task_id: str, host: str, remote_root: str, max_age_seconds: int) -> Optional[Dict[str, object]]:
    if max_age_seconds <= 0:
        return None
    path = _remote_status_cache_path(task_id, host, remote_root)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        payload["cache"] = {"hit": True, "age_seconds": int(age), "path": str(path)}
        return payload
    return None


def _write_remote_status_cache(task_id: str, host: str, remote_root: str, payload: Dict[str, object]) -> None:
    path = _remote_status_cache_path(task_id, host, remote_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = dict(payload)
    serializable["cache"] = {"hit": False, "stored_at": now_iso(), "path": str(path)}
    path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")


def remote_status(
    task_id: str,
    host: str,
    remote_root: str,
    max_age_seconds: int = 0,
    force_refresh: bool = False,
) -> Dict[str, object]:
    if not force_refresh:
        cached = _read_remote_status_cache(task_id, host, remote_root, max_age_seconds)
        if cached is not None:
            return cached
    _, task_path = find_task(task_id)
    task = load_task(task_path)
    task_remote_root = str(remote_root)
    remote_task_dir = _current_remote_task_dir(task_id, task_remote_root)
    remote_out = remote_task_dir + "/_worker_out"
    command = (
        f'out="{remote_out}"; '
        'echo "files:"; find "$out" -maxdepth 1 -type f -printf "%f\\n" 2>/dev/null | sort; '
        'pick_file() { for name in "$@"; do if [ -s "$out/$name" ]; then printf "%s" "$out/$name"; return 0; fi; done; return 1; }; '
        'pid_file="$(pick_file runner.pid worker.pid execution.pid codex.pid || true)"; '
        'stdout_file="$(pick_file stdout.log codex_stdout.log runner_stdout.log || true)"; '
        'stderr_file="$(pick_file stderr.log codex_stderr.log runner_stderr.log || true)"; '
        'echo "pid:"; if [ -n "$pid_file" ]; then cat "$pid_file" 2>/dev/null || true; fi; '
        'echo "exit_code:"; cat "$out/exit_code.txt" 2>/dev/null || true; '
        'echo "run_state:"; cat "$out/run_state.txt" 2>/dev/null || true; '
        'echo "state_json:"; cat "$out/state.json" 2>/dev/null || true; echo; '
        'echo "heartbeat_json:"; cat "$out/heartbeat.json" 2>/dev/null || true; echo; '
        'echo "heartbeat_age_seconds:"; python3 - "$out/heartbeat.json" <<\'PY\' 2>/dev/null || true\n'
        'import datetime,json,sys\n'
        'try:\n'
        '    ts=json.load(open(sys.argv[1])).get("timestamp","")\n'
        '    dt=datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))\n'
        '    print(int((datetime.datetime.now(dt.tzinfo)-dt).total_seconds()))\n'
        'except Exception:\n'
        '    print("")\n'
        'PY\n'
        'echo "running:"; '
        'pid=""; if [ -n "$pid_file" ]; then pid=$(cat "$pid_file" 2>/dev/null || true); fi; '
        'if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then echo yes; else echo no; fi; '
        'echo "stderr_tail:"; if [ -n "$stderr_file" ]; then tail -n 40 "$stderr_file" 2>/dev/null || true; fi; '
        'echo "stdout_tail:"; if [ -n "$stdout_file" ]; then tail -n 40 "$stdout_file" 2>/dev/null || true; fi'
    )
    result = run_ssh(host, command)
    payload = {"task_id": task_id, "remote": f"{host}:{remote_out}", "status": result, "cache": {"hit": False, "stored_at": now_iso()}}
    if int(result.get("returncode", 1) or 0) == 0:
        _write_remote_status_cache(task_id, host, remote_root, payload)
    return payload


def _remote_out_for_status_item(item: Dict[str, object]) -> str:
    remote_out = str(item.get("remote_out_dir") or "")
    if remote_out:
        return remote_out
    task_id = str(item.get("task_id") or "")
    remote_root_value = str(item.get("remote_root") or "")
    return _current_remote_task_dir(task_id, remote_root_value) + "/_worker_out"


def remote_status_bulk(
    items: List[Dict[str, object]],
    host: str,
    timeout_seconds: Optional[float] = 45,
) -> Dict[str, Dict[str, object]]:
    requested: List[Dict[str, str]] = []
    for item in items:
        task_id = str(item.get("task_id") or "")
        remote_root_value = str(item.get("remote_root") or "")
        if not task_id or not remote_root_value:
            continue
        requested.append(
            {
                "task_id": task_id,
                "remote_root": remote_root_value,
                "remote_out": _remote_out_for_status_item(item),
            }
        )
    if not requested:
        return {}
    script = r'''
import datetime
import json
import os
import subprocess
import sys


def read_text(path, limit=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except Exception:
        return ""
    if limit and len(text) > limit:
        return text[-limit:]
    return text


def tail(path, lines=40):
    try:
        completed = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return completed.stdout
    except Exception:
        return ""


def first_existing(out, names):
    for name in names:
        path = os.path.join(out, name)
        try:
            if os.path.getsize(path) > 0:
                return path
        except Exception:
            pass
    return ""


def pid_alive(pid_text):
    pid_text = (pid_text or "").strip()
    if not pid_text.isdigit():
        return False
    try:
        return subprocess.run(["ps", "-p", pid_text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def heartbeat_age(path):
    try:
        payload = json.load(open(path, "r", encoding="utf-8"))
        ts = str(payload.get("timestamp") or "")
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return str(int((datetime.datetime.now(dt.tzinfo) - dt).total_seconds()))
    except Exception:
        return ""


def task_snapshot(item):
    out = item["remote_out"]
    try:
        files = sorted(name for name in os.listdir(out) if os.path.isfile(os.path.join(out, name)))
    except Exception:
        files = []
    pid_file = first_existing(out, ["runner.pid", "worker.pid", "execution.pid", "codex.pid"])
    stdout_file = first_existing(out, ["stdout.log", "codex_stdout.log", "runner_stdout.log"])
    stderr_file = first_existing(out, ["stderr.log", "codex_stderr.log", "runner_stderr.log"])
    pid = read_text(pid_file).strip() if pid_file else ""
    exit_code = read_text(os.path.join(out, "exit_code.txt")).strip()
    run_state = read_text(os.path.join(out, "run_state.txt")).strip()
    state_json = read_text(os.path.join(out, "state.json"), limit=12000)
    heartbeat_json = read_text(os.path.join(out, "heartbeat.json"), limit=12000)
    running = "yes" if pid_alive(pid) else "no"
    stdout = "\n".join(
        [
            "files:",
            "\n".join(files),
            "pid:",
            pid,
            "exit_code:",
            exit_code,
            "run_state:",
            run_state,
            "state_json:",
            state_json,
            "",
            "heartbeat_json:",
            heartbeat_json,
            "",
            "heartbeat_age_seconds:",
            heartbeat_age(os.path.join(out, "heartbeat.json")),
            "running:",
            running,
            "stderr_tail:",
            tail(stderr_file) if stderr_file else "",
            "stdout_tail:",
            tail(stdout_file) if stdout_file else "",
        ]
    )
    return {
        "task_id": item["task_id"],
        "remote_root": item["remote_root"],
        "remote_out": out,
        "returncode": 0,
        "stdout": stdout,
        "stderr": "",
    }


items = json.loads(sys.argv[1])
print(json.dumps({"schema_version": 1, "items": [task_snapshot(item) for item in items]}, ensure_ascii=False))
'''
    command = f"python3 - {shlex.quote(json.dumps(requested, ensure_ascii=False))} <<'PY'\n{script}\nPY"
    result = run_ssh(host, command, timeout=timeout_seconds)
    by_task: Dict[str, Dict[str, object]] = {}
    if int(result.get("returncode", 1) or 0) != 0:
        for item in requested:
            task_id = item["task_id"]
            by_task[task_id] = {
                "task_id": task_id,
                "remote": f"{host}:{item['remote_out']}",
                "status": result,
                "cache": {"hit": False, "bulk": True, "stored_at": now_iso()},
            }
        return by_task
    try:
        payload = json.loads(str(result.get("stdout") or ""))
        snapshots = payload.get("items") if isinstance(payload, dict) else []
    except Exception as exc:
        error_result = dict(result)
        error_result["returncode"] = 1
        error_result["stderr"] = f"bulk remote status parse failed: {exc}"
        for item in requested:
            task_id = item["task_id"]
            by_task[task_id] = {
                "task_id": task_id,
                "remote": f"{host}:{item['remote_out']}",
                "status": error_result,
                "cache": {"hit": False, "bulk": True, "stored_at": now_iso()},
            }
        return by_task
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        task_id = str(snapshot.get("task_id") or "")
        remote_root_value = str(snapshot.get("remote_root") or "")
        remote_out = str(snapshot.get("remote_out") or "")
        status = {
            "returncode": int(snapshot.get("returncode", 0) or 0),
            "stdout": str(snapshot.get("stdout") or ""),
            "stderr": str(snapshot.get("stderr") or ""),
            "command": "bulk remote status snapshot",
            "host": host,
            "bulk": True,
        }
        task_payload = {
            "task_id": task_id,
            "remote": f"{host}:{remote_out}",
            "status": status,
            "cache": {"hit": False, "bulk": True, "stored_at": now_iso()},
        }
        by_task[task_id] = task_payload
        if task_id and remote_root_value and int(status.get("returncode", 1) or 0) == 0:
            _write_remote_status_cache(task_id, host, remote_root_value, task_payload)
    return by_task


def _remote_statuses_for_items(items: List[Dict[str, object]], max_age_seconds: int = 0) -> Dict[str, Dict[str, object]]:
    by_task: Dict[str, Dict[str, object]] = {}
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for item in items:
        task_id = str(item.get("task_id") or "")
        host = str(item.get("host") or "")
        remote_root_value = str(item.get("remote_root") or "")
        if not task_id or not host or not remote_root_value:
            continue
        cached = _read_remote_status_cache(task_id, host, remote_root_value, max_age_seconds)
        if cached is not None:
            by_task[task_id] = cached
            continue
        grouped.setdefault(host, []).append(item)
    for host, host_items in grouped.items():
        by_task.update(remote_status_bulk(host_items, host=host))
    return by_task


def remote_monitor_once(
    task_id: str,
    host: str,
    remote_root: str,
    status_result: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    status_result = status_result or remote_status(task_id, host, remote_root, force_refresh=True)
    if int((status_result.get("status") or {}).get("returncode", 1) or 0) != 0:
        payload = {
            "state": "remote_unreachable",
            "status": status_result,
            "checked_at": now_iso(),
            "reason": "remote_status_failed",
        }
        record = load_registry().get("tasks", {}).get(task_id, {})
        latest_run_id = str(record.get("latest_run_id") or "")
        update_task_record(task_id, last_remote_reconcile=payload, remote_health="unreachable")
        if latest_run_id:
            _update_run_record(task_id, latest_run_id, remote_health="unreachable", last_remote_reconcile=payload)
        return payload
    stdout = str(status_result.get("status", {}).get("stdout", ""))
    normalized = stdout.replace("\r\n", "\n")
    running = "running:\nyes" in normalized
    exit_match = re.search(r"(?m)^exit_code:\n([^\n]*)", normalized)
    exit_value = exit_match.group(1).strip() if exit_match else ""
    has_exit = bool(exit_value and re.match(r"^-?\d+$", exit_value))
    has_result = "result.yaml" in stdout
    if running:
        payload = {"state": "running", "status": status_result, "checked_at": now_iso()}
        record = load_registry().get("tasks", {}).get(task_id, {})
        latest_run_id = str(record.get("latest_run_id") or "")
        update_task_record(
            task_id,
            status="running",
            task_status="running",
            remote_health="reachable",
            run_phase="execute_run",
            last_remote_reconcile=payload,
            remote_state="running",
            last_remote_seen_at=now_iso(),
        )
        if latest_run_id:
            _update_run_record(
                task_id,
                latest_run_id,
                status="running",
                task_status="running",
                state="running",
                remote_health="reachable",
                run_phase="execute_run",
                remote_state="running",
                last_remote_seen_at=now_iso(),
            )
        return payload
    if has_exit or has_result:
        record = load_registry().get("tasks", {}).get(task_id, {})
        latest_run_id = str(record.get("latest_run_id") or "")
        update_task_record(
            task_id,
            remote_health="reachable",
            run_phase="finished_remote",
            remote_state="finished_remote",
            last_remote_seen_at=now_iso(),
            last_remote_reconcile={"state": "finished_remote", "checked_at": now_iso(), "status": status_result},
        )
        if latest_run_id:
            _update_run_record(
                task_id,
                latest_run_id,
                remote_health="reachable",
                run_phase="finished_remote",
                remote_state="finished_remote",
                last_remote_seen_at=now_iso(),
            )
        pulled = sync_pull_remote(task_id, host, remote_root)
        collected = collect(task_id)
        index = str(report_index())
        latest = str(report_latest())
        return {
            "state": "collected",
            "status": status_result,
            "pull": pulled,
            "collect": collected,
            "report_index": index,
            "report_latest": latest,
        }
    payload = {"state": "not_started_or_no_output", "status": status_result, "checked_at": now_iso()}
    record = load_registry().get("tasks", {}).get(task_id, {})
    latest_run_id = str(record.get("latest_run_id") or "")
    update_task_record(
        task_id,
        last_remote_reconcile=payload,
        remote_health="reachable",
        run_phase="launch_unknown",
        remote_state="unknown",
        last_remote_seen_at=now_iso(),
    )
    if latest_run_id:
        _update_run_record(
            task_id,
            latest_run_id,
            remote_health="reachable",
            run_phase="launch_unknown",
            remote_state="unknown",
            last_remote_seen_at=now_iso(),
        )
    return payload


def remote_reconcile_running_tasks() -> Dict[str, object]:
    checked: List[Dict[str, object]] = []
    collected: List[Dict[str, object]] = []
    unknown: List[Dict[str, object]] = []
    items = running_remote_tasks()
    status_by_task = _remote_statuses_for_items(items, max_age_seconds=0)
    for item in items:
        task_id = str(item["task_id"])
        host = str(item["host"])
        remote_root_value = str(item["remote_root"])
        try:
            result = remote_monitor_once(
                task_id,
                host=host,
                remote_root=remote_root_value,
                status_result=status_by_task.get(task_id),
            )
        except Exception as exc:
            result = {"state": "remote_unknown", "task_id": task_id, "error": str(exc), "checked_at": now_iso()}
            update_task_record(task_id, last_remote_reconcile=result, remote_health="degraded", remote_state="unknown")
        state = str(result.get("state") or "")
        checked.append({"task_id": task_id, "state": state})
        if state == "collected":
            collected.append({"task_id": task_id, "result": result.get("collect")})
        elif state in {"remote_unreachable", "remote_unknown", "not_started_or_no_output"}:
            unknown.append({"task_id": task_id, "state": state, "reason": result.get("reason") or result.get("error") or ""})
    return {"checked": checked, "collected": collected, "unknown": unknown, "checked_at": now_iso()}


def _remote_status_unavailable_decision(status_result: Dict[str, object]) -> SupervisionDecision:
    status = status_result.get("status") if isinstance(status_result.get("status"), dict) else {}
    returncode = status.get("returncode")
    stderr = str(status.get("stderr") or "").strip()
    stdout = str(status.get("stdout") or "").strip()
    evidence = [f"remote status returncode={returncode}"]
    if stderr:
        evidence.append(stderr[:240])
    elif stdout:
        evidence.append(stdout[:240])
    cache = status_result.get("cache") if isinstance(status_result.get("cache"), dict) else {}
    if cache:
        evidence.append(f"cache={cache}")
    return SupervisionDecision(
        action="warn",
        confidence=0.7,
        reason_code="remote_status_unavailable",
        summary="Remote status query failed, so the worker state cannot be inferred from this check.",
        evidence=evidence,
        concerns=["Keep the existing task state; do not mark the worker dead or collect from an unreachable status check."],
        next_check_minutes=5,
        source="hard_detector",
    )


def supervise_remote(
    task_id: str,
    host: str,
    remote_root: str,
    model: str = DEFAULT_OLLAMA_MODEL,
    remote_status_cache_seconds: int = 60,
    status_result: Optional[Dict[str, object]] = None,
    allow_model_review: bool = False,
) -> Dict[str, object]:
    status_result = status_result or remote_status(task_id, host, remote_root, max_age_seconds=remote_status_cache_seconds)
    _, task_path = find_task(task_id)
    task = load_task(task_path)
    remote_text = json.dumps(status_result, ensure_ascii=False, indent=2)
    status = status_result.get("status") if isinstance(status_result.get("status"), dict) else {}
    if int(status.get("returncode", 1) or 0) != 0:
        decision = _remote_status_unavailable_decision(status_result)
        payload = {
            "task_id": task_id,
            "decision": decision.as_dict(),
            "hard_detector": decision.as_dict(),
            "llm_advisory": {
                **decision.as_dict(),
                "source": "skipped_due_to_remote_status_error",
            },
            "status": status_result,
            "checked_at": now_iso(),
            "model_dispatch_count": 0,
        }
        try:
            update_task_record(
                task_id,
                last_supervision=payload,
                remote_health="unreachable",
                remote_status_error={
                    "checked_at": payload["checked_at"],
                    "returncode": status.get("returncode"),
                    "stderr": str(status.get("stderr") or "")[:500],
                },
            )
        except OSError as exc:
            payload["registry_update_error"] = f"{type(exc).__name__}: {exc}"
        return payload
    hard_text = str(status_result.get("status", {}).get("stdout", ""))
    hard_decision = fallback_decision(hard_text)
    review_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "task_id": task_id,
                "reason_code": hard_decision.reason_code,
                "action": hard_decision.action,
                "evidence": hard_decision.evidence,
                "concerns": hard_decision.concerns,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record = load_registry().get("tasks", {}).get(task_id, {})
    consumed = set(record.get("supervision_model_review_fingerprints", []) or []) if isinstance(record, dict) else set()
    model_dispatch_count = 0
    if allow_model_review and hard_decision.action == "codex_confirm" and review_fingerprint not in consumed:
        llm_decision = supervise_with_llm(task, remote_text, model=model)
        consumed.add(review_fingerprint)
        model_dispatch_count = 1
        update_task_record(
            task_id,
            supervision_model_review_fingerprints=sorted(consumed),
            supervision_model_review_count=len(consumed),
        )
    else:
        if not allow_model_review:
            skip_reason = "model_review_disabled"
        elif hard_decision.action != "codex_confirm":
            skip_reason = "deterministic_decision_sufficient"
        else:
            skip_reason = "duplicate_supervision_fingerprint"
        llm_decision = SupervisionDecision(
            **{
                **hard_decision.as_dict(),
                "source": f"skipped:{skip_reason}",
            }
        )
    decision = hard_decision
    if model_dispatch_count and llm_decision.action in {
        "continue",
        "wait",
        "wait_gpu",
        "codex_confirm",
        "collect",
        "warn",
        "stop_recommended",
    }:
        decision = llm_decision
    payload = {
        "task_id": task_id,
        "decision": decision.as_dict(),
        "hard_detector": hard_decision.as_dict(),
        "llm_advisory": llm_decision.as_dict(),
        "status": status_result,
        "checked_at": now_iso(),
        "model_dispatch_count": model_dispatch_count,
        "model_review_fingerprint": review_fingerprint,
    }
    try:
        update_task_record(task_id, last_supervision=payload)
    except OSError as exc:
        payload["registry_update_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def running_remote_tasks() -> List[Dict[str, object]]:
    tasks = load_registry().get("tasks", {})
    items: List[Dict[str, object]] = []
    for task_id, record in tasks.items():
        if not isinstance(record, dict) or record.get("status") not in {"running", "launching", "launch_unknown"}:
            continue
        remote = record.get("remote") or {}
        if not isinstance(remote, dict) or not remote.get("host") or not remote.get("remote_root"):
            continue
        items.append(
            {
                "task_id": str(task_id),
                "host": str(remote.get("host")),
                "remote_root": str(remote.get("remote_root")),
                "remote_out_dir": str(remote.get("remote_out_dir", "")),
                "run_id": str(remote.get("run_id", "")),
            }
        )
    return items


def supervise_running_remote_tasks(
    model: str = DEFAULT_OLLAMA_MODEL,
    collect_finished: bool = True,
    remote_status_cache_seconds: int = 60,
    allow_model_review: bool = False,
) -> Dict[str, object]:
    checked: List[Dict[str, object]] = []
    collected: List[Dict[str, object]] = []
    blocked: List[Dict[str, object]] = []
    items = running_remote_tasks()
    status_by_task = _remote_statuses_for_items(items, max_age_seconds=remote_status_cache_seconds)
    for item in items:
        task_id = str(item["task_id"])
        host = str(item["host"])
        remote_root_value = str(item["remote_root"])
        decision = supervise_remote(
            task_id,
            host=host,
            remote_root=remote_root_value,
            model=model,
            remote_status_cache_seconds=remote_status_cache_seconds,
            status_result=status_by_task.get(task_id),
            allow_model_review=allow_model_review,
        )
        checked.append(
            {
                "task_id": task_id,
                "decision": decision.get("decision"),
                "model_dispatch_count": int(decision.get("model_dispatch_count", 0) or 0),
            }
        )
        action = (decision.get("decision") or {}).get("action")
        if action == "collect" and collect_finished:
            pull = sync_pull_remote(task_id, host=host, remote_root=remote_root_value)
            collect_result = collect(task_id)
            collected.append({"task_id": task_id, "pull": pull, "collect": collect_result})
        elif action in {"warn", "stop_recommended", "codex_confirm"}:
            blocked.append({"task_id": task_id, "action": action, "decision": decision.get("decision")})
    if collected:
        report_index()
        report_latest()
    return {
        "checked": checked,
        "collected": collected,
        "blocked": blocked,
        "model_dispatch_count": sum(int(item.get("model_dispatch_count", 0) or 0) for item in checked),
        "model_review_policy": "explicit_only" if allow_model_review else "no_model",
    }


def sync_pull_remote(task_id: str, host: str, remote_root: str) -> Dict[str, object]:
    with _remote_sync_task_lock(task_id):
        _, task_path = find_task(task_id)
        task = load_task(task_path)
        task_remote_root = str(remote_root)
        remote_task_dir = _current_remote_task_dir(task_id, task_remote_root)
        remote_out = remote_task_dir + "/_worker_out"
        remote_artifacts = remote_task_dir + "/artifacts"
        local_out = worker_out_dir(task_id)
        if local_out.exists():
            shutil.rmtree(local_out)
        result = scp_from_remote(host, remote_out, local_out)
        artifacts_result: Dict[str, object] | None = None
        if int(result.get("returncode", 1) or 0) == 0:
            artifacts_result = scp_from_remote(host, remote_artifacts, local_out / "artifacts")
        payload = {
            "worker_out": result,
            "artifacts": artifacts_result,
        }
        update_task_record(task_id, remote_pull=payload)
        return {
            "task_id": task_id,
            "remote": f"{host}:{remote_out}",
            "remote_artifacts": f"{host}:{remote_artifacts}",
            "local": str(local_out),
            "result": result,
            "artifacts_result": artifacts_result,
        }


def _auto_sync_before_collect(task_id: str, out_dir: Path) -> Optional[Dict[str, object]]:
    if (out_dir / "result.yaml").exists():
        return None
    record = load_registry().get("tasks", {}).get(task_id, {})
    if not isinstance(record, dict):
        return None
    remote = record.get("remote") or {}
    if not isinstance(remote, dict):
        return None
    host = str(remote.get("host") or "")
    remote_root = str(remote.get("remote_root") or remote.get("remote_task_root") or "")
    if not host or not remote_root:
        return None
    try:
        return sync_pull_remote(task_id, host=host, remote_root=remote_root)
    except Exception as exc:
        payload = {"error": str(exc), "host": host, "remote_root": remote_root, "attempted_at": now_iso()}
        update_task_record(task_id, remote_auto_sync_error=payload)
        return {"task_id": task_id, "auto_sync": "failed", **payload}


def remote_stop(
    task_id: str,
    host: str,
    remote_root: str,
    reason: str = "浜哄伐鏆仠",
    reset_to_pending: bool = True,
) -> Dict[str, object]:
    task_remote_root = str(remote_root)
    remote_task_dir = _current_remote_task_dir(task_id, task_remote_root)
    remote_out = remote_task_dir + "/_worker_out"
    reason_json = json.dumps(reason, ensure_ascii=False)
    command = (
        f'out="{remote_out}"; task_dir="{remote_task_dir}"; mkdir -p "$out"; '
        'runner_pid="$(cat "$out/runner.pid" 2>/dev/null || true)"; '
        'heartbeat_pid="$(cat "$out/heartbeat.pid" 2>/dev/null || true)"; '
        'codex_pid="$(cat "$out/codex.pid" 2>/dev/null || true)"; '
        'process_group="$(cat "$out/process_group.pid" 2>/dev/null || true)"; '
        'child_pids="$(pgrep -f "$task_dir|$out" 2>/dev/null | awk -v self="$$" -v parent="$PPID" \'$1 != self && $1 != parent {print $1}\' | tr "\\n" " " || true)"; '
        'if [ -n "$process_group" ]; then kill -TERM -- "-$process_group" 2>/dev/null || true; fi; '
        'for pid in $child_pids "$runner_pid" "$codex_pid" "$heartbeat_pid"; do '
        '  if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then kill -TERM "$pid" 2>/dev/null || true; fi; '
        'done; '
        'sleep 2; '
        'child_pids="$(pgrep -f "$task_dir|$out" 2>/dev/null | awk -v self="$$" -v parent="$PPID" \'$1 != self && $1 != parent {print $1}\' | tr "\\n" " " || true)"; '
        'if [ -n "$process_group" ]; then kill -KILL -- "-$process_group" 2>/dev/null || true; fi; '
        'for pid in $child_pids "$runner_pid" "$codex_pid" "$heartbeat_pid"; do '
        '  if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then kill -KILL "$pid" 2>/dev/null || true; fi; '
        'done; '
        'if [ -f "$out/gpu_lock_dir.txt" ]; then rm -rf "$(cat "$out/gpu_lock_dir.txt")" 2>/dev/null || true; fi; '
        'date -Is > "$out/stopped_at.txt"; '
        'echo manual_pause > "$out/run_state.txt"; '
        'if [ ! -s "$out/exit_code.txt" ]; then echo 130 > "$out/exit_code.txt"; fi; '
        f'python3 - "$out/state.json" {shlex.quote(reason_json)} <<\'PY\'\n'
        'import json, pathlib, sys, datetime\n'
        'path = pathlib.Path(sys.argv[1])\n'
        'reason = json.loads(sys.argv[2])\n'
        'try:\n'
        '    payload = json.loads(path.read_text())\n'
        'except Exception:\n'
        '    payload = {"schema_version": 1}\n'
        'payload.update({"state": "stopped", "phase": "manual_pause", "exit_code": 130, "reason": reason, "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})\n'
        'path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))\n'
        'PY\n'
        'echo "runner_pid:$runner_pid"; '
        'echo "codex_pid:$codex_pid"; '
        'echo "heartbeat_pid:$heartbeat_pid"; '
        'echo "process_group:$process_group"; '
        'echo "child_pids_after:"; pgrep -f "$task_dir|$out" 2>/dev/null | awk -v self="$$" -v parent="$PPID" \'$1 != self && $1 != parent {print $1}\' || true; '
        'echo "runner_alive:"; if [ -n "$runner_pid" ] && ps -p "$runner_pid" >/dev/null 2>&1; then echo yes; else echo no; fi; '
        'echo "codex_alive:"; if [ -n "$codex_pid" ] && ps -p "$codex_pid" >/dev/null 2>&1; then echo yes; else echo no; fi; '
        'echo "heartbeat_alive:"; if [ -n "$heartbeat_pid" ] && ps -p "$heartbeat_pid" >/dev/null 2>&1; then echo yes; else echo no; fi; '
        'echo "state_json:"; cat "$out/state.json" 2>/dev/null || true'
    )
    stop_result = run_ssh(host, command)
    stop_stdout = str(stop_result.get("stdout", ""))
    stopped = (
        int(stop_result.get("returncode", 1) or 0) == 0
        and "runner_alive:\nyes" not in stop_stdout
        and "codex_alive:\nyes" not in stop_stdout
        and "heartbeat_alive:\nyes" not in stop_stdout
    )
    pause_result = None
    if stopped:
        pause_result = manual_pause(task_id, reason=reason, reset_to_pending=reset_to_pending, explicit_requeue=reset_to_pending)
    update_task_record(
        task_id,
        remote_stop={
            "host": host,
            "remote": f"{host}:{remote_out}",
            "result": stop_result,
            "reason": reason,
            "stopped": stopped,
        },
    )
    return {"task_id": task_id, "remote": f"{host}:{remote_out}", "stop": stop_result, "pause": pause_result}


def manual_pause(
    task_id: str,
    reason: str = "浜哄伐鏆仠",
    reset_to_pending: bool = True,
    explicit_requeue: bool = False,
) -> Dict[str, object]:
    """Record a human-requested pause without losing run history.

    By default the task book is moved back to pending so the scheduler can
    launch a fresh attempt later. The latest run remains recorded as partial.
    """
    ensure_orchestrator_dirs()
    current_status, _ = find_task(task_id)
    if current_status == "completed":
        raise ValueError(f"cannot manual-pause completed task: {task_id}")

    now = now_iso()
    if reset_to_pending:
        if not explicit_requeue:
            raise ValueError("reset_to_pending requires explicit_requeue=True")
        final_status = "pending"
        task_path = _move_task_folder(task_id, "pending")
    else:
        final_status = "partial"
        task_path = _move_task_folder(task_id, "partial")

    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        latest_run_id = record.get("latest_run_id")
        paused_run = None
        if latest_run_id:
            runs = list(record.get("runs", []) or [])
            for index, existing in enumerate(runs):
                if isinstance(existing, dict) and existing.get("run_id") == latest_run_id:
                    paused_run = dict(existing)
                    paused_run.update(
                        {
                            "status": "partial",
                            "task_status": "partial",
                            "run_phase": "manual_pause",
                            "finished_at": now,
                            "summary_short": reason,
                            "pause_reason": reason,
                            "state": "manual_pause",
                            "remote_state": "manual_pause",
                        }
                    )
                    runs[index] = paused_run
                    break
            if paused_run is None:
                paused_run = {
                    "task_id": task_id,
                    "run_id": latest_run_id,
                    "status": "partial",
                    "task_status": "partial",
                    "run_phase": "manual_pause",
                    "finished_at": now,
                    "summary_short": reason,
                    "pause_reason": reason,
                    "state": "manual_pause",
                    "remote_state": "manual_pause",
                }
                runs.append(paused_run)
            record["runs"] = runs
        if reset_to_pending:
            record.update(
                {
                    "status": final_status,
                    "task_status": final_status,
                    "started_at": None,
                    "finished_at": None,
                    "result_path": None,
                    "report_path": None,
                    "summary_short": f"Reset to pending after manual pause: {reason}",
                    "manual_pause_reason": reason,
                    "manual_paused_at": now,
                    "status_transition_source": "manual_pause",
                    "status_transition_reason": reason,
                    "status_transition_actor": "user",
                }
            )
        else:
            record.update(
                {
                    "status": final_status,
                    "task_status": final_status,
                    "finished_at": now,
                    "summary_short": reason,
                    "manual_pause_reason": reason,
                    "manual_paused_at": now,
                    "status_transition_source": "manual_pause",
                    "status_transition_reason": reason,
                    "status_transition_actor": "user",
                }
            )
        return record

    updated_record = mutate_registry(_mutate)
    latest_run_id = updated_record.get("latest_run_id") if isinstance(updated_record, dict) else None
    paused_run = None
    if latest_run_id and isinstance(updated_record, dict):
        for existing in updated_record.get("runs", []) or []:
            if isinstance(existing, dict) and existing.get("run_id") == latest_run_id:
                paused_run = existing
                break
    return {
        "task_id": task_id,
        "status": final_status,
        "task_path": str(task_path / "task.md") if task_path else None,
        "latest_run_id": latest_run_id,
        "paused_run": paused_run,
        "reason": reason,
        "reset_to_pending": reset_to_pending,
        "explicit_requeue": explicit_requeue,
    }


def requeue_completed(
    task_id: str,
    reason: str = "Explicitly requeued completed task",
) -> Dict[str, object]:
    """Move a completed task back to pending while preserving prior run history."""
    ensure_orchestrator_dirs()
    current_status, _ = find_task(task_id)
    if current_status != "completed":
        raise ValueError(f"requeue-completed requires completed task, got {current_status}: {task_id}")

    now = now_iso()
    task_path = _move_task_folder(task_id, "pending")

    def _mutate(registry: Dict[str, object]) -> Dict[str, object]:
        tasks = registry.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        record.update(
            {
                "status": "pending",
                "task_status": "pending",
                "started_at": None,
                "finished_at": None,
                "result_path": None,
                "report_path": None,
                "summary_short": f"Requeued completed task: {reason}",
                "manual_requeue_reason": reason,
                "manual_requeued_at": now,
                "status_transition_source": "manual_requeue",
                "status_transition_reason": reason,
                "status_transition_actor": "user",
                "requeue_from_completed": True,
            }
        )
        history = list(record.get("requeue_history", []) or [])
        history.append(
            {
                "at": now,
                "from_status": "completed",
                "to_status": "pending",
                "reason": reason,
                "latest_run_id": record.get("latest_run_id"),
                "latest_success_run_id": record.get("latest_success_run_id"),
            }
        )
        record["requeue_history"] = history
        return record

    updated_record = mutate_registry(_mutate)
    return {
        "task_id": task_id,
        "status": "pending",
        "task_path": str(task_path / "task.md") if task_path else None,
        "latest_run_id": updated_record.get("latest_run_id") if isinstance(updated_record, dict) else None,
        "latest_success_run_id": updated_record.get("latest_success_run_id") if isinstance(updated_record, dict) else None,
        "reason": reason,
        "preserved_previous_runs": True,
    }


def _failure_result(task: TaskBook, reason: str) -> Dict[str, object]:
    return {
        "task_id": task.task_id,
        "status": "failed",
        "worker": task.worker,
        "started_at": None,
        "finished_at": now_iso(),
        "summary_short": reason,
        "changed_files": [],
        "commands_run": [],
        "outputs": {"worker_notes": "worker_notes.md", "stdout": "stdout.log", "stderr": "stderr.log"},
        "risks": [reason],
        "next_actions": ["Inspect worker output and rerun the task."],
    }


def _validate_result_contract(task: TaskBook, payload: Dict[str, object], target: Path) -> List[str]:
    errors: List[str] = []
    if str(payload.get("task_id", "")) != task.task_id:
        errors.append("result.yaml task_id does not match task")
    if str(payload.get("status", "")) not in {"success", "partial", "failed"}:
        errors.append("result.yaml status is invalid")
    for field in ("worker", "summary_short"):
        if field not in payload:
            errors.append(f"result.yaml missing {field}")
    metrics_path = target / "metrics.json"
    if metrics_path.exists():
        try:
            json.loads(metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"metrics.json is not valid JSON: {exc}")
    code_changes = payload.get("code_changes") or {}
    if isinstance(code_changes, dict) and code_changes.get("changed") is True and not (target / "patch.diff").exists():
        errors.append("code_changes.changed=true but patch.diff is missing")
    latest_run_id = load_registry().get("tasks", {}).get(task.task_id, {}).get("latest_run_id")
    result_run_id = payload.get("run_id")
    if latest_run_id and result_run_id and str(result_run_id) != str(latest_run_id):
        errors.append("result.yaml run_id does not match latest registry run_id")
    return errors


def _audit_result_safety(target: Path) -> Dict[str, object]:
    scanned: Dict[str, str] = {}
    for name in ("stdout.log", "stderr.log", "worker_notes.md", "codex_stdout.log", "codex_stderr.log", "patch.diff"):
        path = target / name
        if path.exists() and path.is_file():
            scanned[name] = path.read_text(encoding="utf-8", errors="replace")[-20000:]
    patterns = {
        "destructive_rm": r"\brm\s+-[^\n]*r[^\n]*f\b|\brm\s+-[^\n]*f[^\n]*r\b",
        "git_reset": r"\bgit\s+reset\b",
        "git_clean": r"\bgit\s+clean\b",
        "package_install": r"\b(pip|pip3|conda|apt|apt-get)\s+install\b",
        "network_fetch": r"\b(wget|curl)\b\s+https?://",
        "sync_delete": r"\brsync\b[^\n]*--delete\b|\brclone\b[^\n]*\bsync\b",
        "target_label_terms": r"\b(DF40|NTIRE|target[_ -]?label|test[_ -]?label|leaderboard|private)\b",
    }
    negation = re.compile(r"\b(no|not|without|did not|was not|were not|未|沒有|没有|无)\b", flags=re.IGNORECASE)
    findings: List[Dict[str, object]] = []
    for filename, text in scanned.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            compact = line.strip()
            if not compact:
                continue
            for code, pattern in patterns.items():
                if re.search(pattern, compact, flags=re.IGNORECASE):
                    severity = "review"
                    if code in {"destructive_rm", "git_reset", "git_clean", "sync_delete"}:
                        severity = "high"
                    elif code in {"package_install", "network_fetch"}:
                        severity = "medium"
                    if negation.search(compact) and code not in {"target_label_terms"}:
                        severity = "note"
                    findings.append({"code": code, "severity": severity, "file": filename, "line": line_no, "text": compact[:300]})
    summary = {
        "schema_version": 1,
        "scanned_files": sorted(scanned),
        "finding_count": len(findings),
        "high_count": sum(1 for item in findings if item["severity"] == "high"),
        "medium_count": sum(1 for item in findings if item["severity"] == "medium"),
        "findings": findings[:200],
    }
    (target / "safety_audit.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _source_changed_files(payload: Dict[str, object]) -> List[str]:
    changed_files = [str(item).strip() for item in payload.get("changed_files", []) or [] if str(item).strip()]
    code_changes = payload.get("code_changes") or {}
    code_changed = isinstance(code_changes, dict) and code_changes.get("changed") is True
    if not changed_files and not code_changed:
        return []
    return changed_files


def _partial_input_sources_from_bindings(input_bindings: object) -> List[Dict[str, object]]:
    partial_input_sources: List[Dict[str, object]] = []
    if not isinstance(input_bindings, dict):
        return partial_input_sources
    for source_task_id, binding in input_bindings.items():
        if isinstance(binding, dict) and str(binding.get("source_status") or "") == "partial":
            partial_input_sources.append(
                {
                    "task_id": str(source_task_id),
                    "run_id": binding.get("run_id"),
                    "caveats": binding.get("caveats", []),
                    "binding_reason": binding.get("binding_reason"),
                }
            )
    return partial_input_sources


def _apply_partial_input_claim_limitation(payload: Dict[str, object], partial_input_sources: List[Dict[str, object]]) -> Dict[str, object]:
    if not partial_input_sources:
        return payload
    result = dict(payload)
    result["used_partial_inputs"] = True
    result["partial_input_sources"] = partial_input_sources
    limitations = list(result.get("claim_limitations", []) or [])
    limitations.append(
        {
            "type": "partial_input_claim_limitation",
            "execution_allowed": True,
            "formal_claim_allowed": False,
            "claim_level": "provisional",
            "reason": "This run used one or more partial upstream results; outputs may support diagnostics but must not be claimed as fully validated.",
            "partial_input_sources": partial_input_sources,
            "must_not_claim": [
                "all upstream dependencies completed successfully",
                "full final conclusion validated without caveats",
                "partial upstream artifacts are equivalent to completed inputs",
            ],
        }
    )
    result["claim_limitations"] = limitations
    result["claim_level"] = "provisional"
    result["formal_claim_allowed"] = False
    result["execution_allowed_with_claim_limitation"] = True
    risks = list(result.get("risks", []) or [])
    risks.append("Used partial upstream inputs; formal claims require caveats.")
    result["risks"] = risks
    return result


def _no_source_patch_message(task_id: str) -> str:
    return (
        f"# No source patch collected for {task_id}.\n"
        "# The worker result did not declare source-code changes, so collect() did not snapshot the shared worktree diff.\n"
    )


def _ensure_latest_result_compat(task_id: str) -> None:
    base = result_dir(task_id)
    latest = result_latest_dir(task_id)
    if latest.exists() or not (base / "result.yaml").exists():
        return
    latest.mkdir(parents=True, exist_ok=True)
    legacy_run = result_run_dir(task_id, "legacy")
    legacy_run.mkdir(parents=True, exist_ok=True)
    for item in base.iterdir():
        if item.name in {"latest", "runs"}:
            continue
        if item.is_dir():
            if (latest / item.name).exists():
                shutil.rmtree(latest / item.name)
            if (legacy_run / item.name).exists():
                shutil.rmtree(legacy_run / item.name)
            shutil.copytree(item, latest / item.name)
            shutil.copytree(item, legacy_run / item.name)
        else:
            shutil.copy2(item, latest / item.name)
            shutil.copy2(item, legacy_run / item.name)


def collect(task_id: str) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    _, task_path = find_task(task_id)
    task = load_task(task_path)
    out_dir = worker_out_dir(task_id)
    auto_sync = _auto_sync_before_collect(task_id, out_dir)
    result_yaml = out_dir / "result.yaml"
    run_id = None
    if result_yaml.exists():
        try:
            pre_payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8")) or {}
            run_id = pre_payload.get("run_id")
        except yaml.YAMLError:
            run_id = None
    if not run_id:
        run_id_path = out_dir / "run_id.txt"
        if run_id_path.exists():
            run_id = run_id_path.read_text(encoding="utf-8", errors="replace").strip()
    run_id = str(run_id or task_id)
    target = result_latest_dir(task_id)
    run_target = result_run_dir(task_id, run_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    if run_target.exists():
        shutil.rmtree(run_target)
    run_target.mkdir(parents=True, exist_ok=True)

    if not out_dir.exists():
        out_dir.mkdir(parents=True, exist_ok=True)

    if result_yaml.exists():
        payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8")) or {}
    else:
        payload = _failure_result(task, "missing _worker_out/result.yaml")
        result_yaml.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    _copy_file(task_path, target / "task.md")
    _copy_file(task_path, run_target / "task.md")

    for item in out_dir.iterdir():
        if item.is_dir():
            _copy_dir(item, target / item.name)
        else:
            _copy_file(item, target / item.name)

    for item in target.iterdir():
        if item.is_dir():
            _copy_dir(item, run_target / item.name)
        else:
            _copy_file(item, run_target / item.name)

    for required in ("worker_notes.md", "stdout.log", "stderr.log"):
        path = target / required
        if not path.exists():
            path.write_text("", encoding="utf-8")

    patch_path = target / "patch.diff"
    if not patch_path.exists():
        source_changed_files = _source_changed_files(payload)
        if source_changed_files:
            patch_path.write_text(git_diff(worktree_dir(task_id), source_changed_files), encoding="utf-8")
        else:
            patch_path.write_text(_no_source_patch_message(task_id), encoding="utf-8")

    contract_errors = _validate_result_contract(task, payload, target)
    from .contract_validator import validate_final_contract

    final_contract = validate_final_contract(task, target)
    (target / "final_contract_validation.json").write_text(
        json.dumps(final_contract.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if final_contract.checked and not final_contract.ok:
        contract_errors.extend(final_contract.errors)
    if contract_errors and payload.get("status") == "success":
        payload["status"] = "partial"
        payload["summary_short"] = str(payload.get("summary_short", "") or "") + " Result contract validation downgraded this run to partial."
        risks = list(payload.get("risks", []) or [])
        risks.extend(contract_errors)
        payload["risks"] = risks
        (target / "result.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if contract_errors:
        (target / "contract_errors.json").write_text(json.dumps(contract_errors, indent=2, ensure_ascii=False), encoding="utf-8")

    safety_audit = _audit_result_safety(target)
    shutil.copy2(target / "safety_audit.json", run_target / "safety_audit.json")
    if safety_audit.get("high_count") and payload.get("status") == "success":
        payload["status"] = "partial"
        payload["summary_short"] = str(payload.get("summary_short", "") or "") + " Safety audit found high-risk log patterns; downgraded to partial pending review."
        risks = list(payload.get("risks", []) or [])
        risks.append("Safety audit found high-risk log patterns.")
        payload["risks"] = risks
        (target / "result.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        shutil.copy2(target / "result.yaml", run_target / "result.yaml")
    current_record = load_registry().get("tasks", {}).get(task_id, {})
    input_bindings = current_record.get("input_bindings") if isinstance(current_record, dict) else {}
    partial_input_sources = _partial_input_sources_from_bindings(input_bindings)
    used_partial_inputs = bool(partial_input_sources)
    if used_partial_inputs:
        payload = _apply_partial_input_claim_limitation(payload, partial_input_sources)
        (target / "result.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if (target / "result.yaml").exists():
        shutil.copy2(target / "result.yaml", run_target / "result.yaml")
    if (target / "final_contract_validation.json").exists():
        shutil.copy2(target / "final_contract_validation.json", run_target / "final_contract_validation.json")
    if (target / "contract_errors.json").exists():
        shutil.copy2(target / "contract_errors.json", run_target / "contract_errors.json")

    result = worker_result_from_dict(payload)
    source_changed_files = _source_changed_files(payload)
    diff_stat = git_diff_stat(worktree_dir(task_id), source_changed_files) if source_changed_files else ""
    report_path = write_task_report(target / "report.md", task, result, target, diff_stat=diff_stat)
    write_task_report(run_target / "report.md", task, result, run_target, diff_stat=diff_stat)
    update_after_collect(task, result, report_path)
    mark_from_result(task, result, target, report_path)
    update_task_record(task_id, latest_run_id=run_id, latest_result_path=str(target), latest_report_path=str(report_path), archived_result_path=str(run_target))
    if auto_sync:
        update_task_record(task_id, remote_auto_sync=auto_sync)

    if result.status == "success":
        final_status = "completed"
    elif result.status == "partial":
        final_status = "partial"
    else:
        final_status = "failed"
    artifact_metadata = _artifact_contract(run_target)
    usable_as_input = final_status in {"completed", "partial"} and bool(artifact_metadata.get("ok"))
    caveats: List[str] = []
    if final_status == "partial":
        caveats.append("Run ended partial; artifacts may be usable for analysis but not for final claims.")
    partial_info = payload.get("partial")
    if isinstance(partial_info, dict):
        caveats.extend(str(item) for item in partial_info.get("caveats", []) or [])
    continuation = _emit_continuation_task(task, payload, result.status)
    _update_run_record(
        task_id,
        run_id,
        status=final_status,
        task_status=final_status,
        run_phase="collected",
        state=final_status,
        remote_state="collected",
        finished_at=result.finished_at or now_iso(),
        collected_at=now_iso(),
        result_path=str(run_target),
        report_path=str(run_target / "report.md"),
        summary_short=result.summary_short,
        artifact_contract_valid=bool(artifact_metadata.get("ok")),
        usable_as_input=usable_as_input,
        available_artifacts=artifact_metadata.get("available_artifacts", {}),
        missing_artifacts=artifact_metadata.get("missing_artifacts", []),
        invalid_artifacts=artifact_metadata.get("invalid_artifacts", []),
        caveats=caveats,
        used_partial_inputs=used_partial_inputs,
        partial_input_sources=partial_input_sources,
        continuation_emit=continuation,
    )
    if used_partial_inputs:
        update_task_record(task_id, used_partial_inputs=True, partial_input_sources=partial_input_sources)
    failure_action_request = None
    artifact_action_requests = create_artifact_action_requests(task_id, run_id=run_id, result_path=target)
    if artifact_action_requests.get("created") or artifact_action_requests.get("existing") or artifact_action_requests.get("skipped"):
        _update_run_record(
            task_id,
            run_id,
            artifact_action_requests=artifact_action_requests,
            artifact_action_request_ids=[
                item.get("action_id")
                for item in list(artifact_action_requests.get("created", []) or []) + list(artifact_action_requests.get("existing", []) or [])
                if item.get("action_id")
            ],
        )
    failure_type = str(payload.get("failure_type") or "")
    recommended = payload.get("recommended_action") if isinstance(payload.get("recommended_action"), dict) else recommended_action_for_failure(failure_type)
    if failure_type and isinstance(recommended, dict) and recommended.get("type") == "create_action_request":
        failure_action_request = create_failure_action_request(
            task_id,
            run_id=run_id,
            failure_type=failure_type,
            recommended_action=recommended,
            result_path=str(target / "result.yaml"),
            reason=str(payload.get("summary_short") or f"{failure_type} reported by collected result"),
        )
        if failure_action_request.get("action_id"):
            _update_run_record(
                task_id,
                run_id,
                failure_action_request_id=failure_action_request.get("action_id"),
                failure_action_request_status=failure_action_request.get("status"),
            )
    try:
        from solution_agent.context_index import index_collected_result

        context_index_update = index_collected_result(root(), task_id=task_id, result_path=target)
    except Exception as exc:
        context_index_update = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _refresh_task_run_index(task_id)
    current_status, current_task = find_task(task_id)
    destination = task_dir(final_status, task_id)
    if current_status != final_status:
        if destination.exists():
            shutil.rmtree(destination)
        current_task.parent.rename(destination)
    return {
        "status": final_status,
        "result_path": str(target),
        "run_result_path": str(run_target),
        "report_path": str(report_path),
        "auto_sync": auto_sync,
        "failure_action_request": failure_action_request,
        "artifact_action_requests": artifact_action_requests,
        "context_index_update": context_index_update,
        "continuation": continuation,
    }


def report(task_id: str) -> Path:
    _, task_path = find_task(task_id)
    task = load_task(task_path)
    _ensure_latest_result_compat(task_id)
    target = result_latest_dir(task_id)
    result_yaml = target / "result.yaml"
    if not result_yaml.exists():
        raise FileNotFoundError(f"result.yaml not found for {task_id}; run collect first")
    result = worker_result_from_dict(yaml.safe_load(result_yaml.read_text(encoding="utf-8")) or {})
    return write_task_report(target / "report.md", task, result, target, diff_stat="")


def report_index() -> Path:
    return write_index(root() / "reports" / "experiment_index.md", load_registry())


def report_latest() -> Path:
    return write_latest_summary(root() / "reports" / "latest_summary.md", load_registry())


def export_gpt_bundle(name: Optional[str] = None, include_running: bool = True, include_partial: bool = True) -> Dict[str, object]:
    ensure_orchestrator_dirs()
    timestamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").replace("T", "_")
    bundle_dir = root() / "exports" / (name or f"gpt_bundle_{timestamp}")
    if bundle_dir.exists():
        shutil.rmtree(_fs_path(bundle_dir))
    for subdir in ("tasks", "reports", "results", "summaries", "all_md"):
        (bundle_dir / subdir).mkdir(parents=True, exist_ok=True)

    registry = load_registry()
    included: List[Dict[str, object]] = []
    statuses = {"completed"}
    if include_partial:
        statuses.add("partial")
    if include_running:
        statuses.add("running")

    def copy_if_exists(src: Path, dst: Path) -> Optional[str]:
        if not src.exists():
            return None
        _copy_file(src, dst)
        return str(dst)

    for task_id, record in sorted((registry.get("tasks") or {}).items()):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status not in statuses:
            continue
        task_bucket = bundle_dir / "tasks" / str(task_id)
        report_bucket = bundle_dir / "reports" / str(task_id)
        result_bucket = bundle_dir / "results" / str(task_id)
        task_bucket.mkdir(parents=True, exist_ok=True)
        report_bucket.mkdir(parents=True, exist_ok=True)
        result_bucket.mkdir(parents=True, exist_ok=True)

        copied: Dict[str, object] = {}
        for state in ("pending", "running", "completed", "failed", "partial"):
            task_path = task_file(state, str(task_id))
            copied_path = copy_if_exists(task_path, task_bucket / "task.md")
            if copied_path:
                copied["task_md"] = copied_path
                break

        latest = result_latest_dir(str(task_id))
        for filename in ("report.md", "worker_notes.md", "result.yaml", "metrics.json", "safety_audit.json", "contract_errors.json", "final_contract_validation.json"):
            copied_path = copy_if_exists(latest / filename, result_bucket / filename)
            if copied_path:
                copied[filename] = copied_path
        copied_path = copy_if_exists(latest / "task.md", task_bucket / "collected_task.md")
        if copied_path:
            copied["collected_task_md"] = copied_path

        included.append({"task_id": task_id, "status": status, "copied": copied})

    copy_if_exists(root() / "reports" / "experiment_index.md", bundle_dir / "summaries" / "experiment_index.md")
    copy_if_exists(root() / "reports" / "latest_summary.md", bundle_dir / "summaries" / "latest_summary.md")
    all_md_dir = bundle_dir / "all_md"
    merged_sections: List[str] = ["# All Experiment Markdown", ""]
    for item in included:
        task_id = str(item["task_id"])
        status = str(item["status"])
        merged_sections.extend([f"# {task_id}", "", f"Status: `{status}`", ""])
        source_pairs = [
            ("task", bundle_dir / "tasks" / task_id / "task.md"),
            ("collected_task", bundle_dir / "tasks" / task_id / "collected_task.md"),
            ("report", bundle_dir / "results" / task_id / "report.md"),
            ("worker_notes", bundle_dir / "results" / task_id / "worker_notes.md"),
        ]
        for label, src in source_pairs:
            if not src.exists():
                continue
            flat_name = f"{task_id}__{label}.md"
            dst = all_md_dir / flat_name
            _copy_file(src, dst)
            merged_sections.extend([f"## {label}", "", src.read_text(encoding="utf-8", errors="replace"), ""])
    (all_md_dir / "ALL.md").write_text("\n".join(merged_sections), encoding="utf-8")
    manifest = {
        "created_at": now_iso(),
        "bundle_dir": str(bundle_dir),
        "include_running": include_running,
        "include_partial": include_partial,
        "tasks": included,
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    readme_lines = [
        "# GPT Bundle",
        "",
        f"Created at: {manifest['created_at']}",
        "",
        "## Contents",
        "",
        "- `tasks/`: original task books and collected task copies",
        "- `reports/`: reserved for report-focused copies",
        "- `results/`: report.md, worker_notes.md, result.yaml, metrics.json, audits",
        "- `summaries/`: experiment index and latest summary",
        "- `manifest.json`: machine-readable file list",
        "",
        "## Included Tasks",
        "",
    ]
    readme_lines.extend(f"- {item['task_id']}: {item['status']}" for item in included)
    (bundle_dir / "README.md").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Resync sweep (inspired by Optio reconcile-worker pattern)
# ---------------------------------------------------------------------------

_STALE_RUNNING_STATES = frozenset({"running", "launching", "launch_unknown"})


def stale_running_recovery(
    stale_after_minutes: int = 120,
    heartbeat_stale_seconds: int = 600,
) -> Dict[str, object]:
    """Scan for tasks stuck in running/launching/launch_unknown with no live
    heartbeat and transition them to failed.

    Called by the scheduler resync sweep every N cycles to catch tasks that
    were orphaned by a crashed scheduler or a lost event.  The function only
    marks tasks stale; it never kills any remote process.

    A task is considered stale when BOTH conditions hold:
      1. started_at is older than stale_after_minutes
      2. The local heartbeat.json either does not exist or its heartbeat_at
         timestamp is older than heartbeat_stale_seconds
    """
    recovered: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []

    registry = load_registry()
    tasks = registry.get("tasks", {})

    for task_id, record in tasks.items():
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status not in _STALE_RUNNING_STATES:
            continue

        started_age = _iso_age_seconds(record.get("started_at"))
        if started_age is None or started_age < stale_after_minutes * 60:
            skipped.append({"task_id": task_id, "reason": "started_at too recent", "started_age_s": started_age})
            continue

        # Check local heartbeat.json written by the remote runner loop.
        hb_path = worker_out_dir(str(task_id)) / "heartbeat.json"
        hb_age: Optional[int] = None
        if hb_path.exists():
            try:
                hb_data = json.loads(hb_path.read_text(encoding="utf-8"))
                hb_age = _iso_age_seconds(hb_data.get("heartbeat_at") or hb_data.get("updated_at"))
            except Exception:
                hb_age = None

        if hb_age is not None and hb_age < heartbeat_stale_seconds:
            skipped.append({"task_id": task_id, "reason": "heartbeat recent", "heartbeat_age_s": hb_age})
            continue

        update_task_record(
            str(task_id),
            status="failed",
            task_status="failed",
            finished_at=now_iso(),
            summary_short="stale recovery: no heartbeat after scheduled run",
            status_transition_source="stale_lock_recovery",
            status_transition_reason=(
                f"started_age={started_age}s hb_age={hb_age}s "
                f"threshold={stale_after_minutes}min/{heartbeat_stale_seconds}s"
            ),
            status_transition_actor="scheduler",
        )
        recovered.append(
            {
                "task_id": task_id,
                "from_status": status,
                "started_age_s": started_age,
                "heartbeat_age_s": hb_age,
            }
        )

    return {"recovered": recovered, "skipped": skipped}


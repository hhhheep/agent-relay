from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml


_PLACEHOLDER = re.compile(r"(<[^>]+>|\b(?:todo|tbd|placeholder)\b)", re.IGNORECASE)
_TERMINAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BOOTSTRAP_MODES = {"existing_checkout", "release_archive", "git_ref"}


@dataclass(frozen=True)
class RemoteDeployProfile:
    name: str
    binding_id: str
    host: str
    solution_agent_root: str
    project_workdir: str
    remote_run_root: str
    codex_path: str
    python_bootstrap: str
    plugin_home: str
    personal_marketplace: str
    provider_env_file: str
    terminal_hub_name: str
    terminal_default: str
    terminal_slots: dict[str, dict[str, str]]
    terminal_segment_bytes: int
    terminal_retain_segments: int
    terminal_max_children: int
    terminal_storage_profile_id: str
    job_backend: str
    job_max_concurrent: int
    initialize_checkout: bool
    bootstrap_mode: str
    bootstrap_source: str | None
    bootstrap_sha256: str | None
    worker_pool: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.worker_pool is None:
            result.pop("worker_pool")
        return result


def _absolute_remote_path(value: Any, field: str, errors: list[str]) -> str:
    text = str(value or "").strip()
    if not text:
        errors.append(f"{field} is required")
    elif _PLACEHOLDER.search(text):
        errors.append(f"{field} contains a placeholder")
    elif not PurePosixPath(text).is_absolute():
        errors.append(f"{field} must be an absolute POSIX path")
    return text


def parse_remote_deploy_profile(name: str, value: dict[str, Any]) -> RemoteDeployProfile:
    errors: list[str] = []
    binding_id = str(value.get("binding_id") or name).strip()
    if not _TERMINAL_ID.fullmatch(binding_id):
        errors.append("binding_id must be a stable profile identifier")
    host = str(value.get("host") or "").strip()
    if not host or _PLACEHOLDER.search(host):
        errors.append("host is required and cannot be a placeholder")
    solution_root = _absolute_remote_path(value.get("solution_agent_root"), "solution_agent_root", errors)
    project_workdir = _absolute_remote_path(value.get("project_workdir"), "project_workdir", errors)
    remote_run_root = _absolute_remote_path(value.get("remote_run_root"), "remote_run_root", errors)
    codex_path = _absolute_remote_path(value.get("codex_path"), "codex_path", errors)
    python_bootstrap = _absolute_remote_path(value.get("python_bootstrap"), "python_bootstrap", errors)
    plugin_home = _absolute_remote_path(value.get("plugin_home"), "plugin_home", errors)
    personal_marketplace = _absolute_remote_path(value.get("personal_marketplace"), "personal_marketplace", errors)
    provider_env_file = _absolute_remote_path(value.get("provider_env_file"), "provider_env_file", errors)
    remote_terminals = value.get("remote_terminals") if isinstance(value.get("remote_terminals"), dict) else {}
    terminal_default = str(remote_terminals.get("default") or "").strip()
    terminal_hub_name = str(remote_terminals.get("hub_name") or "").strip()
    if not _TERMINAL_ID.fullmatch(terminal_hub_name):
        errors.append("remote_terminals.hub_name must be a stable tmux session identifier")
    terminal_storage_profile_id = str(remote_terminals.get("storage_profile_id") or name).strip()
    if not _TERMINAL_ID.fullmatch(terminal_storage_profile_id):
        errors.append("remote_terminals.storage_profile_id must be a stable profile identifier")
    slots_value = remote_terminals.get("slots") if isinstance(remote_terminals.get("slots"), dict) else {}
    terminal_slots: dict[str, dict[str, str]] = {}
    terminal_windows: set[str] = set()
    for terminal_id, slot_value in slots_value.items():
        terminal_id = str(terminal_id)
        if not _TERMINAL_ID.fullmatch(terminal_id) or not isinstance(slot_value, dict):
            errors.append(f"invalid remote terminal id: {terminal_id}")
            continue
        window_name = str(slot_value.get("window_name") or "").strip()
        cwd = _absolute_remote_path(slot_value.get("cwd"), f"remote_terminals.slots.{terminal_id}.cwd", errors)
        shell = _absolute_remote_path(slot_value.get("shell"), f"remote_terminals.slots.{terminal_id}.shell", errors)
        if not _TERMINAL_ID.fullmatch(window_name):
            errors.append(f"remote_terminals.slots.{terminal_id}.window_name is invalid")
        if window_name in terminal_windows:
            errors.append("remote terminal slots must use distinct tmux window names")
        terminal_windows.add(window_name)
        terminal_slots[terminal_id] = {"window_name": window_name, "cwd": cwd, "shell": shell}
    if not terminal_slots:
        errors.append("remote_terminals.slots must declare at least one fixed terminal")
    if terminal_default not in terminal_slots:
        errors.append("remote_terminals.default must name one declared fixed terminal")
    try:
        terminal_segment_bytes = int(remote_terminals.get("segment_bytes", 4 * 1024 * 1024))
        terminal_retain_segments = int(remote_terminals.get("retain_segments", 16))
        terminal_max_children = int(remote_terminals.get("max_children", 8))
    except (TypeError, ValueError):
        terminal_segment_bytes = 0
        terminal_retain_segments = 0
        terminal_max_children = 0
    if not 64 * 1024 <= terminal_segment_bytes <= 64 * 1024 * 1024:
        errors.append("remote terminal segment_bytes is outside 65536..67108864")
    if not 2 <= terminal_retain_segments <= 256:
        errors.append("remote terminal retain_segments is outside 2..256")
    if not 1 <= terminal_max_children <= 64:
        errors.append("remote terminal max_children must be between 1 and 64")
    jobs = value.get("jobs") if isinstance(value.get("jobs"), dict) else {}
    job_backend = str(jobs.get("backend") or "").strip()
    if job_backend not in {"systemd_transient", "detached_process_supervisor"}:
        errors.append("jobs.backend must be systemd_transient or detached_process_supervisor")
    try:
        job_max_concurrent = int(jobs.get("max_concurrent"))
    except (TypeError, ValueError):
        job_max_concurrent = 0
    if not 1 <= job_max_concurrent <= 64:
        errors.append("jobs.max_concurrent must be between 1 and 64")
    worker_pool = None
    if value.get("worker_pool") is not None:
        from solution_agent.harness.worker_sessions import validate_policy
        try:
            worker_pool = validate_policy(value["worker_pool"])
            if worker_pool["max_running"] > job_max_concurrent:
                errors.append("worker_pool exceeds jobs.max_concurrent")
        except ValueError as exc:
            errors.append(str(exc))

    role_paths = {
        "solution_agent_root": solution_root,
        "project_workdir": project_workdir,
        "remote_run_root": remote_run_root,
    }
    populated = [path for path in role_paths.values() if path]
    if len(populated) != len(set(populated)):
        errors.append("solution_agent_root, project_workdir, and remote_run_root must be distinct")
    root_parts = [part.lower() for part in PurePosixPath(solution_root).parts]
    if "runs" in root_parts or any(part.startswith("old-run") for part in root_parts):
        errors.append("solution_agent_root cannot be a historical run snapshot")

    initialize = bool(value.get("initialize_checkout", False))
    bootstrap = value.get("bootstrap") if isinstance(value.get("bootstrap"), dict) else {}
    bootstrap_mode = str(bootstrap.get("mode") or "existing_checkout").strip()
    bootstrap_source = str(bootstrap.get("source") or "").strip() or None
    bootstrap_sha256 = str(bootstrap.get("sha256") or "").strip().lower() or None
    if bootstrap_mode not in _BOOTSTRAP_MODES:
        errors.append(f"bootstrap.mode must be one of {sorted(_BOOTSTRAP_MODES)}")
    if initialize and bootstrap_mode == "existing_checkout":
        errors.append("initialize_checkout=true requires release_archive or git_ref bootstrap")
    if bootstrap_mode in {"release_archive", "git_ref"} and not bootstrap_source:
        errors.append(f"bootstrap.source is required for {bootstrap_mode}")
    if bootstrap_mode == "release_archive":
        if not bootstrap_sha256 or len(bootstrap_sha256) != 64 or any(char not in "0123456789abcdef" for char in bootstrap_sha256):
            errors.append("release_archive bootstrap requires a hexadecimal SHA-256")

    if errors:
        raise ValueError("invalid remote deployment profile:\n- " + "\n- ".join(errors))
    return RemoteDeployProfile(
        name=name,
        binding_id=binding_id,
        host=host,
        solution_agent_root=solution_root,
        project_workdir=project_workdir,
        remote_run_root=remote_run_root,
        codex_path=codex_path,
        python_bootstrap=python_bootstrap,
        plugin_home=plugin_home,
        personal_marketplace=personal_marketplace,
        provider_env_file=provider_env_file,
        terminal_hub_name=terminal_hub_name,
        terminal_default=terminal_default,
        terminal_slots=terminal_slots,
        terminal_segment_bytes=terminal_segment_bytes,
        terminal_retain_segments=terminal_retain_segments,
        terminal_max_children=terminal_max_children,
        terminal_storage_profile_id=terminal_storage_profile_id,
        job_backend=job_backend,
        job_max_concurrent=job_max_concurrent,
        initialize_checkout=initialize,
        bootstrap_mode=bootstrap_mode,
        bootstrap_source=bootstrap_source,
        bootstrap_sha256=bootstrap_sha256,
        worker_pool=worker_pool,
    )


def load_remote_deploy_profile(path, name: str) -> RemoteDeployProfile:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if payload.get("schema_version") != 1 or not isinstance(payload.get("profiles"), dict):
        raise ValueError("remote deployment profile file must use schema_version: 1 and profiles mapping")
    value = payload["profiles"].get(name)
    if not isinstance(value, dict):
        raise ValueError(f"remote deployment profile not found: {name}")
    return parse_remote_deploy_profile(name, value)


def deployment_profiles_path(solution_root):
    """External per-machine profiles; legacy checkout path remains the fallback."""
    import os
    from pathlib import Path
    configured = os.environ.get("SOLUTION_AGENT_PROFILES_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(solution_root) / "config" / "harness_remote_deploy_profiles.yaml"

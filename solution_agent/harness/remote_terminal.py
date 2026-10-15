from __future__ import annotations

import argparse
import base64
import binascii
import errno
import getpass
import hashlib
import hmac
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ALLOWED_CONTROLS = {"enter": "Enter", "interrupt": "C-c", "eof": "C-d"}
_LOGGER_PROTOCOL = "2"
_WORKPLACE_IDENTITY_RELATIVE = Path(".solution-agent") / "workplace.id"
_WORKPLACE_IDENTITY_ENV = "SOLUTION_AGENT_WORKPLACE_IDENTITY_B64"
_WORKPLACE_IDENTITY_RE = re.compile(rb"solution-agent-workplace-v1:[0-9a-f]{32}\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json_retry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    last: Exception | None = None
    for _ in range(3):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.02)
    if last is not None:
        raise last
    return {}


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise TerminalError("path_outside_run_root", f"path is outside terminal run root: {path}") from exc
    return resolved


class TerminalError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = True,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.next_action = next_action

    def result(self, operation: str, profile_id: str | None, terminal_id: str | None) -> dict[str, Any]:
        return {
            "ok": False,
            "operation": operation,
            "error": {
                "code": self.code,
                "message": str(self)[:1200],
                "recoverable": self.recoverable,
                "next_action": self.next_action,
            },
            "profile_id": profile_id,
            "terminal_id": terminal_id,
        }


def _validate_read_window(after_cursor: int, max_output_bytes: int) -> None:
    if (type(after_cursor) is not int or after_cursor < 0
            or type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 65536):
        raise TerminalError("invalid_read_window", "after_cursor must be a non-negative integer and max_output_bytes must be an integer in 1..65536")


@dataclass(frozen=True)
class TerminalSlot:
    terminal_id: str
    window_name: str
    cwd: Path
    shell: str


@dataclass(frozen=True)
class TerminalProfile:
    profile_id: str
    deployment_profile_name: str
    solution_root: Path
    run_root: Path
    hub_name: str
    default_terminal: str
    slots: dict[str, TerminalSlot]
    segment_bytes: int
    retain_segments: int
    max_children: int = 8
    job_backend: str = "detached_process_supervisor"
    job_max_concurrent: int = 4
    storage_profile_id: str | None = None


def _canonical_workplace_identity(value: bytes) -> bytes:
    if not _WORKPLACE_IDENTITY_RE.fullmatch(value):
        raise TerminalError(
            "workplace_identity_invalid",
            "workplace.id must be canonical UTF-8: solution-agent-workplace-v1:<32 lowercase hex> followed by one LF",
            recoverable=False,
        )
    return value


def verify_workplace_identity(profile: TerminalProfile) -> dict[str, Any]:
    encoded = os.environ.get(_WORKPLACE_IDENTITY_ENV, "").strip()
    if not encoded:
        raise TerminalError(
            "workplace_identity_missing",
            f"local launcher did not provide {_WORKPLACE_IDENTITY_ENV}",
            recoverable=False,
        )
    try:
        local_bytes = _canonical_workplace_identity(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise TerminalError(
            "workplace_identity_invalid",
            "local workplace identity is not valid canonical base64",
            recoverable=False,
        ) from exc
    remote_path = profile.slots[profile.default_terminal].cwd / _WORKPLACE_IDENTITY_RELATIVE
    try:
        remote_bytes = _canonical_workplace_identity(remote_path.read_bytes())
    except OSError as exc:
        raise TerminalError(
            "workplace_identity_missing",
            f"remote workplace identity is unavailable: {remote_path}",
            recoverable=False,
        ) from exc
    if not hmac.compare_digest(local_bytes, remote_bytes):
        raise TerminalError(
            "workplace_identity_mismatch",
            "local folder and remote workplace identity files differ",
            recoverable=False,
        )
    return {
        "path": str(remote_path),
        "sha256": hashlib.sha256(remote_bytes).hexdigest(),
    }


def load_terminal_profile(solution_root: Path) -> TerminalProfile:
    if os.environ.get("SOLUTION_AGENT_TERMINAL_NESTING") == "1":
        raise TerminalError(
            "nested_terminal_controller",
            "a Codex process running inside a managed terminal cannot control the same terminal service",
            recoverable=False,
        )
    from solution_agent.harness.remote_profile import deployment_profiles_path
    config_path = deployment_profiles_path(solution_root)
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TerminalError("profile_config_unavailable", f"cannot read terminal profile config: {exc}") from exc
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise TerminalError("profile_identity_missing", "remote deploy profile config has no profiles")
    requested = os.environ.get("SOLUTION_AGENT_PROFILE_ID", "").strip()
    if requested:
        profile_id = requested
    elif len(profiles) == 1:
        profile_id = str(next(iter(profiles)))
    else:
        raise TerminalError("profile_identity_missing", "SOLUTION_AGENT_PROFILE_ID is required when multiple profiles exist")
    value = profiles.get(profile_id)
    if not isinstance(value, dict):
        raise TerminalError("profile_identity_missing", f"unknown terminal profile: {profile_id}")
    configured_root = Path(str(value.get("solution_agent_root") or ""))
    if os.name != "nt" and configured_root.resolve() != solution_root.resolve():
        raise TerminalError(
            "profile_root_mismatch",
            f"profile solution root {configured_root} does not match runtime root {solution_root}",
            recoverable=False,
        )
    run_root_text = str(value.get("remote_run_root") or "").strip()
    terminal_config = value.get("remote_terminals")
    if not run_root_text or not isinstance(terminal_config, dict):
        raise TerminalError("terminal_profile_missing", f"profile {profile_id} has no remote_terminals declaration")
    binding_id = str(value.get("binding_id") or profile_id).strip()
    storage_profile_id = str(terminal_config.get("storage_profile_id") or profile_id).strip()
    if not _ID_RE.fullmatch(binding_id) or not _ID_RE.fullmatch(storage_profile_id):
        raise TerminalError("terminal_profile_invalid", "binding_id and storage_profile_id must be stable profile identifiers")
    slots_value = terminal_config.get("slots")
    hub_name = str(terminal_config.get("hub_name") or "").strip()
    default_terminal = str(terminal_config.get("default") or "").strip()
    if not _ID_RE.fullmatch(hub_name):
        raise TerminalError("terminal_profile_invalid", "remote_terminals.hub_name must be a stable tmux session identifier")
    if not isinstance(slots_value, dict) or not slots_value or default_terminal not in slots_value:
        raise TerminalError("terminal_profile_invalid", "remote_terminals must declare slots and a valid default")
    slots: dict[str, TerminalSlot] = {}
    window_names: set[str] = set()
    for terminal_id, slot_value in slots_value.items():
        terminal_id = str(terminal_id)
        if not _ID_RE.fullmatch(terminal_id) or not isinstance(slot_value, dict):
            raise TerminalError("terminal_profile_invalid", f"invalid fixed terminal id: {terminal_id}")
        window_name = str(slot_value.get("window_name") or "").strip()
        cwd_text = str(slot_value.get("cwd") or "").strip()
        cwd = Path(cwd_text)
        shell = str(slot_value.get("shell") or "").strip()
        if not _ID_RE.fullmatch(window_name):
            raise TerminalError("terminal_profile_invalid", f"invalid tmux window name for {terminal_id}")
        # Terminal declarations describe the remote Linux host even when a
        # Windows control checkout is only running ``server.py --check``.
        if not PurePosixPath(cwd_text).is_absolute() or not PurePosixPath(shell).is_absolute():
            raise TerminalError("terminal_profile_invalid", f"terminal {terminal_id} needs absolute cwd and shell")
        if window_name in window_names:
            raise TerminalError("terminal_profile_invalid", f"duplicate tmux window name: {window_name}")
        window_names.add(window_name)
        slots[terminal_id] = TerminalSlot(terminal_id=terminal_id, window_name=window_name, cwd=cwd, shell=shell)
    segment_bytes = int(terminal_config.get("segment_bytes", 4 * 1024 * 1024))
    retain_segments = int(terminal_config.get("retain_segments", 16))
    if not 64 * 1024 <= segment_bytes <= 64 * 1024 * 1024 or not 2 <= retain_segments <= 256:
        raise TerminalError("terminal_profile_invalid", "terminal segment limits are outside supported bounds")
    jobs = value.get("jobs") if isinstance(value.get("jobs"), dict) else {}
    job_backend = str(jobs.get("backend") or "").strip()
    if job_backend not in {"systemd_transient", "detached_process_supervisor"}:
        raise TerminalError("terminal_profile_invalid", "jobs.backend is not supported")
    try:
        job_max_concurrent = int(jobs.get("max_concurrent"))
    except (TypeError, ValueError) as exc:
        raise TerminalError("terminal_profile_invalid", "jobs.max_concurrent is invalid") from exc
    if not 1 <= job_max_concurrent <= 64:
        raise TerminalError("terminal_profile_invalid", "jobs.max_concurrent must be 1..64")
    try:
        max_children = int(terminal_config.get("max_children", 8))
    except (TypeError, ValueError) as exc:
        raise TerminalError("terminal_profile_invalid", "remote_terminals.max_children is invalid") from exc
    if not 1 <= max_children <= 64:
        raise TerminalError("terminal_profile_invalid", "remote_terminals.max_children must be 1..64")
    return TerminalProfile(
        profile_id=binding_id,
        deployment_profile_name=profile_id,
        solution_root=solution_root,
        run_root=Path(run_root_text),
        hub_name=hub_name,
        default_terminal=default_terminal,
        slots=slots,
        segment_bytes=segment_bytes,
        retain_segments=retain_segments,
        max_children=max_children,
        job_backend=job_backend,
        job_max_concurrent=job_max_concurrent,
        storage_profile_id=storage_profile_id,
    )


def _windows_process_identity(pid: int) -> dict[str, Any]:
    """Query creation time without signalling a process or trusting PID alone."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return {"state": "exited" if ctypes.get_last_error() == 87 else "unknown"}
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return {"state": "unknown"}
        return {"state": "alive" if code.value == 259 else "exited",
                "process_start_marker": str((created.dwHighDateTime << 32) | created.dwLowDateTime)}
    finally:
        kernel.CloseHandle(handle)


def _lease_process_identity() -> dict[str, Any]:
    from solution_agent.harness.terminal_commands import _boot_id, _process_marker

    marker = (_windows_process_identity(os.getpid()).get("process_start_marker")
              if os.name == "nt" else _process_marker(os.getpid()))
    return {"pid": os.getpid(), "process_start_marker": marker,
            "host_boot_id": _boot_id() if os.name != "nt" else None}


def _lease_process_observation(record: dict[str, Any]) -> dict[str, Any]:
    pid, marker = record.get("pid"), record.get("process_start_marker")
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or not isinstance(marker, str) or not marker:
        return {"state": "unknown", "reason": "process_identity_missing"}
    if os.name != "nt":
        from solution_agent.harness.terminal_commands import _process_snapshot

        if not isinstance(record.get("host_boot_id"), str) or not record["host_boot_id"]:
            return {"state": "unknown", "reason": "boot_identity_missing"}
        return _process_snapshot(pid, marker, record.get("host_boot_id"))
    observed = _windows_process_identity(pid)
    if observed.get("state") != "alive":
        return {"state": observed["state"], "reason": "process_absent_or_unreadable"}
    if observed.get("process_start_marker") != marker:
        return {"state": "exited", "reason": "pid_reused"}
    return {"state": "alive", "reason": "identity_verified"}


class _Lease:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        os.chmod(path, 0o600)
        self.locked = False

    def acquire(self) -> None:
        lock_attempted = False
        try:
            if os.name == "nt":
                import msvcrt

                # Windows byte-range locks may extend beyond EOF. Do not read
                # the range before locking: an existing writer denies that read.
                self.handle.seek(0)
                lock_attempted = True
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                lock_attempted = True
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
        except OSError as exc:
            self.handle.close()
            contention = {errno.EAGAIN, errno.EWOULDBLOCK}
            if os.name == "nt":
                contention.add(errno.EACCES)
            if not lock_attempted or exc.errno not in contention:
                raise TerminalError(
                    "terminal_lock_failed",
                    f"terminal writer lease lock failed (errno={exc.errno}); owner unknown",
                    next_action="Inspect the OS lock error; do not remove the lock file or terminate a presumed owner.",
                ) from exc
            raise TerminalError(
                "terminal_busy",
                f"another Codex client owns the fixed terminal writer lease (errno={exc.errno})",
                next_action="Continue in the owning task or wait for that MCP client to exit; do not force-steal the lease.",
            ) from exc

    def release(self) -> None:
        if self.handle.closed:
            return
        if self.locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.locked = False
        self.handle.close()


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class ProfileTerminalManager:
    def __init__(self, profile: TerminalProfile, *, runner: Runner = subprocess.run) -> None:
        self.profile = profile
        self.runner = runner
        self.base = profile.run_root / "terminal-sessions" / (profile.storage_profile_id or profile.profile_id)
        self.socket_path = self.base / "tmux.sock"
        self._leases: dict[str, _Lease] = {}
        self.connection_id = uuid.uuid4().hex

    def _slot(self, terminal_id: str | None) -> TerminalSlot:
        selected = terminal_id or self.profile.default_terminal
        slot = self.profile.slots.get(selected)
        if slot is not None:
            return slot
        if not _ID_RE.fullmatch(selected):
            raise TerminalError("terminal_id_invalid", f"terminal_id is invalid: {selected}")
        template = self.profile.slots[self.profile.default_terminal]
        digest = hashlib.sha256(selected.encode("utf-8")).hexdigest()[:16]
        return TerminalSlot(
            terminal_id=selected,
            window_name=f"task-{digest}",
            cwd=template.cwd,
            shell=template.shell,
        )

    def _slot_root(self, slot: TerminalSlot) -> Path:
        return _inside(self.base / slot.terminal_id, self.base)

    def _tmux(self, *arguments: str, input_bytes: bytes | None = None, check: bool = False) -> subprocess.CompletedProcess[bytes]:
        command = ["tmux", "-S", str(self.socket_path), *arguments]
        try:
            result = self.runner(
                command,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TerminalError("tmux_unavailable", "tmux is not installed on the remote profile") from exc
        except subprocess.TimeoutExpired as exc:
            raise TerminalError("tmux_timeout", "tmux control command timed out") from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:800].strip()
            raise TerminalError("tmux_command_failed", detail or "tmux command failed")
        return result

    def _target(self, slot: TerminalSlot) -> str:
        return f"{self.profile.hub_name}:{slot.window_name}"

    def _has_hub(self) -> bool:
        return self._tmux("has-session", "-t", self.profile.hub_name).returncode == 0

    def _has_window(self, slot: TerminalSlot) -> bool:
        windows = self._window_names()
        return slot.window_name in windows

    def _window_names(self) -> list[str]:
        result = self._tmux("list-windows", "-t", self.profile.hub_name, "-F", "#{window_name}")
        if result.returncode != 0:
            return []
        return result.stdout.decode("utf-8", errors="replace").splitlines()

    def _metadata_path(self, slot: TerminalSlot) -> Path:
        return self._slot_root(slot) / "session.json"

    def _manifest_path(self, slot: TerminalSlot) -> Path:
        return self._slot_root(slot) / "stream_manifest.json"

    def _load_metadata(self, slot: TerminalSlot) -> dict[str, Any]:
        return _read_json_retry(self._metadata_path(slot))

    def _write_metadata(self, slot: TerminalSlot, value: dict[str, Any]) -> None:
        _atomic_json(self._metadata_path(slot), value)

    def _acquire(self, slot: TerminalSlot) -> None:
        if slot.terminal_id in self._leases:
            return
        lease = _Lease(self._slot_root(slot) / "writer.lock")
        try:
            lease.acquire()
        except TerminalError as exc:
            if exc.code == "terminal_busy":
                # This record is advisory: even an exited PID cannot override
                # a contended kernel lock. Legacy/malformed records stay unknown.
                try:
                    metadata = self._load_metadata(slot)
                    raw = metadata.get("lease_owner")
                    record = raw if isinstance(raw, dict) else {}
                    limits = {"process_start_marker": 128, "host_boot_id": 64,
                              "connection_id": 32, "task_id": 128, "lock_type": 24}
                    owner = {key: value[:limit] for key, limit in limits.items()
                             if isinstance((value := record.get(key)), str)}
                    pid = record.get("pid")
                    if type(pid) is int and 0 < pid <= 0xFFFFFFFF:
                        owner["pid"] = pid
                    observation = _lease_process_observation(owner)
                except (OSError, ValueError, TypeError):
                    owner, observation = {}, {"state": "unknown", "reason": "owner_record_unreadable"}
                diagnostic = {"recorded_owner": owner, "liveness": observation, "ownership_verified": False}
                raise TerminalError(
                    exc.code, f"{exc}; diagnostics=" + json.dumps(diagnostic, separators=(",", ":")),
                    next_action="Owner metadata is advisory. Do not delete/steal the lock or kill a presumed owner. Reattach the same terminal after its MCP owner normally exits; terminal_read remains available.",
                ) from exc
            raise
        self._leases[slot.terminal_id] = lease

    def _release(self, slot: TerminalSlot) -> None:
        lease = self._leases.pop(slot.terminal_id, None)
        if lease is not None:
            lease.release()

    def _require_attached(self, slot: TerminalSlot) -> None:
        if slot.terminal_id not in self._leases:
            raise TerminalError(
                "terminal_not_attached",
                "call terminal_open once in this Codex task before sending input",
            )

    def _marker(self, slot: TerminalSlot, key: str) -> str:
        result = self._tmux("show-options", "-w", "-v", "-t", self._target(slot), key, check=True)
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _query(self, slot: TerminalSlot) -> dict[str, Any]:
        fmt = "#{session_id}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}"
        result = self._tmux("display-message", "-p", "-t", f"{self._target(slot)}.0", fmt, check=True)
        fields = result.stdout.decode("utf-8", errors="replace").rstrip("\r\n").split("\t", 5)
        if len(fields) != 6:
            raise TerminalError("tmux_state_invalid", "tmux returned an invalid fixed-terminal state")
        return {
            "session_id": fields[0],
            "pane_id": fields[1],
            "pane_pid": int(fields[2]) if fields[2].isdigit() else None,
            "pane_dead": fields[3] == "1",
            "current_command": fields[4],
            "cwd": fields[5],
        }

    def _ensure_logger(self, slot: TerminalSlot) -> None:
        state = self._query(slot)
        pane_id = state["pane_id"]
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "logger",
            "--slot-root",
            str(self._slot_root(slot)),
            "--segment-bytes",
            str(self.profile.segment_bytes),
            "--retain-segments",
            str(self.profile.retain_segments),
        ]
        pipe_command = "umask 077; exec " + shlex.join(command)
        # A pipe command can outlive the MCP transport in a stale state even
        # though tmux still reports the pane itself as healthy. Refresh only
        # the logger on every client attach; the session, pane, foreground
        # process, cwd, and transcript history are preserved.
        self._tmux("pipe-pane", "-t", pane_id, check=True)
        self._tmux("pipe-pane", "-t", pane_id, pipe_command, check=True)
        self._tmux(
            "set-option",
            "-w",
            "-t",
            self._target(slot),
            "@solution_agent_logger_protocol",
            _LOGGER_PROTOCOL,
            check=True,
        )

    def _create(self, slot: TerminalSlot, generation: int, owner_task_id: str | None) -> dict[str, Any]:
        if not slot.cwd.is_dir():
            raise TerminalError("cwd_not_found", f"fixed terminal cwd does not exist: {slot.cwd}")
        if not Path(slot.shell).is_file():
            raise TerminalError("shell_not_found", f"fixed terminal shell does not exist: {slot.shell}")
        self.base.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base, 0o700)
        slot_root = self._slot_root(slot)
        slot_root.mkdir(parents=True, exist_ok=True)
        os.chmod(slot_root, 0o700)
        environment = f"SOLUTION_AGENT_TERMINAL_NESTING=1; export SOLUTION_AGENT_TERMINAL_NESTING; exec {shlex.quote(slot.shell)}"
        if self._has_hub():
            if len(self._window_names()) >= self.profile.max_children:
                raise TerminalError(
                    "terminal_quota_exceeded",
                    f"terminal hub child quota reached: {self.profile.max_children}",
                )
            self._tmux(
                "new-window",
                "-d",
                "-t",
                self.profile.hub_name,
                "-n",
                slot.window_name,
                "-c",
                str(slot.cwd),
                shlex.join(["/bin/sh", "-lc", environment]),
                check=True,
            )
        else:
            self._tmux(
                "new-session",
                "-d",
                "-s",
                self.profile.hub_name,
                "-n",
                slot.window_name,
                "-c",
                str(slot.cwd),
                shlex.join(["/bin/sh", "-lc", environment]),
                check=True,
            )
        target = self._target(slot)
        self._tmux("set-option", "-w", "-t", target, "remain-on-exit", "on", check=True)
        self._tmux("set-option", "-w", "-t", target, "@solution_agent_terminal", slot.terminal_id, check=True)
        self._tmux("set-option", "-w", "-t", target, "@solution_agent_generation", str(generation), check=True)
        state = self._query(slot)
        metadata = {
            "schema_version": 1,
            "profile_id": self.profile.profile_id,
            "terminal_id": slot.terminal_id,
            "hub_name": self.profile.hub_name,
            "window_name": slot.window_name,
            "tmux_socket": str(self.socket_path),
            "configured_cwd": str(slot.cwd),
            "shell": slot.shell,
            "remote_uid": os.getuid() if hasattr(os, "getuid") else None,
            "hostname": socket.gethostname(),
            "generation": generation,
            "owner_task_id": owner_task_id,
            "owner_client_pid": os.getpid(),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "state": "running",
            "session_id": state["session_id"],
            "pane_id": state["pane_id"],
        }
        self._write_metadata(slot, metadata)
        self._ensure_logger(slot)
        return metadata

    def _verify_existing(self, slot: TerminalSlot, metadata: dict[str, Any]) -> None:
        configured_cwd = metadata.get("configured_cwd")
        if not isinstance(configured_cwd, str) or not configured_cwd or Path(configured_cwd).resolve() != slot.cwd.resolve():
            raise TerminalError(
                "terminal_identity_mismatch",
                "fixed terminal metadata mismatch: configured_cwd",
                recoverable=False,
                next_action="Restore the original workplace configuration or use a separately declared terminal/worktree; do not restart the existing terminal to rebind it.",
            )
        expected = {
            "terminal_id": slot.terminal_id,
            "hub_name": self.profile.hub_name,
            "window_name": slot.window_name,
            "tmux_socket": str(self.socket_path),
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise TerminalError("terminal_identity_mismatch", f"fixed terminal metadata mismatch: {key}", recoverable=False)
        if self._marker(slot, "@solution_agent_terminal") != slot.terminal_id:
            raise TerminalError("terminal_identity_mismatch", "tmux terminal marker mismatch", recoverable=False)
        state = self._query(slot)
        if state["session_id"] != metadata.get("session_id") or state["pane_id"] != metadata.get("pane_id"):
            raise TerminalError("terminal_identity_mismatch", "tmux session or pane identity changed", recoverable=False)

    def _stream_summary(self, slot: TerminalSlot) -> dict[str, Any]:
        manifest_path = self._manifest_path(slot)
        manifest = _read_json_retry(manifest_path)
        return {
            "cursor": int(manifest.get("total_bytes", 0)),
            "retained_from": int(manifest.get("retained_from", 0)),
            "segment_count": len(manifest.get("segments") or []),
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256_file(manifest_path) if manifest_path.is_file() else None,
        }

    def open(
        self,
        terminal_id: str | None = None,
        *,
        restart: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        slot = self._slot(terminal_id)
        self._acquire(slot)
        try:
            metadata = self._load_metadata(slot)
            exists = self._has_window(slot)
            if exists and not metadata:
                raise TerminalError("unknown_tmux_session", "a same-named tmux session exists without Harness metadata", recoverable=False)
            if not exists and metadata and not restart:
                raise TerminalError(
                    "terminal_needs_restart",
                    "the fixed terminal metadata exists but its tmux session is absent",
                    next_action="Inspect the prior receipt, then call terminal_open with restart=true only for approved maintenance.",
                )
            if not exists:
                generation = int(metadata.get("generation", 0)) + 1 if metadata else 1
                metadata = self._create(slot, generation, task_id)
                created = True
            else:
                self._verify_existing(slot, metadata)
                self._ensure_logger(slot)
                created = False
            state = self._query(slot)
            if state["pane_dead"] and restart:
                self._tmux("kill-window", "-t", self._target(slot), check=True)
                generation = int(metadata.get("generation", 0)) + 1 if metadata else 1
                metadata = self._create(slot, generation, task_id)
                created = True
                state = self._query(slot)
            if state["pane_dead"]:
                raise TerminalError(
                    "terminal_exited",
                    "the fixed terminal top-level shell has exited",
                    next_action="Use an approved maintenance restart; ordinary attach will not silently recreate it.",
                )
            metadata["updated_at"] = _utc_now()
            metadata["state"] = "running"
            metadata["profile_id"] = self.profile.profile_id
            if task_id is not None:
                metadata["owner_task_id"] = task_id
            metadata["owner_client_pid"] = os.getpid()
            metadata["lease_owner"] = {
                **_lease_process_identity(), "connection_id": self.connection_id,
                "task_id": metadata.get("owner_task_id"),
                "lock_type": "windows_byte_range" if os.name == "nt" else "posix_flock",
            }
            self._write_metadata(slot, metadata)
            return {
                "ok": True,
                "operation": "terminal_open",
                "created": created,
                "attached": True,
                "profile_id": self.profile.profile_id,
                "terminal_id": slot.terminal_id,
                "hub_id": self.profile.hub_name,
                "window_id": slot.window_name,
                "owner_task_id": metadata.get("owner_task_id"),
                "hostname": socket.gethostname(),
                "remote_user": getpass.getuser(),
                "generation": metadata["generation"],
                "state": state,
                "stream": self._stream_summary(slot),
                "next_action": "Use terminal_exec for ordinary commands; reserve terminal_send for REPL input and control keys.",
            }
        except Exception:
            self._release(slot)
            raise

    def _read_raw_bytes(self, slot: TerminalSlot, after_cursor: int, max_output_bytes: int) -> dict[str, Any]:
        _validate_read_window(after_cursor, max_output_bytes)
        manifest = _read_json_retry(self._manifest_path(slot))
        total = int(manifest.get("total_bytes", 0))
        retained_from = int(manifest.get("retained_from", 0))
        cursor = max(after_cursor, retained_from)
        truncated = after_cursor < retained_from
        output = bytearray()
        for item in manifest.get("segments") or []:
            if not isinstance(item, dict) or not item.get("retained", True):
                continue
            start = int(item.get("start", 0))
            end = int(item.get("end", start))
            if end <= cursor or start >= total:
                continue
            name = str(item.get("file") or "")
            if Path(name).name != name:
                raise TerminalError("transcript_manifest_invalid", "transcript segment path is invalid", recoverable=False)
            path = _inside(self._slot_root(slot) / name, self._slot_root(slot))
            if not path.is_file():
                raise TerminalError("transcript_segment_missing", f"retained transcript segment is missing: {name}")
            offset = max(cursor, start) - start
            remaining = max_output_bytes - len(output)
            if remaining <= 0:
                break
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(min(remaining, end - start - offset))
            output.extend(chunk)
            cursor += len(chunk)
            if len(output) >= max_output_bytes:
                break
        if cursor < total:
            truncated = True
        return {
            "raw": bytes(output),
            "raw_bytes": len(output),
            "after_cursor": after_cursor,
            "next_cursor": cursor,
            "retained_from": retained_from,
            "total_bytes": total,
            "truncated": truncated,
            "manifest_sha256": _sha256_file(self._manifest_path(slot)) if self._manifest_path(slot).is_file() else None,
        }

    def _read_raw(self, slot: TerminalSlot, after_cursor: int, max_output_bytes: int) -> dict[str, Any]:
        from solution_agent.harness.terminal_commands import sanitize_terminal_text

        result = self._read_raw_bytes(slot, after_cursor, max_output_bytes)
        raw = result.pop("raw")
        text: str | None = None
        decoded_bytes = len(raw)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            if exc.end == len(raw) and exc.reason == "unexpected end of data":
                for cut in range(len(raw) - 1, max(-1, len(raw) - 4), -1):
                    try:
                        text = raw[:cut].decode("utf-8", errors="strict")
                        decoded_bytes = cut
                        break
                    except UnicodeDecodeError:
                        continue
        binary = text is None or (decoded_bytes == 0 and bool(raw))
        if binary:
            sanitized = None
            removed = 0
        else:
            sanitized, removed = sanitize_terminal_text(text or "")
        result.update(
            output=sanitized,
            decoded_bytes=0 if binary else decoded_bytes,
            binary_artifact_required=binary,
            replacement_count=0,
            control_sequences_removed=removed,
            preview_is_sanitized=not binary,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
        return result

    def send(
        self,
        terminal_id: str | None,
        *,
        chars: str | None,
        control: str | None,
        after_cursor: int | None,
        wait_ms: int,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        """Send interactive input only; deterministic commands belong to exec()."""
        slot = self._slot(terminal_id)
        self._require_attached(slot)
        if chars is None and control is None:
            raise TerminalError("terminal_input_missing", "terminal_send needs chars or one allowed control")
        if chars is not None and len(chars.encode("utf-8")) > 65536:
            raise TerminalError("terminal_input_too_large", "terminal_send input exceeds 65536 bytes")
        if control is not None and control not in _ALLOWED_CONTROLS:
            raise TerminalError("terminal_control_invalid", f"unsupported terminal control: {control}")
        if not 0 <= wait_ms <= 30000:
            raise TerminalError("terminal_wait_invalid", "wait_ms must be between 0 and 30000")
        _validate_read_window(0 if after_cursor is None else after_cursor, max_output_bytes)
        before = self._stream_summary(slot)
        cursor = before["cursor"] if after_cursor is None else after_cursor
        state = self._query(slot)
        if state["pane_dead"]:
            raise TerminalError("terminal_exited", "the fixed terminal top-level shell has exited")
        pane_id = state["pane_id"]
        if chars:
            buffer_name = f"solution-agent-{uuid.uuid4().hex}"
            self._tmux("load-buffer", "-b", buffer_name, "-", input_bytes=chars.encode("utf-8"), check=True)
            self._tmux("paste-buffer", "-d", "-b", buffer_name, "-t", pane_id, check=True)
        if control is not None:
            self._tmux("send-keys", "-t", pane_id, _ALLOWED_CONTROLS[control], check=True)
        deadline = time.monotonic() + wait_ms / 1000
        last_total = before["cursor"]
        quiet_since: float | None = None
        wait_reason = "timeout" if wait_ms else "not_waited"
        while time.monotonic() < deadline:
            time.sleep(0.05)
            current = self._stream_summary(slot)["cursor"]
            if current != last_total:
                last_total = current
                quiet_since = time.monotonic()
            elif quiet_since is not None and time.monotonic() - quiet_since >= 0.25:
                wait_reason = "output_quiet"
                break
            state = self._query(slot)
            if state["pane_dead"]:
                wait_reason = "session_exit"
                break
        result = self._read_raw(slot, int(cursor), max_output_bytes)
        state = self._query(slot)
        result.update(
            ok=True,
            operation="terminal_send",
            profile_id=self.profile.profile_id,
            terminal_id=slot.terminal_id,
            wait_reason=wait_reason,
            session_status="exited" if state["pane_dead"] else "running",
            state=state,
            next_action="Use terminal_read for later output. Silence or timeout does not prove command completion.",
        )
        return result

    def exec(
        self,
        terminal_id: str | None,
        *,
        command: str | None,
        command_id: str | None,
        wait_ms: int,
        max_preview_bytes: int,
        task_id: str | None = None,
        work_unit_id: str | None = None,
        timeout_seconds: float | None = None,
        cancel: bool = False,
        access_mode: str = "read_only",
        writer_lease_id: str | None = None,
        runtime_release_id: str | None = None,
        worker_packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from solution_agent.harness.terminal_commands import TerminalCommandRunner

        slot = self._slot(terminal_id)
        runner = TerminalCommandRunner(
            self.base / "jobs",
            cwd=slot.cwd,
            shell=slot.shell,
            backend=self.profile.job_backend,
            max_concurrent=self.profile.job_max_concurrent,
            capsule_state_root=self.profile.solution_root / "state",
            runtime_release_id=runtime_release_id,
        )
        result = runner.execute(
            command=command,
            command_id=command_id,
            wait_ms=wait_ms,
            max_preview_bytes=max_preview_bytes,
            task_id=task_id,
            work_unit_id=work_unit_id,
            timeout_seconds=timeout_seconds,
            cancel=cancel,
            access_mode=access_mode,
            writer_lease_id=writer_lease_id,
            worker_packet=worker_packet,
        )
        result.update(
            ok=True,
            operation="terminal_exec",
            profile_id=self.profile.profile_id,
            execution_context=slot.terminal_id,
            terminal_attached_required=False,
        )
        return result

    def read(self, terminal_id: str | None, *, after_cursor: int, max_output_bytes: int) -> dict[str, Any]:
        slot = self._slot(terminal_id)
        if not self._has_window(slot):
            raise TerminalError("terminal_not_found", "the interactive terminal window is absent")
        metadata = self._load_metadata(slot)
        if not metadata:
            raise TerminalError("terminal_identity_missing", "the fixed terminal has no Harness metadata")
        self._verify_existing(slot, metadata)
        result = self._read_raw(slot, after_cursor, max_output_bytes)
        state = self._query(slot)
        result.update(
            ok=True,
            operation="terminal_read",
            profile_id=self.profile.profile_id,
            terminal_id=slot.terminal_id,
            wait_reason="session_exit" if state["pane_dead"] else "snapshot",
            session_status="exited" if state["pane_dead"] else "running",
            state=state,
            raw_transcript_ref={
                "manifest_path": str(self._manifest_path(slot)),
                "manifest_sha256": result.get("manifest_sha256"),
            },
            surface="raw_diagnostic",
            deprecated_for_normal_commands=True,
            migration_target="terminal_exec",
            full_output_fallback=False,
        )
        return result

    def close(self, terminal_id: str | None, *, action: str) -> dict[str, Any]:
        slot = self._slot(terminal_id)
        if action not in {"detach", "terminate"}:
            raise TerminalError("terminal_close_action_invalid", "action must be detach or terminate")
        if action == "detach":
            self._release(slot)
            return {
                "ok": True,
                "operation": "terminal_close",
                "profile_id": self.profile.profile_id,
                "terminal_id": slot.terminal_id,
                "action": "detach",
                "session_status": "running" if self._has_window(slot) else "absent",
                "next_action": "The fixed terminal persists; a later Codex task may attach it.",
            }
        self._require_attached(slot)
        existed = self._has_window(slot)
        if existed:
            self._tmux("kill-window", "-t", self._target(slot), check=True)
        time.sleep(0.1)
        manifest_path = self._manifest_path(slot)
        manifest = _read_json_retry(manifest_path)
        receipt = {
            "schema_version": 1,
            "profile_id": self.profile.profile_id,
            "terminal_id": slot.terminal_id,
            "action": "terminate",
            "terminated_at": _utc_now(),
            "session_existed": existed,
            "stream_total_bytes": int(manifest.get("total_bytes", 0)),
            "stream_retained_from": int(manifest.get("retained_from", 0)),
            "stream_manifest_sha256": _sha256_file(manifest_path) if manifest_path.is_file() else None,
        }
        _atomic_json(self._slot_root(slot) / "final_receipt.json", receipt)
        metadata = self._load_metadata(slot)
        if metadata:
            metadata["state"] = "terminated"
            metadata["updated_at"] = _utc_now()
            self._write_metadata(slot, metadata)
        self._release(slot)
        return {
            "ok": True,
            "operation": "terminal_close",
            "profile_id": self.profile.profile_id,
            "terminal_id": slot.terminal_id,
            "action": "terminate",
            "session_status": "terminated",
            "receipt_path": str(self._slot_root(slot) / "final_receipt.json"),
            "receipt": receipt,
        }

    def release_all(self) -> None:
        for terminal_id in list(self._leases):
            self._release(self.profile.slots[terminal_id])


def _logger(slot_root: Path, segment_bytes: int, retain_segments: int) -> int:
    slot_root.mkdir(parents=True, exist_ok=True)
    os.chmod(slot_root, 0o700)
    manifest_path = slot_root / "stream_manifest.json"
    manifest = _read_json_retry(manifest_path)
    segments = list(manifest.get("segments") or [])
    total_bytes = int(manifest.get("total_bytes", 0))
    next_index = max((int(item.get("index", 0)) for item in segments if isinstance(item, dict)), default=0) + 1
    current: dict[str, Any] | None = None
    handle = None
    digest = None

    def publish() -> None:
        retained = [item for item in segments if item.get("retained", True)]
        value = {
            "schema_version": 1,
            "total_bytes": total_bytes,
            "retained_from": int(retained[0]["start"]) if retained else total_bytes,
            "segment_bytes": segment_bytes,
            "retain_segments": retain_segments,
            "segments": segments,
            "updated_at": _utc_now(),
        }
        _atomic_json(manifest_path, value)

    def open_segment() -> tuple[Any, Any, dict[str, Any]]:
        nonlocal next_index
        name = f"transcript-{next_index:06d}.bin"
        next_index += 1
        path = slot_root / name
        stream = path.open("xb")
        os.chmod(path, 0o600)
        item = {
            "index": next_index - 1,
            "file": name,
            "start": total_bytes,
            "end": total_bytes,
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "closed": False,
            "retained": True,
        }
        segments.append(item)
        return stream, hashlib.sha256(), item

    def rotate() -> None:
        nonlocal handle, digest, current
        if handle is not None and current is not None and digest is not None:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            current["closed"] = True
            current["sha256"] = digest.hexdigest()
        retained = [item for item in segments if item.get("retained", True)]
        while len(retained) >= retain_segments:
            oldest = retained.pop(0)
            old_path = slot_root / str(oldest["file"])
            if old_path.is_file():
                old_path.unlink()
            oldest["retained"] = False
        handle, digest, current = open_segment()
        publish()

    try:
        rotate()
        source = sys.stdin.buffer
        read_available = getattr(source, "read1", source.read)
        while True:
            # BufferedReader.read(size) may wait for the entire requested size
            # on a pipe. read1 publishes ordinary short terminal output as soon
            # as one underlying read returns, while retaining the same bound.
            block = read_available(65536)
            if not block:
                break
            view = memoryview(block)
            while view:
                assert handle is not None and digest is not None and current is not None
                available = segment_bytes - int(current["bytes"])
                if available <= 0:
                    rotate()
                    continue
                part = bytes(view[:available])
                view = view[len(part) :]
                handle.write(part)
                handle.flush()
                digest.update(part)
                total_bytes += len(part)
                current["bytes"] = int(current["bytes"]) + len(part)
                current["end"] = total_bytes
                current["sha256"] = digest.hexdigest()
                publish()
    finally:
        if handle is not None and not handle.closed and current is not None and digest is not None:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            current["closed"] = True
            current["sha256"] = digest.hexdigest()
            publish()
    return 0


def terminal_runtime_check(solution_root: Path) -> dict[str, Any]:
    try:
        profile = load_terminal_profile(solution_root)
    except TerminalError as exc:
        return {"configured": False, "available": False, "code": exc.code, "message": str(exc)}
    remote_runtime = os.name != "nt"
    tmux_available = False
    if remote_runtime:
        try:
            result = subprocess.run(["tmux", "-V"], capture_output=True, timeout=5, check=False)
            tmux_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            tmux_available = False
    return {
        "configured": True,
        "available": tmux_available,
        "runtime": "remote" if remote_runtime else "local-control",
        "profile_id": profile.profile_id,
        "default_terminal": profile.default_terminal,
        "terminal_ids": sorted(profile.slots),
        "tmux_available": tmux_available,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    logger = subparsers.add_parser("logger")
    logger.add_argument("--slot-root", required=True)
    logger.add_argument("--segment-bytes", type=int, required=True)
    logger.add_argument("--retain-segments", type=int, required=True)
    args = parser.parse_args()
    if args.command == "logger":
        return _logger(Path(args.slot_root), args.segment_bytes, args.retain_segments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution_agent.harness.task_capsule import atomic_write_json
from solution_agent.utils.process_utils import process_is_alive


_COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|P.*?\x1b\\|[@-_])",
    re.DOTALL,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "lost"}


class CommandReceiptError(RuntimeError):
    def __init__(self, code: str, command_id: str):
        self.code = code
        super().__init__(f"{code}: {command_id}; no command was submitted")


@contextmanager
def _exclusive_job_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        pass
    else:
        os.write(descriptor, b"0")
        os.close(descriptor)
    with path.open("r+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.01)
    assert last_error is not None
    raise last_error


def _process_marker(pid: int) -> str | None:
    """Return a Linux process start marker so a recycled PID is not trusted."""

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        text = stat_path.read_text(encoding="utf-8")
        closing = text.rfind(")")
        fields = text[closing + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _process_exists(pid: int, marker: str | None = None) -> bool:
    if not process_is_alive(pid):
        return False
    if marker is not None:
        observed = _process_marker(pid)
        if observed is not None and observed != marker:
            return False
    return True


def _boot_id() -> str | None:
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip() or None
    except OSError:
        return None


def _process_snapshot(pid: int, marker: str | None = None, boot_id: str | None = None) -> dict[str, Any]:
    """Distinguish absence from failed observation; never signal a process."""
    if not isinstance(pid, int) or pid <= 0:
        return {'state': 'unknown', 'reason': 'process_identity_missing'}
    if sys.platform.startswith('linux'):
        current_boot = _boot_id()
        if boot_id is not None:
            if current_boot is None:
                return {'state': 'unknown', 'reason': 'boot_identity_unreadable'}
            if boot_id != current_boot:
                return {'state': 'exited', 'reason': 'host_rebooted'}
        try:
            text = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
            fields = text[text.rfind(')') + 2:].split()
            state, observed = fields[0], fields[19]
        except FileNotFoundError:
            return {'state': 'exited', 'reason': 'process_absent'}
        except (OSError, IndexError, UnicodeError):
            return {'state': 'unknown', 'reason': 'process_evidence_unreadable'}
        if marker is not None and observed != marker:
            return {'state': 'exited', 'reason': 'pid_reused'}
        if state in {'Z', 'X', 'x'}:
            return {'state': 'exited', 'reason': 'process_terminated'}
        if marker is None:
            return {'state': 'unknown', 'reason': 'process_start_identity_missing'}
        return {'state': 'alive', 'reason': 'identity_verified'}
    # Existing portable non-destructive probe remains available on Windows.
    return {'state': 'alive' if process_is_alive(pid) else 'exited', 'reason': 'portable_process_probe'}


def _systemd_liveness(unit: str) -> dict[str, Any]:
    snapshot = _systemd_snapshot(unit)
    if snapshot is None:
        return {'state': 'unknown', 'reason': 'systemd_query_unavailable'}
    active = snapshot.get('ActiveState')
    if active in {'active', 'activating', 'deactivating', 'reloading'}:
        return {'state': 'alive', 'reason': 'systemd_unit_active', 'unit': snapshot}
    if active in {'inactive', 'failed'}:
        # A unit state alone is not a descendant-process inventory. Preserve
        # uncertainty if systemd still reports a main process.
        if snapshot.get('MainPID', 0) != 0:
            return {'state': 'unknown', 'reason': 'systemd_state_pid_conflict', 'unit': snapshot}
        return {'state': 'exited', 'reason': 'systemd_unit_inactive', 'unit': snapshot}
    return {'state': 'unknown', 'reason': 'systemd_state_unrecognized', 'unit': snapshot}


def _process_group_snapshot(pgid: int, start_marker: str | None, boot_id: str | None) -> dict[str, Any]:
    """Track surviving job descendants after their leader has exited (Linux)."""
    if not sys.platform.startswith('linux'):
        return {'state': 'unknown', 'reason': 'process_group_probe_unavailable'}
    boot = _boot_id()
    if boot_id is None or boot is None or start_marker is None or not str(start_marker).isdigit():
        return {'state': 'unknown', 'reason': 'process_group_identity_missing'}
    if boot != boot_id:
        return {'state': 'exited', 'reason': 'host_rebooted'}
    members, uncertain = [], False
    try:
        entries = list(Path('/proc').iterdir())
    except OSError:
        return {'state': 'unknown', 'reason': 'process_inventory_unreadable'}
    if len(entries) > 20000:
        return {'state': 'unknown', 'reason': 'process_inventory_bound_exceeded'}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            text = (entry / 'stat').read_text(encoding='utf-8')
            fields = text[text.rfind(')') + 2:].split()
            if int(fields[2]) != pgid or int(fields[3]) != pgid or fields[0] in {'Z','X','x'}:
                continue
            if int(fields[19]) < int(start_marker):
                uncertain = True
                continue
            members.append(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, IndexError, ValueError, UnicodeError):
            uncertain = True
    return {'state': 'alive' if members else 'unknown' if uncertain else 'exited',
            'reason': 'job_group_members_present' if members else 'inventory_incomplete' if uncertain else 'job_group_empty',
            'members': sorted(members)}


def _systemd_unit_exists(unit: str) -> bool:
    executable = shutil.which("systemctl")
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "--user", "show", unit, "--property=LoadState", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() not in {"", "not-found"}


def _systemd_snapshot(unit: str) -> dict[str, Any] | None:
    executable = shutil.which("systemctl")
    if executable is None:
        return None
    properties = ("MainPID", "ExecMainStatus", "Result", "ActiveState", "SubState")
    try:
        result = subprocess.run(
            [
                executable,
                "--user",
                "show",
                unit,
                *(f"--property={name}" for name in properties),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    values: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            values[key] = int(value) if key in {"MainPID", "ExecMainStatus"} and value.isdigit() else value
    return values or None


@dataclass(frozen=True)
class Preview:
    text: str | None
    tail_text: str | None
    raw_bytes: int
    total_bytes: int
    truncated: bool
    binary_artifact_required: bool
    control_sequences_removed: int


def sanitize_terminal_text(text: str) -> tuple[str, int]:
    sanitized, ansi_count = _ANSI_RE.subn("", text)
    sanitized, control_count = _CONTROL_RE.subn("", sanitized)
    return sanitized, ansi_count + control_count


def bounded_utf8_preview(path: Path, limit: int) -> Preview:
    if not 1 <= limit <= 65536:
        raise ValueError("preview limit must be 1..65536")
    total = path.stat().st_size if path.is_file() else 0
    with path.open("rb") as handle:
        raw = handle.read(min(total, limit + 4))
    upper = min(len(raw), limit)
    for cut in range(upper, max(0, upper - 4), -1):
        try:
            text = raw[:cut].decode("utf-8", errors="strict")
            tail_text: str | None = None
            if total > cut:
                with path.open("rb") as handle:
                    handle.seek(max(0, total - limit - 4))
                    tail_raw = handle.read()
                for start in range(max(0, len(tail_raw) - limit), min(len(tail_raw), len(tail_raw) - limit + 4) + 1):
                    try:
                        tail_text = tail_raw[start:].decode("utf-8", errors="strict")
                        break
                    except UnicodeDecodeError:
                        continue
            sanitized, removed = sanitize_terminal_text(text)
            sanitized_tail, tail_removed = sanitize_terminal_text(tail_text or "")
            return Preview(
                text=sanitized,
                tail_text=sanitized_tail if tail_text is not None else None,
                raw_bytes=cut,
                total_bytes=total,
                truncated=total > cut,
                binary_artifact_required=False,
                control_sequences_removed=removed + tail_removed,
            )
        except UnicodeDecodeError:
            continue
    return Preview(
        text=None,
        tail_text=None,
        raw_bytes=0,
        total_bytes=total,
        truncated=total > 0,
        binary_artifact_required=True,
        control_sequences_removed=0,
    )


class TerminalCommandRunner:
    """Run ordinary commands as isolated supervised jobs, never inside tmux."""

    def __init__(
        self,
        jobs_root: Path,
        *,
        cwd: Path,
        shell: str,
        backend: str = "detached_process_supervisor",
        max_concurrent: int = 4,
        capsule_state_root: Path | None = None,
        runtime_release_id: str | None = None,
    ):
        self.jobs_root = jobs_root.resolve()
        self.cwd = cwd.resolve()
        self.shell = shell
        if backend not in {"systemd_transient", "detached_process_supervisor"}:
            raise ValueError(f"unsupported job backend: {backend}")
        if not 1 <= max_concurrent <= 64:
            raise ValueError("max_concurrent must be 1..64")
        self.backend = backend
        self.max_concurrent = max_concurrent
        self.capsule_state_root = capsule_state_root.resolve() if capsule_state_root is not None else None
        self.runtime_release_id = runtime_release_id

    def _active_job_count(self) -> int:
        if not self.jobs_root.is_dir():
            return 0
        active = 0
        for path in self.jobs_root.glob("*/status.json"):
            try:
                if _read_json(path).get("state") in {"prepared", "running"}:
                    active += 1
            except (OSError, json.JSONDecodeError):
                continue
        return active

    def _directory(self, command_id: str) -> Path:
        if not _COMMAND_ID.fullmatch(command_id):
            raise ValueError("invalid command_id")
        directory = (self.jobs_root / command_id).resolve()
        directory.relative_to(self.jobs_root)
        return directory

    def prepare(
        self,
        command: str,
        *,
        task_id: str | None = None,
        work_unit_id: str | None = None,
        timeout_seconds: float | None = None,
        access_mode: str = "read_only",
        writer_lease_id: str | None = None,
        worker_packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not command.strip():
            raise ValueError("command cannot be empty")
        encoded = command.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("terminal_exec command exceeds 1 MiB")
        if not self.cwd.is_dir():
            raise ValueError(f"job cwd does not exist: {self.cwd}")
        if not Path(self.shell).is_absolute():
            raise ValueError("job shell must be an absolute path")
        if timeout_seconds is not None and not 0 < timeout_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("timeout_seconds must be positive and at most 604800")
        if access_mode not in {"read_only", "writer"}:
            raise ValueError("access_mode must be read_only or writer")
        if access_mode == "writer" and not writer_lease_id:
            raise ValueError("writer jobs require writer_lease_id")
        if access_mode == "read_only" and writer_lease_id is not None:
            raise ValueError("read_only jobs cannot carry writer_lease_id")
        if worker_packet is not None:
            if access_mode != 'writer' or worker_packet.get('task_id') != task_id or worker_packet.get('work_unit_id') != work_unit_id:
                raise ValueError('worker preparation scope mismatch')
        with _exclusive_job_lock(self.jobs_root / ".launch.lock"):
            if worker_packet is not None:
                from solution_agent.harness.codex_worker import prepared_submission
                original = prepared_submission(worker_packet, self.jobs_root)
                if original is not None:
                    raise CommandReceiptError('worker_already_prepared', original)
            if self._active_job_count() >= self.max_concurrent:
                raise RuntimeError(f"job concurrency limit reached: {self.max_concurrent}")
            command_id = uuid.uuid4().hex
            directory = self._directory(command_id)
            directory.mkdir(parents=True, mode=0o700)
            request_script = directory / "request.sh"
            request_script.write_bytes(encoded + (b"\n" if not encoded.endswith(b"\n") else b""))
            os.chmod(request_script, 0o600)
            request = {
                "schema_version": 2,
                "command_id": command_id,
                "execution_guardian": True,
                "command_sha256": hashlib.sha256(encoded).hexdigest(),
                "command_bytes": len(encoded),
                "cwd": str(self.cwd),
                "shell": self.shell,
                "task_id": task_id,
                "work_unit_id": work_unit_id,
                "timeout_seconds": timeout_seconds,
                "access_mode": access_mode,
                "writer_lease_id": writer_lease_id,
                "capsule_state_root": str(self.capsule_state_root) if self.capsule_state_root is not None else None,
                "backend": self.backend,
                "systemd_unit": f"solution-agent-job-{command_id}" if self.backend == "systemd_transient" else None,
                "created_at": _utc_now(),
            }
            if self.runtime_release_id is not None:
                request["runtime_release_id"] = self.runtime_release_id
            if worker_packet is not None:
                from solution_agent.harness.codex_worker import _digest
                request['worker_request_sha256'] = _digest(worker_packet)
                # Persist the result/control link before the job can execute.
                atomic_write_json(directory / 'worker.json', {
                    key: worker_packet[key] for key in ('task_id', 'work_unit_id', 'control', 'output')})
            atomic_write_json(directory / "request.json", request)
            atomic_write_json(
                directory / "status.json",
                {
                    **request,
                    "state": "prepared",
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "failure_reason": None,
                },
            )
        return request

    def launch(self, request: dict[str, Any]) -> None:
        directory = self._directory(str(request["command_id"]))
        runner_path = Path(__file__).resolve()
        source_root = runner_path.parents[2]
        environment = dict(os.environ)
        inherited_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(source_root) + (os.pathsep + inherited_pythonpath if inherited_pythonpath else "")
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        process: subprocess.Popen[bytes] | None = None
        launcher: dict[str, Any]
        try:
            if self.backend == "systemd_transient":
                executable = shutil.which("systemd-run")
                if executable is None:
                    raise RuntimeError("systemd-run is unavailable")
                unit = str(request["systemd_unit"])
                command = [
                    executable,
                    "--user",
                    "--quiet",
                    "--collect",
                    f"--unit={unit}",
                    "--property=Type=exec",
                    "--property=KillMode=control-group",
                    "--property=TimeoutStopSec=5",
                    f"--setenv=PYTHONPATH={environment['PYTHONPATH']}",
                    "--setenv=PYTHONUTF8=1",
                    "--setenv=PYTHONIOENCODING=utf-8",
                    sys.executable,
                    str(runner_path),
                    "run",
                    "--command-dir",
                    str(directory),
                ]
                started = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
                if started.returncode != 0:
                    raise RuntimeError(f"systemd-run failed: {started.stderr.strip()[:500]}")
                launcher = {
                    "schema_version": 2,
                    "backend": self.backend,
                    "systemd_unit": unit,
                    "launched_at": _utc_now(),
                }
            else:
                process = subprocess.Popen(
                    [sys.executable, str(runner_path), "run", "--command-dir", str(directory)],
                    cwd=str(self.cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=environment,
                )
                launcher = {
                    "schema_version": 2,
                    "backend": self.backend,
                    "supervisor_pid": process.pid,
                    "supervisor_process_marker": _process_marker(process.pid),
                    "host_boot_id": _boot_id(),
                    "launched_at": _utc_now(),
                }
        except Exception as exc:
            finalize(directory, 127, _utc_now(), failure_reason=f"supervisor_launch_failed:{type(exc).__name__}")
            raise
        atomic_write_json(directory / "launcher.json", launcher)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            state = _read_json(directory / "status.json").get("state")
            if state != "prepared":
                return
            if process is not None and process.poll() is not None:
                break
            time.sleep(0.01)
        status = _read_json(directory / "status.json")
        if status.get("state") == "prepared" and process is not None and process.poll() is not None:
            self.reconcile(str(request['command_id']))

    def _read_status(self, command_id: str) -> dict[str, Any]:
        """Read evidence only. Missing/corrupt records are not execution outcomes."""
        directory = self._directory(command_id)
        if not directory.exists():
            raise CommandReceiptError("not_found", command_id)
        try:
            status = _read_json(directory / "status.json")
            request = _read_json(directory / "request.json")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CommandReceiptError("receipt_corrupt", command_id) from exc
        if (not isinstance(status, dict) or not isinstance(request, dict)
                or status.get("command_id") != command_id or request.get("command_id") != command_id
                or status.get("schema_version") != 2 or request.get("schema_version") != 2
                or status.get("state") not in _TERMINAL_STATES | {"prepared", "running"}
                or any(status.get(key) != request.get(key) for key in
                       ("task_id", "work_unit_id", "writer_lease_id", "execution_guardian", "command_sha256"))):
            raise CommandReceiptError("receipt_corrupt", command_id)
        return status

    def reconcile(self, command_id: str) -> dict[str, Any]:
        directory = self._directory(command_id)
        status = self._read_status(command_id)
        state = str(status.get("state") or "")
        if state in _TERMINAL_STATES:
            if state == "lost":
                self._report_lost_help(directory, status)
            return status
        if (directory / 'result_manifest.json').exists():
            try:
                receipt = _read_json(directory / 'result_manifest.json')
                if (receipt.get('schema_version') != 2
                        or any(receipt.get(key) != status.get(key) for key in ('command_id', 'command_sha256'))
                        or any(key in receipt and receipt[key] != status.get(key) for key in
                               ('task_id', 'work_unit_id', 'runtime_release_id'))
                        or receipt.get('state') not in {'succeeded', 'failed', 'cancelled'}
                        or type(receipt.get('exit_code')) is not int
                        or any(not isinstance(receipt.get(key), str) or not receipt[key]
                               for key in ('started_at', 'finished_at'))
                        or receipt.get('state') == 'succeeded' and
                           (receipt['exit_code'] != 0 or receipt.get('failure_reason') is not None)):
                    raise ValueError('terminal_manifest_conflict')
                terminal = {**status, **{key: receipt.get(key) for key in
                    ('state', 'exit_code', 'started_at', 'finished_at', 'failure_reason', 'signal', 'systemd')}}
                _release_writer_lease_from_job(directory, terminal)
                _write_completion_event(directory, terminal)
                atomic_write_json(directory / 'status.json', terminal)
                self._report_lost_help(directory, terminal)
                return terminal
            except (OSError, ValueError, TypeError, AttributeError):
                return {**status, 'process_tracking': {'state': 'unknown', 'observations': [],
                        'reason': 'terminal_manifest_unverified'}}
        pid = status.get("supervisor_pid")
        marker = status.get("supervisor_process_marker")
        launcher_path = directory / "launcher.json"
        launcher_unreadable = False
        try:
            launcher = _read_json(launcher_path) if launcher_path.is_file() else {}
            if not isinstance(launcher, dict):
                raise ValueError('invalid_launcher_object')
        except (OSError, ValueError):
            launcher = {}
            launcher_unreadable = True
        if not isinstance(pid, int):
            pid = launcher.get("supervisor_pid")
            marker = launcher.get("supervisor_process_marker")
        boot = status.get('host_boot_id', launcher.get('host_boot_id'))
        observations = []
        if launcher_unreadable:
            observations.append({'role': 'launcher', 'state': 'unknown', 'reason': 'launcher_evidence_unreadable'})
        if isinstance(pid, int):
            observations.append({'role': 'supervisor', **_process_snapshot(pid, marker if isinstance(marker, str) else None, boot)})
        if status.get('execution_guardian'):
            try:
                guardian_record = _read_json(directory / 'execution_guardian.json')
                if any(guardian_record.get(key) != status.get(key) for key in
                       ('command_id', 'command_sha256', 'task_id', 'work_unit_id')):
                    raise ValueError('guardian_scope_conflict')
                observations.append({'role': 'execution_guardian', **_process_snapshot(
                    guardian_record['pid'], guardian_record['process_marker'], guardian_record['host_boot_id'])})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                observations.append({'role': 'execution_guardian', 'state': 'unknown',
                                     'reason': 'guardian_identity_unavailable'})
        child = status.get('child_pid')
        if isinstance(child, int):
            observations.append({'role': 'child', **_process_snapshot(child, status.get('child_process_marker'), boot)})
        if isinstance(status.get('child_pgid'), int):
            observations.append({'role': 'job_group', **_process_group_snapshot(status['child_pgid'], status.get('child_process_marker'), boot)})
        unit = launcher.get("systemd_unit")
        if isinstance(unit, str):
            observations.append({'role': 'systemd', **_systemd_liveness(unit)})
        if any(item['state'] == 'alive' for item in observations):
            return {**status, 'process_tracking': {'state': 'alive', 'observations': observations,
                    'supervisor_missing': any(item['role'] == 'supervisor' and item['state'] == 'exited' for item in observations)}}
        if not observations or any(item['state'] == 'unknown' for item in observations):
            return {**status, 'process_tracking': {'state': 'unknown', 'observations': observations,
                    'reason': 'execution_not_proven_absent'}}
        lost = _mark_lost(directory, status, "supervisor_missing_without_terminal_receipt")
        self._report_lost_help(directory, lost)
        return lost

    @staticmethod
    def _report_lost_help(directory: Path, status: dict[str, Any]) -> None:
        if not (directory / "help-watch.json").is_file():
            return
        try:
            from solution_agent.harness.worker_help import finalize_watch
            finalize_watch(directory, _read_json(directory / "request.json"), status)
        except Exception as exc:
            # Side-channel recovery must not alter the authoritative lifecycle.
            print(json.dumps({"help_recovery_error": type(exc).__name__}), file=sys.stderr)

    def wait_result(self, command_id: str, wait_ms: int) -> dict[str, Any]:
        if not 0 <= wait_ms <= 30000:
            raise ValueError("wait_ms must be between 0 and 30000")
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            status = self.reconcile(command_id)
            if status.get("state") in _TERMINAL_STATES or time.monotonic() >= deadline:
                return status
            time.sleep(0.05)

    def cancel(self, command_id: str) -> dict[str, Any]:
        directory = self._directory(command_id)
        status = self._read_status(command_id)
        if status.get("state") in _TERMINAL_STATES:
            return status
        atomic_write_json(
            directory / "cancel_request.json",
            {
                "schema_version": 1,
                "command_id": command_id,
                "requested_at": _utc_now(),
            },
        )
        child_pid = status.get("child_pid")
        child_marker = status.get("child_process_marker")
        if not isinstance(child_pid, int) or not _process_exists(
            child_pid, child_marker if isinstance(child_marker, str) else None
        ):
            return self.wait_result(command_id, 5000)
        try:
            if os.name == "posix":
                os.killpg(child_pid, signal.SIGTERM)
            else:
                os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return self.wait_result(command_id, 5000)

    def read_result(self, command_id: str, max_preview_bytes: int) -> dict[str, Any]:
        directory = self._directory(command_id)
        status = self.reconcile(command_id)
        if status.get("state") in _TERMINAL_STATES and not (directory / "completion_event.json").is_file():
            _write_completion_event(directory, status)
            _release_writer_lease_from_job(directory, status)
        result: dict[str, Any] = {
            "command_id": command_id,
            "task_id": status.get("task_id"),
            "work_unit_id": status.get("work_unit_id"),
            "runtime_release_id": status.get("runtime_release_id"),
            "access_mode": status.get("access_mode"),
            "writer_lease_id": status.get("writer_lease_id"),
            "state": status.get("state"),
            "status": status.get("state"),
            "exit_code": status.get("exit_code"),
            "failure_reason": status.get("failure_reason"),
            "signal": status.get("signal"),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "supervisor_pid": status.get("supervisor_pid"),
            "child_pid": status.get("child_pid"),
            "host_boot_id": status.get("host_boot_id"),
            "process_tracking": status.get("process_tracking"),
            "backend": status.get("backend"),
            "systemd_unit": status.get("systemd_unit"),
            "command_dir": str(directory),
            "execution_surface": "isolated_job",
        }
        if (status.get('process_tracking', {}).get('supervisor_missing')
                and all(isinstance(status.get(key), str) and status[key].strip()
                        for key in ('task_id', 'work_unit_id'))):
            try:
                result['recovery_monitor'] = recover_command_monitor(self,
                    {key: status[key] for key in ('command_id', 'task_id', 'work_unit_id')})
            except (OSError, ValueError, RuntimeError) as error:
                result['recovery_monitor'] = {'monitor_state': 'unknown',
                    'reason': getattr(error, 'code', type(error).__name__), 'relaunch_permitted': False}
        for name in ("stdout", "stderr"):
            path = directory / f"{name}.bin"
            if not path.exists():
                path.write_bytes(b"")
            preview = bounded_utf8_preview(path, max_preview_bytes)
            result[name] = {
                "preview": preview.text,
                "tail_preview": preview.tail_text,
                "raw_bytes": preview.raw_bytes,
                "total_bytes": preview.total_bytes,
                "truncated": preview.truncated,
                "binary_artifact_required": preview.binary_artifact_required,
                "control_sequences_removed": preview.control_sequences_removed,
                "path": str(path),
                "sha256": _hash_file(path),
            }
        result["result_manifest_path"] = str(directory / "result_manifest.json")
        result["completion_event_path"] = str(directory / "completion_event.json")
        event_path = directory / "completion_event.json"
        if event_path.is_file():
            event = _read_json(event_path)
            result["event_id"] = event.get("event_id")
            result["event_material"] = bool(event.get("material"))
        else:
            result["event_id"] = None
            result["event_material"] = False
        lease_release_path = directory / "writer_lease_release.json"
        result["writer_lease_release"] = (
            _read_json(lease_release_path) if lease_release_path.is_file() else None
        )
        result["next_action"] = (
            "Use this deterministic command result."
            if result["state"] in _TERMINAL_STATES
            else "End this model turn; do not poll in a model loop. Resume after one material event or explicit manual resume."
        )
        return result

    def execute(
        self,
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
        worker_packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (command is None) == (command_id is None):
            raise ValueError("provide exactly one of command or command_id")
        if cancel and command is not None:
            raise ValueError("cancel requires command_id and cannot launch a new command")
        if command is not None:
            request = self.prepare(
                command,
                task_id=task_id,
                work_unit_id=work_unit_id,
                timeout_seconds=timeout_seconds,
                access_mode=access_mode,
                writer_lease_id=writer_lease_id,
                worker_packet=worker_packet,
            )
            command_id = str(request["command_id"])
            self.launch(request)
        assert command_id is not None
        if command is None:
            status = self._read_status(command_id)
            if (task_id is not None and status.get("task_id") != task_id
                    or work_unit_id is not None and status.get("work_unit_id") != work_unit_id):
                raise CommandReceiptError("job_scope_conflict", command_id)
        if cancel:
            self.cancel(command_id)
        waited = self.wait_result(command_id, wait_ms)
        result = self.read_result(command_id, max_preview_bytes)
        result["wait_timed_out"] = waited.get("state") not in _TERMINAL_STATES
        return result


def _mark_lost(directory: Path, status: dict[str, Any], reason: str) -> dict[str, Any]:
    if status.get("state") in _TERMINAL_STATES:
        return status
    lost = {
        **status,
        "state": "lost",
        "finished_at": _utc_now(),
        "exit_code": None,
        "failure_reason": reason,
    }
    _release_writer_lease_from_job(directory, lost)
    _write_completion_event(directory, lost)
    atomic_write_json(directory / "status.json", lost)
    return lost


def _write_completion_event(directory: Path, status: dict[str, Any]) -> None:
    state = str(status["state"])
    atomic_write_json(
        directory / "completion_event.json",
        {
            "schema_version": 1,
            "event_id": f"terminal-command:{status['command_id']}:{state}",
            "command_id": status["command_id"],
            "task_id": status.get("task_id"),
            "work_unit_id": status.get("work_unit_id"),
            "state": state,
            "material": True,
            "occurred_at": status["finished_at"],
        },
    )


def _release_writer_lease_from_job(directory: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    lease_id = request.get("writer_lease_id")
    if request.get("access_mode") != "writer" or not isinstance(lease_id, str) or not lease_id:
        return None
    receipt_path = directory / "writer_lease_release.json"
    if receipt_path.is_file():
        existing = _read_json(receipt_path)
        if existing.get("released") is True and all(
            existing.get(key) == request.get(key)
            for key in ("command_id", "task_id", "work_unit_id", "writer_lease_id")
        ):
            return existing
    state_root = request.get("capsule_state_root")
    task_id = request.get("task_id")
    work_unit_id = request.get("work_unit_id")
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "command_id": request.get("command_id"),
        "task_id": task_id,
        "work_unit_id": work_unit_id,
        "writer_lease_id": lease_id,
        "released": False,
        "released_at": None,
    }
    if not all(isinstance(value, str) and value for value in (state_root, task_id, work_unit_id)):
        receipt["failure_reason"] = "writer_lease_release_context_missing"
        atomic_write_json(receipt_path, receipt)
        return receipt
    try:
        from solution_agent.harness.task_capsule import CapsuleConflict, TaskCapsuleStore

        store = TaskCapsuleStore(Path(state_root))
        for _ in range(3):
            capsule = store.load(task_id)
            matching = [item for item in capsule.writer_leases if item.lease_id == lease_id]
            if not matching:
                receipt.update(released=True, released_at=_utc_now(), disposition="already_absent")
                atomic_write_json(receipt_path, receipt)
                return receipt
            if matching[0].owner_work_unit != work_unit_id:
                raise RuntimeError("writer lease owner does not match job work unit")
            try:
                store.release_writer_lease(task_id, lease_id, expected_version=capsule.version)
                receipt.update(released=True, released_at=_utc_now(), disposition="released")
                atomic_write_json(receipt_path, receipt)
                return receipt
            except CapsuleConflict:
                continue
        raise RuntimeError("writer lease changed during release")
    except Exception as exc:
        receipt["failure_reason"] = f"writer_lease_release_failed:{type(exc).__name__}"
        atomic_write_json(receipt_path, receipt)
        return receipt


def finalize(
    command_dir: Path,
    exit_code: int,
    started_at: str,
    *,
    failure_reason: str | None = None,
    forced_state: str | None = None,
) -> dict[str, Any]:
    directory = command_dir.resolve()
    request = _read_json(directory / "request.json")
    stdout = directory / "stdout.bin"
    stderr = directory / "stderr.bin"
    for path in (stdout, stderr):
        if not path.exists():
            path.write_bytes(b"")
    finished = _utc_now()
    state = forced_state or ("succeeded" if exit_code == 0 and failure_reason is None else "failed")
    if state not in _TERMINAL_STATES:
        raise ValueError(f"invalid terminal state: {state}")
    termination_signal = -exit_code if exit_code < 0 else None
    systemd_state = (
        _systemd_snapshot(str(request["systemd_unit"]))
        if request.get("backend") == "systemd_transient" and request.get("systemd_unit")
        else None
    )
    manifest = {
        "schema_version": 2,
        "command_id": request["command_id"],
        "command_sha256": request["command_sha256"],
        "task_id": request.get('task_id'),
        "work_unit_id": request.get('work_unit_id'),
        "started_at": started_at,
        "finished_at": finished,
        "state": state,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
        "signal": termination_signal,
        "systemd": systemd_state,
        "stdout": {"path": str(stdout), "bytes": stdout.stat().st_size, "sha256": _hash_file(stdout)},
        "stderr": {"path": str(stderr), "bytes": stderr.stat().st_size, "sha256": _hash_file(stderr)},
    }
    if "runtime_release_id" in request:
        manifest["runtime_release_id"] = request["runtime_release_id"]
    status_path = directory / "status.json"
    current = _read_json(status_path)
    terminal_status = {
        **current,
        "state": state,
        "started_at": started_at,
        "finished_at": finished,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
        "signal": termination_signal,
        "systemd": systemd_state,
    }
    # A reader can reconcile directly from the manifest, before status.json is
    # published. Finish the existing lease disposition before either terminal
    # surface becomes visible; completion must include its release receipt.
    _release_writer_lease_from_job(directory, request)
    atomic_write_json(directory / "result_manifest.json", manifest)
    _write_completion_event(directory, terminal_status)
    atomic_write_json(status_path, terminal_status)
    if (directory / "help-watch.json").is_file():
        from solution_agent.harness.worker_help import finalize_watch
        try:
            finalize_watch(directory, request, manifest)
        except Exception as exc:
            # Optional routing failures must not invalidate the original job receipt.
            print(json.dumps({"help_finalize_error": str(exc)[:1000]}), file=sys.stderr)
    return manifest


def run_job(command_dir: Path) -> None:
    """Launch one independent guardian; never retry an existing execution.

    The guardian owns wait(), timeout and terminal receipts. Losing this outer
    launcher therefore does not lose the child's eventual exit code.
    Legacy requests retain their original execution path.
    """
    directory = command_dir.resolve()
    request = _read_json(directory / 'request.json')
    if not request.get('execution_guardian'):
        run_execution(directory)
        return
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), 'guardian', '--command-dir', str(directory),
         '--supervisor-pid', str(os.getpid()), '--supervisor-marker', _process_marker(os.getpid()) or ''],
        cwd=request['cwd'], env=dict(os.environ), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
    )
    process.wait()
    # A crashed guardian is uncertain execution, never a successful result.
    # Reconciliation uses its recorded identity and the original child/group.


def run_execution(command_dir: Path, supervisor_pid: int | None = None,
                  supervisor_marker: str | None = None) -> None:
    directory = command_dir.resolve()
    request = _read_json(directory / "request.json")
    if supervisor_pid is not None:
        atomic_write_json(directory / 'execution_guardian.json', {
            **{key: request.get(key) for key in ('command_id', 'command_sha256', 'task_id', 'work_unit_id')},
            'schema_version': 1, 'pid': os.getpid(), 'process_marker': _process_marker(os.getpid()),
            'host_boot_id': _boot_id(), 'supervisor_pid': supervisor_pid, 'created_at': _utc_now(),
        })
    started = _utc_now()
    shell = str(request["shell"])
    shell_args = [shell]
    if Path(shell).name == "bash":
        shell_args.extend(["--noprofile", "--norc"])
    shell_args.append(str(directory / "request.sh"))
    try:
        with (directory / "stdout.bin").open("wb") as stdout, (directory / "stderr.bin").open("wb") as stderr:
            # A cancellation recorded before PID registration must not be lost.
            if (directory / "cancel_request.json").is_file():
                finalize(directory, 0, started, failure_reason="cancel_requested", forced_state="cancelled")
                return
            child = subprocess.Popen(
                shell_args,
                cwd=str(request["cwd"]),
                env=dict(os.environ, SOLUTION_AGENT_COMMAND_ID=request["command_id"],
                         SOLUTION_AGENT_COMMAND_DIR=str(directory)),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=True,
            )
            running = {
                **request,
                "state": "running",
                "started_at": started,
                "finished_at": None,
                "exit_code": None,
                "failure_reason": None,
                "supervisor_pid": supervisor_pid if supervisor_pid is not None else os.getpid(),
                "supervisor_process_marker": supervisor_marker if supervisor_pid is not None else _process_marker(os.getpid()),
                "execution_guardian_pid": os.getpid() if supervisor_pid is not None else None,
                "execution_guardian_process_marker": _process_marker(os.getpid()) if supervisor_pid is not None else None,
                "child_pid": child.pid,
                "child_process_marker": _process_marker(child.pid),
                "child_pgid": child.pid if sys.platform.startswith('linux') else None,
                "host_boot_id": _boot_id(),
            }
            atomic_write_json(directory / "status.json", running)
            timeout_seconds = request.get("timeout_seconds")
            try:
                deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds is not None else None
                cancel_deadline = None
                while True:
                    if cancel_deadline is None and (directory / "cancel_request.json").is_file():
                        try:
                            if os.name == "posix":
                                os.killpg(child.pid, signal.SIGTERM)
                            else:
                                child.terminate()
                        except ProcessLookupError:
                            pass
                        # Bound shutdown using the existing timeout escalation below.
                        cancel_deadline = time.monotonic() + 5
                        deadline = min(deadline, cancel_deadline) if deadline is not None else cancel_deadline
                    try:
                        exit_code = child.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(child.pid, signal.SIGTERM)
                    else:
                        child.terminate()
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if child.poll() is None:
                        raise
                try:
                    exit_code = child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == "posix":
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
                    except ProcessLookupError:
                        pass
                    exit_code = child.wait()
                finalize(
                    directory,
                    exit_code,
                    started,
                    failure_reason="cancel_requested" if (directory / "cancel_request.json").is_file() else "timeout_exceeded",
                    forced_state="cancelled",
                )
                return
        if (directory / "cancel_request.json").is_file():
            finalize(
                directory,
                exit_code,
                started,
                failure_reason="cancel_requested",
                forced_state="cancelled",
            )
        else:
            finalize(directory, exit_code, started)
    except BaseException as exc:
        import traceback
        atomic_write_json(directory / "supervisor_error.json", {
            "exception_type": type(exc).__name__, "message": str(exc),
            "traceback": traceback.format_exc(),
        })
        finalize(directory, 127, started, failure_reason=f"job_supervisor_error:{type(exc).__name__}")
        raise


def recover_command_monitor(runner: TerminalCommandRunner, binding: dict[str, str], *,
                            duration: float = 86400, interval: float = 5) -> dict[str, Any]:
    """Reattach an independent bounded observer, without starting the job again."""
    if set(binding) != {'command_id', 'task_id', 'work_unit_id'} or any(
            not isinstance(value, str) or not value.strip() for value in binding.values()):
        raise ValueError('invalid_command_monitor_binding')
    if not 0 < duration <= 86400 or not .1 <= interval <= 60:
        raise ValueError('invalid_command_monitor_bounds')
    status = runner._read_status(binding['command_id'])
    if any(status.get(key) != binding[key] for key in ('task_id', 'work_unit_id')):
        raise CommandReceiptError('job_scope_conflict', binding['command_id'])
    if status['state'] in _TERMINAL_STATES:
        return {**binding, 'monitor_state': 'complete', 'state': status['state'], 'relaunch_permitted': False}
    root = runner.jobs_root.resolve() / '.monitor'
    if root.resolve() != root:
        raise ValueError('monitor_directory_link_not_allowed')
    root.mkdir(exist_ok=True)
    record_path = root / (binding['command_id'] + '.observer.json')
    with _exclusive_job_lock(root / '.recovery.lock'):
        if record_path.exists():
            try:
                previous = _read_json(record_path)
                if any(previous.get(key) != value for key, value in binding.items()):
                    raise ValueError('observer_scope_conflict')
                observation = _process_snapshot(previous['pid'], previous['process_marker'], previous['host_boot_id'])
                if observation['state'] != 'exited':
                    return {**previous, 'monitor_state': observation['state'], 'relaunch_permitted': False}
            except (OSError, ValueError, TypeError, KeyError):
                return {**binding, 'monitor_state': 'unknown', 'reason': 'observer_identity_unavailable',
                        'relaunch_permitted': False}
        bindings_path = root / (binding['command_id'] + '.bindings.json')
        snapshot_path = root / (binding['command_id'] + '.snapshot.json')
        atomic_write_json(bindings_path, [binding])
        source = Path(__file__).resolve()
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(source.parents[2]) + os.pathsep + environment.get('PYTHONPATH', '')
        process = subprocess.Popen(
            [sys.executable, str(source), 'monitor', '--jobs-root', str(runner.jobs_root), '--cwd', str(runner.cwd),
             '--bindings', str(bindings_path), '--snapshot', str(snapshot_path),
             '--duration', str(duration), '--interval', str(interval)],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        record = {**binding, 'pid': process.pid, 'process_marker': _process_marker(process.pid),
                  'host_boot_id': _boot_id(), 'attached_at': _utc_now(), 'snapshot': str(snapshot_path),
                  'duration_seconds': duration, 'monitor_state': 'started', 'relaunch_permitted': False}
        atomic_write_json(record_path, record)
        return record


def monitor_commands(runner: TerminalCommandRunner, bindings: list[dict[str, str]],
                     snapshot_path: Path, *, interval: float = 5.0,
                     duration: float = 300.0, stopped=None) -> dict[str, Any]:
    """Bounded OS monitor for explicit commands; never submits or relaunches.

    Snapshot is separate from authoritative requests/results. An unreachable
    receipt is observable unknown, not proof of command absence or permission
    to retry. Scope is checked before reconciliation can write a lost receipt.
    """
    if not bindings or len(bindings) > 32 or not 0.1 <= interval <= 60 or not 0 < duration <= 86400:
        raise ValueError('invalid_command_monitor_bounds')
    seen = set()
    for binding in bindings:
        if set(binding) != {'command_id', 'task_id', 'work_unit_id'} or any(not isinstance(v, str) or not v.strip() for v in binding.values()):
            raise ValueError('invalid_command_monitor_binding')
        runner._directory(binding['command_id'])
        if binding['command_id'] in seen:
            raise ValueError('duplicate_command_monitor_binding')
        seen.add(binding['command_id'])
    # Keep observations inside this declared jobs storage, outside each command
    # directory. A user-provided path may not overwrite request/status evidence.
    snapshot_path = snapshot_path.resolve()
    expected_root = (runner.jobs_root / '.monitor').resolve()
    expected_root.relative_to(runner.jobs_root.resolve())
    if expected_root != runner.jobs_root.resolve() / '.monitor':
        raise ValueError('monitor_directory_link_not_allowed')
    snapshot_path.relative_to(expected_root)
    if snapshot_path == expected_root:
        raise ValueError('monitor_snapshot_file_required')
    deadline = time.monotonic() + duration
    iterations = 0
    while True:
        rows = []
        for binding in bindings:
            try:
                request = runner._read_status(binding['command_id'])
                if any(request.get(key) != binding[key] for key in ('task_id','work_unit_id')):
                    raise CommandReceiptError('job_scope_conflict', binding['command_id'])
                status = runner.reconcile(binding['command_id'])
                rows.append({**binding, 'state': status['state'], 'exit_code': status.get('exit_code'),
                             'process_tracking': status.get('process_tracking'),
                             'failure_reason': status.get('failure_reason')})
            except (OSError, ValueError, RuntimeError) as error:
                rows.append({**binding, 'state': 'observation_unknown',
                             'reason': getattr(error, 'code', type(error).__name__)})
        iterations += 1
        terminal = all(row['state'] in _TERMINAL_STATES for row in rows)
        cancelled = bool(stopped and stopped())
        expired = time.monotonic() >= deadline
        snapshot = {'schema': 'command-monitor/v1', 'pid': os.getpid(), 'observed_at': _utc_now(),
                    'iterations': iterations, 'commands': rows,
                    'monitor_state': 'complete' if terminal else 'stopped' if cancelled else 'deadline' if expired else 'observing',
                    'relaunch_permitted': False}
        atomic_write_json(snapshot_path, snapshot)
        if terminal or cancelled or expired:
            return snapshot
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--command-dir", type=Path, required=True)
    guardian_parser = subparsers.add_parser('guardian')
    guardian_parser.add_argument('--command-dir', type=Path, required=True)
    guardian_parser.add_argument('--supervisor-pid', type=int, required=True)
    guardian_parser.add_argument('--supervisor-marker', required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--command-dir", type=Path, required=True)
    finalize_parser.add_argument("--exit-code", type=int, required=True)
    finalize_parser.add_argument("--started-at", required=True)
    monitor_parser = subparsers.add_parser('monitor', help='bounded tracking only; no launch or retry')
    monitor_parser.add_argument('--jobs-root', type=Path, required=True)
    monitor_parser.add_argument('--cwd', type=Path, required=True)
    monitor_parser.add_argument('--bindings', type=Path, required=True)
    monitor_parser.add_argument('--snapshot', type=Path, required=True)
    monitor_parser.add_argument('--interval', type=float, default=5.0)
    monitor_parser.add_argument('--duration', type=float, default=300.0)
    recovery_parser = subparsers.add_parser('recover', help='reattach observation to the same command; no job retry')
    recovery_parser.add_argument('--jobs-root', type=Path, required=True)
    recovery_parser.add_argument('--cwd', type=Path, required=True)
    recovery_parser.add_argument('--command-id', required=True)
    recovery_parser.add_argument('--task-id', required=True)
    recovery_parser.add_argument('--work-unit-id', required=True)
    recovery_parser.add_argument('--duration', type=float, default=86400)
    recovery_parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args()
    if args.operation == "run":
        run_job(args.command_dir)
    elif args.operation == 'guardian':
        run_execution(args.command_dir, args.supervisor_pid, args.supervisor_marker or None)
    elif args.operation == 'recover':
        runner = TerminalCommandRunner(args.jobs_root, cwd=args.cwd, shell=sys.executable)
        print(json.dumps(recover_command_monitor(runner,
            {'command_id': args.command_id, 'task_id': args.task_id, 'work_unit_id': args.work_unit_id},
            duration=args.duration, interval=args.interval)))
    elif args.operation == "finalize":
        finalize(args.command_dir, args.exit_code, args.started_at)
    elif args.operation == 'monitor':
        bindings = json.loads(args.bindings.read_text(encoding='utf-8'))
        stop = [False]
        def stop_monitor(*unused):
            stop[0] = True
        signal.signal(signal.SIGTERM, stop_monitor)
        signal.signal(signal.SIGINT, stop_monitor)
        runner = TerminalCommandRunner(args.jobs_root, cwd=args.cwd, shell=sys.executable)
        snapshot = monitor_commands(runner, bindings, args.snapshot, interval=args.interval,
                                    duration=args.duration, stopped=lambda: stop[0])
        print(json.dumps({'monitor_state': snapshot['monitor_state'], 'iterations': snapshot['iterations'],
                          'snapshot': str(args.snapshot), 'relaunch_permitted': False}))


if __name__ == "__main__":
    main()

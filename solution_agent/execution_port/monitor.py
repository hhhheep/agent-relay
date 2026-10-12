"""monitor.py — check and update job state (heartbeat, exit, timeout, kill)."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .io_utils import atomic_write_json, read_json
from ..utils.subprocess_utils import hidden_subprocess_kwargs
from ..utils.process_utils import process_is_alive


# ── Process utilities ─────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    return process_is_alive(pid)


def _kill_tree(pid: int) -> None:
    """Kill process and all children.  SIGTERM → 3s wait → SIGKILL (Linux).
    Falls back to taskkill on Windows."""
    if hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(3)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, OSError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                **hidden_subprocess_kwargs(),
            )
        except OSError:
            pass


def _read_exit_code(job_dir: Path) -> Optional[int]:
    p = job_dir / "exit_code.txt"
    if p.exists():
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return None


# ── Core check ────────────────────────────────────────────────────────────────

def check_once(job_dir: Path) -> dict:
    """
    Single non-blocking check.  Updates state.json and heartbeat.json.

    Transitions handled:
      running → exited        (exit_code.txt found)
      running → timeout       (elapsed > timeout_sec → kill tree)
      running → killed        (monitor sent kill signal)
      running → lost_process  (PID gone, no exit_code.txt)

    Returns the current state dict.
    """
    state_path = job_dir / "state.json"
    state = read_json(state_path)
    now = datetime.now(timezone.utc)

    # Update heartbeat regardless of state
    atomic_write_json(job_dir / "heartbeat.json", {
        "last_seen": now.isoformat(),
        "pid": state.get("pid"),
        "status": state.get("status"),
    })

    status = state.get("status")
    if status != "running":
        return dict(state)

    pid = state.get("pid")
    exit_code = _read_exit_code(job_dir)

    # ── 1. wrapper wrote exit_code.txt → process finished cleanly ─────────────
    if exit_code is not None:
        state.update(
            status="exited",
            exit_code=exit_code,
            finished_at=now.isoformat(),
        )
        atomic_write_json(state_path, state)
        return dict(state)

    # ── 2. timeout check ──────────────────────────────────────────────────────
    timeout_sec = state.get("timeout_sec")
    started_at_str = state.get("started_at")
    if timeout_sec and started_at_str:
        try:
            started = datetime.fromisoformat(started_at_str)
            elapsed = (now - started).total_seconds()
            if elapsed > timeout_sec:
                if pid:
                    _kill_tree(pid)
                state.update(
                    status="timeout",
                    finished_at=now.isoformat(),
                    exit_code=-1,
                )
                atomic_write_json(state_path, state)
                return dict(state)
        except (ValueError, TypeError):
            pass

    # ── 3. PID gone without exit_code.txt → lost_process ─────────────────────
    if pid and not _pid_alive(pid):
        state.update(
            status="lost_process",
            finished_at=now.isoformat(),
            exit_code=-1,
        )
        atomic_write_json(state_path, state)
        return dict(state)

    return dict(state)


# ── Loop mode ─────────────────────────────────────────────────────────────────

_TERMINAL = {"exited", "timeout", "killed", "lost_process", "failed_launch",
             "succeeded", "failed_exit_code", "artifact_missing", "metrics_missing"}


def monitor_loop(job_dir: Path, interval: float = 5.0) -> dict:
    """Poll check_once() until the job reaches a terminal state."""
    while True:
        state = check_once(job_dir)
        if state.get("status") in _TERMINAL:
            return state
        time.sleep(interval)

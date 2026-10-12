"""Execute Agent: non-blocking process launch with local PID monitor."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..utils.subprocess_utils import hidden_subprocess_kwargs
from ..utils.process_utils import process_is_alive as _pid_alive

# ── Launch ────────────────────────────────────────────────────────────────────

_WRAPPER_SCRIPT = """\
import subprocess, sys, pathlib
out_dir = pathlib.Path(sys.argv[1])
command = sys.argv[2]
cwd    = sys.argv[3]

stdout_fh = (out_dir / "stdout.log").open("w", encoding="utf-8")
stderr_fh = (out_dir / "stderr.log").open("w", encoding="utf-8")

result = subprocess.run(command, shell=True, stdout=stdout_fh, stderr=stderr_fh, cwd=cwd)

stdout_fh.close()
stderr_fh.close()

(out_dir / "exit_code.txt").write_text(str(result.returncode), encoding="utf-8")
"""


def launch_background(
    command: str,
    out_dir: Path,
    worktree: Path,
) -> dict:
    """
    Launch *command* non-blocking via a wrapper subprocess.

    Writes to out_dir:
      runner.pid    — wrapper process ID (text)
      state.json    — {"status": "running", "started_at": <iso>, ...}
      stdout.log    — captured stdout (written when process completes)
      stderr.log    — captured stderr (written when process completes)
      exit_code.txt — exit code (written by wrapper on completion)

    Returns the launched result dict (status=launched).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    wrapper_path = out_dir / "_launch_wrapper.py"
    wrapper_path.write_text(_WRAPPER_SCRIPT, encoding="utf-8")

    started_at = datetime.now(timezone.utc).isoformat()

    proc = subprocess.Popen(
        [sys.executable, str(wrapper_path), str(out_dir), command, str(worktree)],
        # stdout/stderr of the wrapper itself go to devnull; the command's
        # output is redirected inside the wrapper script
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_subprocess_kwargs(new_process_group=True),
    )

    (out_dir / "runner.pid").write_text(str(proc.pid), encoding="utf-8")

    state = {
        "status": "running",
        "pid": proc.pid,
        "command": command,
        "started_at": started_at,
        "finished_at": None,
        "exit_code": None,
    }
    (out_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    result = {
        "status": "launched",
        "pid": proc.pid,
        "command": command,
        "started_at": started_at,
        "out_dir": str(out_dir),
    }
    _write_result_yaml(result, out_dir)

    return result


# ── Status check (non-blocking) ───────────────────────────────────────────────

def check_background_status(out_dir: Path) -> dict:
    """
    Non-blocking status check.  Reads state.json; if exit_code.txt exists,
    transitions state to succeeded/failed.

    Returns state dict.  status values: running | succeeded | failed | unknown.
    """
    state_path = out_dir / "state.json"
    if not state_path.exists():
        return {"status": "unknown", "reason": "state.json not found"}

    state: dict = json.loads(state_path.read_text(encoding="utf-8"))

    if state.get("status") in {"succeeded", "failed"}:
        return dict(state)

    # Check if the wrapper wrote exit_code.txt
    exit_code = _read_exit_code_file(out_dir)
    if exit_code is None:
        # Also check if the PID is still alive
        pid = state.get("pid")
        if pid is not None and not _pid_alive(pid):
            # Wrapper died without writing exit_code.txt — treat as failed
            exit_code = -1

    if exit_code is None:
        return dict(state)  # still running

    finished_at = datetime.now(timezone.utc).isoformat()
    status = "succeeded" if exit_code == 0 else "failed"
    state.update(
        status=status,
        finished_at=finished_at,
        exit_code=exit_code,
    )
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _write_result_yaml(
        {
            "status": status,
            "pid": state.get("pid"),
            "command": state.get("command", ""),
            "started_at": state.get("started_at"),
            "finished_at": finished_at,
            "exit_code": exit_code,
            "out_dir": str(out_dir),
        },
        out_dir,
    )
    return dict(state)


# ── Blocking wait (optional, for CI / test) ───────────────────────────────────

def wait_for_background(
    out_dir: Path,
    timeout_secs: float = 3600,
    poll_interval: float = 5.0,
) -> dict:
    """
    Poll check_background_status() until the process is no longer running.

    Returns the final state dict.  Raises TimeoutError if timeout_secs elapses.
    """
    deadline = time.monotonic() + timeout_secs
    while True:
        state = check_background_status(out_dir)
        if state.get("status") not in {"running", "launched"}:
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"background process still running after {timeout_secs}s "
                f"(pid={state.get('pid')})"
            )
        time.sleep(poll_interval)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_exit_code_file(out_dir: Path) -> Optional[int]:
    p = out_dir / "exit_code.txt"
    if p.exists():
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return None


def _write_result_yaml(result: dict, out_dir: Path) -> None:
    (out_dir / "result.yaml").write_text(
        yaml.safe_dump(result, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

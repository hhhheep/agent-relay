"""safe_runner — launch execution_wrapper for a registered job."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .io_utils import atomic_write_json, read_json
from ..utils.subprocess_utils import hidden_subprocess_kwargs

_WRAPPER_MODULE = "solution_agent.execution_port.execution_wrapper"


def run_execution(job_dir: Path) -> dict:
    """
    Launch execution_wrapper for the job in job_dir.

    Requires state.json.status == 'registered'.
    Returns the updated state dict (status: running or failed_launch).
    """
    state_path = job_dir / "state.json"
    state = read_json(state_path)

    if state.get("status") != "registered":
        raise RuntimeError(
            f"job {job_dir.name!r} is not in 'registered' state "
            f"(current: {state.get('status')!r})"
        )

    state["status"] = "launching"
    atomic_write_json(state_path, state)

    started_at = datetime.now(timezone.utc).isoformat()
    solution_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath_parts = [item for item in existing_pythonpath.split(os.pathsep) if item]
    if str(solution_root) not in pythonpath_parts:
        environment["PYTHONPATH"] = os.pathsep.join([str(solution_root), *pythonpath_parts])
    environment["SOLUTION_AGENT_ROOT"] = str(solution_root)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", _WRAPPER_MODULE, str(job_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=solution_root,
            env=environment,
            # start_new_session isolates the process group for clean kill
            start_new_session=True,
            **hidden_subprocess_kwargs(new_process_group=True),
        )
    except Exception as exc:
        state["status"] = "failed_launch"
        state["launch_error"] = str(exc)
        atomic_write_json(state_path, state)
        return dict(state)

    (job_dir / "runner.pid").write_text(str(proc.pid), encoding="utf-8")

    state.update(
        status="running",
        pid=proc.pid,
        started_at=started_at,
    )
    atomic_write_json(state_path, state)

    return dict(state)

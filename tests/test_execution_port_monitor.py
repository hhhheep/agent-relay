"""Sprint 4 tests: monitor.py — exited, timeout, heartbeat, lost_process."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from solution_agent.execution_port.io_utils import atomic_write_json, read_json
from solution_agent.execution_port.monitor import check_once
from solution_agent.execution_port.registry import register_execution
from solution_agent.execution_port.safe_runner import run_execution

_TEST_REGISTRY = Path(__file__).parent / "fixtures" / "script_registry_test.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def _make_spec(tmp_path: Path, script_id: str, args: dict, timeout: int = 8) -> Path:
    spec = {
        "task_id": "test_task",
        "job_type": "test",
        "workspace": {
            "root": str(_REPO_ROOT),
            "cwd": str(_REPO_ROOT),
        },
        "command": {
            "script_id": script_id,
            "args": args,
            "timeout_sec": timeout,
        },
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": ["tests/fixtures/write_metrics.py"]},
        "success_criteria": {"exit_code": 0},
    }
    p = tmp_path / "execution_spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


def _launch(tmp_path: Path, script_id: str, args: dict, timeout: int = 8):
    spec_path = _make_spec(tmp_path, script_id, args, timeout)
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    run_execution(job_dir)
    return job_dir


# ── heartbeat updated on every check_once ────────────────────────────────────

def test_heartbeat_written(tmp_path):
    out = tmp_path / "out"
    job_dir = _launch(tmp_path, "test_write_metrics", {"output_dir": str(out)})

    # Wait briefly to let wrapper start
    time.sleep(0.3)
    check_once(job_dir)

    hb = read_json(job_dir / "heartbeat.json")
    assert "last_seen" in hb
    assert hb["pid"] is not None


# ── exited: wrapper finishes, exit_code.txt written ──────────────────────────

def test_monitor_detects_exited(tmp_path):
    out = tmp_path / "out"
    job_dir = _launch(tmp_path, "test_write_metrics", {"output_dir": str(out)})

    # Wait for wrapper to write exit_code.txt (max 8s)
    deadline = time.monotonic() + 8
    while not (job_dir / "exit_code.txt").exists():
        if time.monotonic() > deadline:
            pytest.fail("exit_code.txt not written within 8s")
        time.sleep(0.2)

    state = check_once(job_dir)
    assert state["status"] == "exited"
    assert state["exit_code"] == 0
    assert state["finished_at"] is not None


# ── timeout: elapsed > timeout_sec → kill ────────────────────────────────────

def test_monitor_timeout_kills(tmp_path):
    # sleep 30s but timeout_sec=2 in state
    job_dir = _launch(tmp_path, "test_sleep", {"seconds": 30}, timeout=8)

    # Manually override started_at to force timeout
    state = read_json(job_dir / "state.json")
    state["started_at"] = "2000-01-01T00:00:00+00:00"  # way in the past
    state["timeout_sec"] = 1
    atomic_write_json(job_dir / "state.json", state)

    # Give process a moment to be alive
    time.sleep(0.3)

    result = check_once(job_dir)
    assert result["status"] == "timeout"
    assert result["exit_code"] == -1


# ── lost_process: PID gone but no exit_code.txt ───────────────────────────────

def test_monitor_lost_process(tmp_path):
    out = tmp_path / "out"
    spec_path = _make_spec(tmp_path, "test_write_metrics", {"output_dir": str(out)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    from datetime import datetime, timezone
    # Fake a running state with a PID that doesn't exist but a recent start
    # so timeout (8s) does not trigger
    fake_state = read_json(job_dir / "state.json")
    fake_state.update(
        status="running",
        pid=999999999,          # almost certainly dead
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    atomic_write_json(job_dir / "state.json", fake_state)

    # Ensure no exit_code.txt
    assert not (job_dir / "exit_code.txt").exists()

    result = check_once(job_dir)
    assert result["status"] == "lost_process"
    assert result["exit_code"] == -1


# ── no-op on already-terminal states ─────────────────────────────────────────

def test_monitor_skips_terminal_state(tmp_path):
    out = tmp_path / "out"
    spec_path = _make_spec(tmp_path, "test_write_metrics", {"output_dir": str(out)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    state = read_json(job_dir / "state.json")
    state["status"] = "succeeded"
    atomic_write_json(job_dir / "state.json", state)

    result = check_once(job_dir)
    assert result["status"] == "succeeded"  # unchanged

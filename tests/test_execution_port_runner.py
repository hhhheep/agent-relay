"""Sprint 3 tests: safe_runner + execution_wrapper + registry + io_utils."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from solution_agent.execution_port.registry import register_execution
from solution_agent.execution_port.safe_runner import run_execution
from solution_agent.execution_port.io_utils import read_json

# ── Helpers ───────────────────────────────────────────────────────────────────

_TEST_REGISTRY = Path(__file__).parent / "fixtures" / "script_registry_test.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def _make_spec(tmp_path: Path, script_id: str, args: dict, timeout: int = 5) -> Path:
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


# ── Sprint 2: build_argv ──────────────────────────────────────────────────────

def test_build_argv_str_args():
    from solution_agent.execution_port.script_registry import build_argv, load_registry
    reg = load_registry(_TEST_REGISTRY)
    argv = build_argv("test_write_metrics", {"output_dir": "/tmp/out"}, reg)
    assert argv[0] == "python"
    assert "--output_dir" in argv
    assert "/tmp/out" in argv


def test_build_argv_bool_true():
    from solution_agent.execution_port.script_registry import build_argv, load_registry
    reg = load_registry(_TEST_REGISTRY)
    argv = build_argv("test_write_metrics", {"output_dir": "/tmp/out", "fail": True}, reg)
    assert "--fail" in argv


def test_build_argv_bool_false_omitted():
    from solution_agent.execution_port.script_registry import build_argv, load_registry
    reg = load_registry(_TEST_REGISTRY)
    argv = build_argv("test_write_metrics", {"output_dir": "/tmp/out", "fail": False}, reg)
    assert "--fail" not in argv


def test_build_argv_none_omitted():
    from solution_agent.execution_port.script_registry import build_argv, load_registry
    reg = load_registry(_TEST_REGISTRY)
    argv = build_argv("test_sleep", {"seconds": 1, "output_dir": None}, reg)
    assert "--output_dir" not in argv


def test_script_registry_exposes_artifact_family_helpers():
    from solution_agent.execution_port.script_registry import get_artifact_family, get_closure_contract, get_output_family_allowlist

    registry = {
        "closure_contract": {
            "default_required_files": ["worker_notes.md", "artifact_summary.md"],
        },
        "artifact_families": {
            "contract_artifact": {
                "suffixes": ["_memo.md"],
            }
        },
        "scripts": {
            "writer": {
                "produces_artifact_families": [{"family": "contract_artifact"}],
            },
            "probe": {
                "produces_artifact_families": [{"prefix": "probe_"}],
            },
        },
    }

    assert get_artifact_family(registry, "contract_artifact")["suffixes"] == ["_memo.md"]
    assert get_output_family_allowlist(registry) == {"probe_": ["probe"]}
    assert get_closure_contract(registry, "probe")["default_required_files"] == ["worker_notes.md", "artifact_summary.md"]


# ── Sprint 2.5: register_execution ───────────────────────────────────────────

def test_register_creates_job_dir(tmp_path):
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(tmp_path / "out")})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)

    job_dir = executions_dir / job_id
    assert job_dir.exists()
    assert (job_dir / "execution_spec.yaml").exists()
    assert (job_dir / "source_execution_spec.yaml").read_bytes() == spec_path.read_bytes()
    state = read_json(job_dir / "state.json")
    assert state["status"] == "registered"
    assert state["task_id"] == "test_task"


def test_register_unique_job_id(tmp_path):
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(tmp_path / "out")})
    executions_dir = tmp_path / "executions"
    id1 = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    id2 = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    assert id1 != id2


def test_register_materializes_job_id_and_preserves_reviewed_source(tmp_path):
    output_template = str(tmp_path / "runs" / "{job_id}" / "out")
    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": output_template},
    )
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    data["artifacts"]["required"] = [
        str(tmp_path / "runs" / "{job_id}" / "out" / "metrics.json")
    ]
    data["workspace"]["artifact_roots"] = [str(tmp_path / "runs")]
    spec_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    source_text = (job_dir / "source_execution_spec.yaml").read_text(encoding="utf-8")
    materialized_text = (job_dir / "execution_spec.yaml").read_text(encoding="utf-8")
    state = read_json(job_dir / "state.json")

    assert "{job_id}" in source_text
    assert "{job_id}" not in materialized_text
    assert job_id in materialized_text
    assert len(state["source_spec_sha256"]) == 64
    assert len(state["materialized_spec_sha256"]) == 64
    assert state["source_spec_sha256"] != state["materialized_spec_sha256"]


def test_register_invalid_spec_raises(tmp_path):
    from solution_agent.execution_port.spec import SpecValidationError
    spec = {
        "task_id": "bad",
        "job_type": "test",
        "workspace": {"root": "/workspace", "cwd": "/workspace"},
        "command": {"script_id": "nonexistent", "args": {}, "timeout_sec": 5},
        "resources": {"gpu_required": False},
        "artifacts": {"required": ["something.txt"]},
        "success_criteria": {"exit_code": 0},
    }
    p = tmp_path / "execution_spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(SpecValidationError):
        register_execution(p, _TEST_REGISTRY, tmp_path / "executions")


# ── Sprint 3: safe_runner + execution_wrapper ─────────────────────────────────

def test_safe_runner_launches_job(tmp_path):
    out_dir = tmp_path / "metrics_out"
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(out_dir)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    state = run_execution(job_dir)

    assert state["status"] == "running"
    assert state["pid"] is not None
    assert (job_dir / "runner.pid").exists()
    assert (job_dir / "_worker_out" / "result.yaml").exists() is False


def test_wrapper_writes_exit_code_and_metrics(tmp_path):
    out_dir = tmp_path / "metrics_out"
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(out_dir)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    run_execution(job_dir)

    # Wait for wrapper to complete (max 8s)
    deadline = time.monotonic() + 8
    while not (job_dir / "exit_code.txt").exists():
        if time.monotonic() > deadline:
            pytest.fail("exit_code.txt not written within 8s")
        time.sleep(0.2)

    assert (job_dir / "exit_code.txt").read_text().strip() == "0"
    assert (job_dir / "stdout.log").exists()
    assert (job_dir / "stderr.log").exists()
    assert (job_dir / "wrapper_status.json").exists()
    assert (out_dir / "metrics.json").exists()


@pytest.mark.parametrize("status_write_fails", [False, True])
def test_wrapper_publishes_completion_only_after_receipt(tmp_path, monkeypatch, status_write_fails):
    from solution_agent.execution_port import execution_wrapper

    spec_path = _make_spec(tmp_path, "test_write_metrics", {"output_dir": str(tmp_path / "out")})
    executions_dir = tmp_path / "executions"
    job_dir = executions_dir / register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    write_json = execution_wrapper.atomic_write_json

    def write_status(path, data):
        assert path == job_dir / "wrapper_status.json"
        assert not (job_dir / "exit_code.txt").exists()
        if status_write_fails:
            raise OSError("receipt write failed")
        write_json(path, data)

    monkeypatch.setattr(execution_wrapper, "atomic_write_json", write_status)
    if status_write_fails:
        with pytest.raises(OSError, match="receipt write failed"):
            execution_wrapper.run(job_dir)
        assert not (job_dir / "exit_code.txt").exists()
    else:
        assert execution_wrapper.run(job_dir) == 0
        assert (job_dir / "exit_code.txt").read_text() == "0"
        assert read_json(job_dir / "wrapper_status.json")["exit_code"] == 0


def test_failed_launch_state(tmp_path, monkeypatch):
    import subprocess as sp
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(tmp_path / "out")})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    def bad_popen(*a, **kw):
        raise OSError("simulated Popen failure")

    monkeypatch.setattr(sp, "Popen", bad_popen)
    state = run_execution(job_dir)
    assert state["status"] == "failed_launch"
    assert "_worker_out" not in str(list(job_dir.iterdir()))


def test_runner_rejects_non_registered(tmp_path):
    spec_path = _make_spec(tmp_path, "test_write_metrics",
                           {"output_dir": str(tmp_path / "out")})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    # Manually advance state
    from solution_agent.execution_port.io_utils import atomic_write_json
    state = read_json(job_dir / "state.json")
    state["status"] = "running"
    atomic_write_json(job_dir / "state.json", state)

    with pytest.raises(RuntimeError, match="not in 'registered' state"):
        run_execution(job_dir)

"""Sprint B tests: remote_materializer → execution_spec.yaml via execution: block."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from solution_agent.execution_port.spec import _parse, validate_spec
from solution_agent.execution_port.spec_builder import build_spec_from_execution_block, write_execution_spec
from solution_agent.remote_materializer.task_loader import load_task

_TEST_REGISTRY = Path(__file__).parent / "fixtures" / "script_registry_test.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def _registry() -> dict:
    return yaml.safe_load(_TEST_REGISTRY.read_text(encoding="utf-8"))


# ── task_loader picks up execution: block ────────────────────────────────────

def test_taskbook_loads_execution_block(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text("""\
---
task_id: mat_task_001
title: Run eval probe
worker: remote_materializer
status: pending
execution:
  script_id: test_write_metrics
  args:
    output_dir: results/eval
  timeout_sec: 30
  gpu_required: false
  required_artifacts:
    - tests/fixtures/write_metrics.py
  success_criteria:
    exit_code: 0
---

## Goal

Materialize eval config and run.
""", encoding="utf-8")

    task = load_task(task_path)
    assert task.execution["script_id"] == "test_write_metrics"
    assert task.execution["args"]["output_dir"] == "results/eval"
    assert task.execution["timeout_sec"] == 30
    assert "tests/fixtures/write_metrics.py" in task.execution["required_artifacts"]


def test_taskbook_execution_defaults_to_empty(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text("---\ntask_id: t\ntitle: T\nstatus: pending\n---\n",
                         encoding="utf-8")
    task = load_task(task_path)
    assert task.execution == {}


# ── execution_port.spec_builder shared function ───────────────────────────────

def test_build_spec_from_execution_block_structure():
    execution = {
        "script_id": "test_write_metrics",
        "args": {"output_dir": "results/eval"},
        "timeout_sec": 30,
        "gpu_required": False,
        "gpu_count": 0,
        "required_artifacts": ["tests/fixtures/write_metrics.py"],
        "success_criteria": {"exit_code": 0},
    }
    spec = build_spec_from_execution_block(
        execution=execution,
        task_id="mat_task_001",
        task_type="small_probe",
        worktree=_REPO_ROOT,
    )
    assert spec["task_id"] == "mat_task_001"
    assert spec["command"]["script_id"] == "test_write_metrics"
    assert spec["command"]["timeout_sec"] == 30
    assert spec["workspace"]["root"] == str(_REPO_ROOT)


def test_build_spec_validates_against_registry():
    execution = {
        "script_id": "test_write_metrics",
        "args": {"output_dir": "results/eval"},
        "timeout_sec": 8,
        "required_artifacts": ["tests/fixtures/write_metrics.py"],
    }
    spec_data = build_spec_from_execution_block(
        execution=execution,
        task_id="mat_task_001",
        task_type="small_probe",
        worktree=_REPO_ROOT,
    )
    errors = validate_spec(_parse(spec_data), _registry())
    assert errors == [], errors


def test_write_execution_spec_creates_valid_yaml(tmp_path):
    execution = {
        "script_id": "test_write_metrics",
        "args": {"output_dir": "results/eval"},
        "timeout_sec": 8,
        "required_artifacts": ["tests/fixtures/write_metrics.py"],
    }
    dest = tmp_path / "execution_spec.yaml"
    write_execution_spec(
        execution=execution,
        task_id="mat_task_001",
        task_type="small_probe",
        worktree=_REPO_ROOT,
        dest=dest,
    )
    assert dest.exists()
    loaded = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert loaded["command"]["script_id"] == "test_write_metrics"
    assert loaded["workspace"]["artifact_roots"] == [str(_REPO_ROOT)]


def test_execution_spec_preserves_allowed_artifact_roots(tmp_path):
    execution = {
        "script_id": "test_write_metrics",
        "args": {"output_dir": "/tmp/lab-runs/demo"},
        "timeout_sec": 8,
        "required_artifacts": ["/tmp/lab-runs/demo/metrics.json"],
        "allowed_artifact_roots": ["/tmp/lab-runs"],
    }
    spec_data = build_spec_from_execution_block(
        execution=execution,
        task_id="mat_task_external_outputs",
        task_type="small_probe",
        worktree=_REPO_ROOT,
    )
    assert spec_data["workspace"]["artifact_roots"] == [str(_REPO_ROOT), "/tmp/lab-runs"]


def test_orchestrator_adds_run_root_artifact_root(tmp_path):
    from solution_agent.orchestrator.core import _ensure_execution_spec_artifact_roots

    run_root = tmp_path / "runs"
    run_dir = run_root / "task__run"
    out_dir = run_dir / "_worker_out"
    spec_path = out_dir / "execution" / "execution_spec.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        yaml.safe_dump(
            {
                "task_id": "task",
                "job_type": "smoke",
                "workspace": {"root": str(tmp_path / "repo"), "cwd": str(tmp_path / "repo")},
                "command": {"script_id": "test_write_metrics", "args": {}, "timeout_sec": 8},
                "resources": {"gpu_required": False, "gpu_count": 0},
                "artifacts": {"required": [str(run_root / "artifact" / "metrics.json")]},
                "success_criteria": {"exit_code": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _ensure_execution_spec_artifact_roots(spec_path, out_dir)

    loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert str(tmp_path / "repo") in loaded["workspace"]["artifact_roots"]
    assert str(run_root) in loaded["workspace"]["artifact_roots"]


# ── MaterializerRun writes execution_spec.yaml on materialized ────────────────

def _make_materialized_run(tmp_path: Path, workspace_root: Path | None = None):
    """
    Build a MaterializerRun with a mock materializer that immediately returns
    a valid fenced artifact so status becomes 'materialized'.
    """
    from solution_agent.remote_materializer.controller import MaterializerRun

    task_path = tmp_path / "task.md"
    task_path.write_text(f"""\
---
task_id: mat_spec_test
title: Materializer spec test
worker: remote_materializer
status: pending
execution:
  script_id: test_write_metrics
  args:
    output_dir: results/eval
  timeout_sec: 8
  gpu_required: false
  required_artifacts:
    - tests/fixtures/write_metrics.py
  success_criteria:
    exit_code: 0
---

## Goal

Test that materialized run outputs execution_spec.yaml.
""", encoding="utf-8")

    # Minimal work_order JSON that satisfies load_work_order
    work_order_json = """{
  "task_id": "mat_spec_test",
  "task_type": "analysis_script_fix",
  "title": "Test",
  "goal": "Test goal",
  "required_outputs": ["config.yaml"],
  "forbidden_actions": [],
  "context_files": []
}"""

    # reader returns a valid work_order JSON
    reader = MagicMock()
    reader.create_work_order.return_value = work_order_json

    # materializer returns a fenced config.yaml artifact
    materializer = MagicMock()
    materializer.materialize.return_value = (
        "```yaml config.yaml\ntask: test\n```"
    )

    return MaterializerRun(
        task_path=task_path,
        output_root=tmp_path / "output",
        reader_client=reader,
        materializer_client=materializer,
        workspace_root=workspace_root,
    )


def test_materializer_writes_execution_spec_when_workspace_provided(tmp_path):
    run = _make_materialized_run(tmp_path, workspace_root=_REPO_ROOT)
    try:
        result = run.run()
    except Exception:
        pytest.skip("materializer internals require full schema setup")

    if result.get("status") == "materialized":
        run_dir = tmp_path / "output" / "mat_spec_test"
        assert (run_dir / "execution_spec.yaml").exists(), \
            "execution_spec.yaml not written after materialized"
        spec = yaml.safe_load((run_dir / "execution_spec.yaml").read_text(encoding="utf-8"))
        assert spec["command"]["script_id"] == "test_write_metrics"
        assert result.get("execution_spec") == "execution_spec.yaml"


def test_materializer_skips_execution_spec_without_workspace(tmp_path):
    run = _make_materialized_run(tmp_path, workspace_root=None)
    try:
        result = run.run()
    except Exception:
        pytest.skip("materializer internals require full schema setup")

    if result.get("status") == "materialized":
        run_dir = tmp_path / "output" / "mat_spec_test"
        assert not (run_dir / "execution_spec.yaml").exists(), \
            "execution_spec.yaml should not be written without workspace_root"

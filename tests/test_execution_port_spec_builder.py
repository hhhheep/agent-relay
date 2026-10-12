"""Sprint 6 tests: spec_builder.py + runner integration with execution_port."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from solution_agent.execution_port.collector import collect
from solution_agent.execution_port.io_utils import read_json
from solution_agent.execution_port.monitor import check_once
from solution_agent.execution_port.registry import register_execution
from solution_agent.execution_port.safe_runner import run_execution
from solution_agent.execution_port.spec import validate_spec, _parse
from solution_agent.small_task_worker.runner import run_small_task_worker
from solution_agent.small_task_worker.spec_builder import build_execution_spec
from solution_agent.small_task_worker.task_contract import parse_task_md

_TEST_REGISTRY = Path(__file__).parent / "fixtures" / "script_registry_test.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def _write_task(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "task.md"
    p.write_text(content, encoding="utf-8")
    return p


_RUN_TASK_MD = """\
---
task_id: probe_run_001
title: Run write_metrics probe
task_type: small_probe
execution_stage: run
execution:
  script_id: test_write_metrics
  args:
    output_dir: "{output_dir}"
  timeout_sec: 8
  gpu_required: false
  gpu_count: 0
  required_artifacts:
    - tests/fixtures/write_metrics.py
  success_criteria:
    exit_code: 0
---

## Goal

Run the metrics probe and collect output.
"""


# ── 1. build_execution_spec produces correct structure ───────────────────────

def test_spec_builder_structure(tmp_path):
    out = tmp_path / "out"
    task_path = _write_task(tmp_path, _RUN_TASK_MD.format(output_dir=out.as_posix()))
    task = parse_task_md(task_path)

    spec = build_execution_spec(task, _REPO_ROOT)

    assert spec["task_id"] == "probe_run_001"
    assert spec["command"]["script_id"] == "test_write_metrics"
    assert spec["command"]["args"]["output_dir"] == out.as_posix()
    assert spec["command"]["timeout_sec"] == 8
    assert spec["resources"]["gpu_required"] is False
    assert "tests/fixtures/write_metrics.py" in spec["artifacts"]["required"]
    assert spec["success_criteria"]["exit_code"] == 0


# ── 2. built spec passes validate_spec with test registry ────────────────────

def test_spec_builder_validates(tmp_path):
    out = tmp_path / "out"
    task_path = _write_task(tmp_path, _RUN_TASK_MD.format(output_dir=out.as_posix()))
    task = parse_task_md(task_path)

    spec_data = build_execution_spec(task, _REPO_ROOT)
    registry = yaml.safe_load(_TEST_REGISTRY.read_text(encoding="utf-8"))

    spec = _parse(spec_data)
    errors = validate_spec(spec, registry)
    assert errors == [], f"Unexpected validation errors: {errors}"


# ── 3. runner writes execution_spec.yaml + status=spec_ready ─────────────────

def test_runner_writes_spec_ready(tmp_path):
    out = tmp_path / "out"
    task_path = _write_task(tmp_path, _RUN_TASK_MD.format(output_dir=out.as_posix()))
    out_dir = tmp_path / "_worker_out"

    result = run_small_task_worker(task_path, worktree=_REPO_ROOT, out_dir=out_dir)

    assert result["status"] == "spec_ready"
    assert (out_dir / "execution_spec.yaml").exists()
    assert (out_dir / "result.yaml").exists()


# ── 4. written spec is valid YAML and passes validate_spec ───────────────────

def test_runner_spec_yaml_valid(tmp_path):
    out = tmp_path / "out"
    task_path = _write_task(tmp_path, _RUN_TASK_MD.format(output_dir=out.as_posix()))
    out_dir = tmp_path / "_worker_out"
    run_small_task_worker(task_path, worktree=_REPO_ROOT, out_dir=out_dir)

    spec_data = yaml.safe_load((out_dir / "execution_spec.yaml").read_text(encoding="utf-8"))
    registry = yaml.safe_load(_TEST_REGISTRY.read_text(encoding="utf-8"))

    spec = _parse(spec_data)
    errors = validate_spec(spec, registry)
    assert errors == []


# ── 5. end-to-end: spec_ready → register → run → collect → succeeded ─────────

def test_spec_ready_to_collect_succeeded(tmp_path):
    out = tmp_path / "out"
    task_path = _write_task(tmp_path, _RUN_TASK_MD.format(output_dir=out.as_posix()))
    worker_out = tmp_path / "_worker_out"

    # Worker produces execution_spec.yaml
    result = run_small_task_worker(task_path, worktree=_REPO_ROOT, out_dir=worker_out)
    assert result["status"] == "spec_ready"

    spec_path = worker_out / "execution_spec.yaml"
    executions_dir = tmp_path / "executions"

    # Orchestrator hands spec to execution_port
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    run_execution(job_dir)

    # Wait for exit_code.txt
    deadline = time.monotonic() + 10
    while not (job_dir / "exit_code.txt").exists():
        if time.monotonic() > deadline:
            pytest.fail("exit_code.txt not written within 10s")
        time.sleep(0.2)

    check_once(job_dir)
    final = collect(job_dir)

    assert final["status"] == "succeeded"
    assert (job_dir / "final_result.yaml").exists()
    assert (job_dir / "_worker_out" / "result.yaml").exists()

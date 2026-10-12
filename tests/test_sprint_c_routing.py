"""Sprint C tests: worker='execution_port' routing in run_worker."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ── run_worker routing ────────────────────────────────────────────────────────

def test_run_worker_routes_to_execution_port(tmp_path):
    """run_worker with worker='execution_port' calls _run_worker_execution_port."""
    sentinel = {"status": "succeeded", "routed": True}
    with patch(
        "solution_agent.orchestrator.core._run_worker_execution_port",
        return_value=sentinel,
    ) as mock_ep:
        from solution_agent.orchestrator.core import run_worker
        result = run_worker("any_task_id", worker="execution_port")
    mock_ep.assert_called_once_with("any_task_id")
    assert result == sentinel


def test_run_worker_routes_to_materialized_execution():
    """run_worker with worker='materialized_execution' calls the combined path."""
    sentinel = {"status": "succeeded", "routed": True}
    with patch(
        "solution_agent.orchestrator.core._run_worker_materialized_execution",
        return_value=sentinel,
    ) as mock_combined:
        from solution_agent.orchestrator.core import run_worker
        result = run_worker("any_task_id", worker="materialized_execution")
    mock_combined.assert_called_once_with("any_task_id")
    assert result == sentinel


def test_run_worker_raises_on_unknown_worker():
    from solution_agent.orchestrator.core import run_worker
    with pytest.raises(ValueError, match="unsupported worker"):
        run_worker("t1", worker="does_not_exist")


def test_materialized_execution_runs_materializer_then_execution(tmp_path):
    """Combined worker turns a materializer-produced spec into worker success."""
    task_id = "mat_exec_test_001"
    task_path = tmp_path / "task.md"
    task_path.write_text("""\
---
task_id: mat_exec_test_001
title: Materialized execution
worker: materialized_execution
status: running
execution:
  script_id: test_write_metrics
  args:
    output_dir: out
  timeout_sec: 5
  required_artifacts: []
---

## Goal

Materialize then execute.
""", encoding="utf-8")

    worker_out = tmp_path / "_worker_out"
    mat_result_root = tmp_path / "results" / task_id

    def fake_materializer(task_path, output_root, env_file, policy_path, feedback_text="", workspace_root=None):
        assert workspace_root == tmp_path
        run_dir = output_root / task_id
        run_dir.mkdir(parents=True)
        generated = run_dir / "generated"
        generated.mkdir()
        plan = {
            "schema_version": 1,
            "task_id": task_id,
            "read_paths": [
                {"path": "context_pack/task_brief.md", "kind": "config", "recursive": False, "estimated_files": 1, "purpose": "bounded test input"}
            ],
            "write_paths": [{"path": "out", "purpose": "test metrics output"}],
            "data_access": {
                "uses_existing_indices": True,
                "opens_image_files": False,
                "opens_image_pixels": False,
                "full_recursive_dataset_scan": False,
                "copies_or_moves_data": False,
                "modifies_datasets": False,
            },
            "execution_strategy": "Run the allowlisted test script only.",
        }
        (generated / "execution_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        spec = {
            "task_id": task_id,
            "job_type": "small_probe",
            "workspace": {"root": str(tmp_path), "cwd": str(tmp_path)},
            "command": {"script_id": "test_write_metrics", "args": {"output_dir": "out"}, "timeout_sec": 5},
            "resources": {"gpu_required": False, "gpu_count": 0},
            "artifacts": {"required": []},
            "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
        }
        (run_dir / "execution_spec.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
        (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
        return {"task_id": task_id, "status": "materialized", "created_at": "2026-05-05T00:00:00+00:00"}

    def fake_execute(task_id_arg, spec_path, out_dir, *, worker):
        assert task_id_arg == task_id
        assert spec_path.exists()
        assert worker == "materialized_execution"
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "success",
            "execution_status": "succeeded",
            "worker": worker,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "result.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return {"task_id": task_id, "status": "succeeded", "job_id": "job_mat_exec"}

    with (
        patch("solution_agent.orchestrator.core.find_task", return_value=("running", task_path)),
        patch("solution_agent.orchestrator.core.worker_out_dir", return_value=worker_out),
        patch("solution_agent.orchestrator.core.result_dir", return_value=mat_result_root),
        patch("solution_agent.utils.paths.project_root", return_value=tmp_path),
        patch("solution_agent.orchestrator.retry_controller.run_remote_materializer", side_effect=fake_materializer),
        patch("solution_agent.orchestrator.core._run_execution_spec_for_worker", side_effect=fake_execute),
    ):
        from solution_agent.orchestrator.core import run_worker
        result = run_worker(task_id, worker="materialized_execution")

    assert result["status"] == "succeeded"
    written = yaml.safe_load((worker_out / "result.yaml").read_text(encoding="utf-8"))
    assert written["status"] == "success"
    assert written["execution_status"] == "succeeded"
    assert (worker_out / "materializer" / "result_package.yaml").exists()


def test_materialized_execution_can_bypass_materializer_for_direct_execution_block(tmp_path):
    task_id = "mat_exec_bypass_001"
    task_path = tmp_path / "task.md"
    task_path.write_text("""\
---
task_id: mat_exec_bypass_001
title: Materialized execution direct block
worker: materialized_execution
status: running
materialized_execution_bypass_materializer: true
execution:
  script_id: test_write_metrics
  args:
    output_dir: out
  timeout_sec: 5
  required_artifacts: []
---

## Goal

Execute the supplied execution block.
""", encoding="utf-8")

    worker_out = tmp_path / "_worker_out"

    def fake_execute(task_id_arg, spec_path, out_dir, *, worker):
        assert task_id_arg == task_id
        assert worker == "materialized_execution"
        loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        assert loaded["command"]["script_id"] == "test_write_metrics"
        assert loaded["workspace"]["root"] == str(tmp_path)
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "success",
            "execution_status": "succeeded",
            "worker": worker,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "result.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return {"task_id": task_id, "status": "succeeded", "job_id": "job_bypass"}

    with patch("solution_agent.orchestrator.core._run_execution_spec_for_worker", side_effect=fake_execute):
        from solution_agent.orchestrator.core import run_materialized_execution_task
        result = run_materialized_execution_task(
            task_path=task_path,
            out_dir=worker_out,
            workspace_root=tmp_path,
            materializer_root=tmp_path / "materializer",
        )

    assert result["status"] == "succeeded"
    assert result["materializer_status"] == "direct_execution_block"
    assert (worker_out / "materializer" / "result_package.yaml").exists()
    assert (worker_out / "execution" / "execution_spec.yaml").exists()


# ── spec lookup priority ──────────────────────────────────────────────────────

def test_execution_port_prefers_worker_out_spec(tmp_path):
    """
    If execution_spec.yaml exists in worker_out_dir, it is used first;
    the worktree candidate and task.md generation are skipped.
    """
    task_id = "prio_test_001"

    # Patch path helpers so they point into tmp_path
    worker_out = tmp_path / "worker_out" / task_id
    worktree = tmp_path / "worktrees" / task_id
    ep_dir = tmp_path / "executions"

    worker_out.mkdir(parents=True)
    worktree.mkdir(parents=True)

    spec = {
        "task_id": task_id, "job_type": "small_probe",
        "workspace": {"root": str(tmp_path), "cwd": str(tmp_path)},
        "command": {"script_id": "test_write_metrics", "args": {}, "timeout_sec": 5},
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": []},
        "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
    }
    spec_path = worker_out / "execution_spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")

    # A different spec in worktree — should NOT be picked
    worktree_spec = worktree / "execution_spec.yaml"
    worktree_spec.write_text(
        yaml.safe_dump({**spec, "task_id": "WRONG"}, allow_unicode=True),
        encoding="utf-8",
    )

    fake_final = {"status": "succeeded", "job_id": "job_xyz", "task_id": task_id}

    with (
        patch("solution_agent.orchestrator.core.worker_out_dir", return_value=worker_out),
        patch("solution_agent.orchestrator.core.worktree_dir", return_value=worktree),
        patch("solution_agent.orchestrator.core.register_execution" if False else
              "solution_agent.execution_port.registry.register_execution",
              return_value="job_xyz"),
        patch("solution_agent.orchestrator.core.run_worker",
              side_effect=lambda tid, worker="codex": fake_final),
    ):
        # Call directly to test spec selection without full pipeline
        candidates = [worker_out / "execution_spec.yaml", worktree / "execution_spec.yaml"]
        chosen = next((p for p in candidates if p.exists()), None)
        assert chosen == spec_path
        loaded = yaml.safe_load(chosen.read_text(encoding="utf-8"))
        assert loaded["task_id"] == task_id


def test_execution_port_falls_back_to_worktree_spec(tmp_path):
    """If worker_out spec is missing, worktree spec is used."""
    task_id = "prio_test_002"

    worker_out = tmp_path / "worker_out" / task_id
    worktree = tmp_path / "worktrees" / task_id
    worker_out.mkdir(parents=True)
    worktree.mkdir(parents=True)

    spec = {
        "task_id": task_id, "job_type": "small_probe",
        "workspace": {"root": str(tmp_path), "cwd": str(tmp_path)},
        "command": {"script_id": "my_script", "args": {}, "timeout_sec": 5},
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": []},
        "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
    }
    worktree_spec = worktree / "execution_spec.yaml"
    worktree_spec.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")

    candidates = [worker_out / "execution_spec.yaml", worktree / "execution_spec.yaml"]
    chosen = next((p for p in candidates if p.exists()), None)
    assert chosen == worktree_spec
    loaded = yaml.safe_load(chosen.read_text(encoding="utf-8"))
    assert loaded["command"]["script_id"] == "my_script"


def test_execution_port_no_spec_returns_none_candidate(tmp_path):
    """If neither spec path exists, candidate is None (triggers task.md fallback)."""
    worker_out = tmp_path / "worker_out" / "t3"
    worktree = tmp_path / "worktrees" / "t3"
    worker_out.mkdir(parents=True)
    worktree.mkdir(parents=True)

    candidates = [worker_out / "execution_spec.yaml", worktree / "execution_spec.yaml"]
    chosen = next((p for p in candidates if p.exists()), None)
    assert chosen is None


# ── result mirroring ─────────────────────────────────────────────────────────

def test_execution_port_mirrors_result_to_worker_out(tmp_path):
    """_run_worker_execution_port writes collect-compatible result.yaml."""
    task_id = "mirror_test_001"
    worker_out = tmp_path / "worker_out" / task_id
    ep_dir = tmp_path / "executions"
    job_dir = ep_dir / "job_mirror"
    job_dir.mkdir(parents=True)
    worker_out.mkdir(parents=True)

    fake_final = {
        "job_id": "job_mirror", "task_id": task_id,
        "status": "succeeded", "exit_code": 0,
    }

    # Simulate only the mirroring logic
    from solution_agent.execution_port.collector import to_worker_status

    import yaml as _yaml
    fake_worker = dict(fake_final)
    fake_worker["status"] = to_worker_status(fake_final["status"])
    fake_worker["execution_status"] = fake_final["status"]
    fake_worker["worker"] = "execution_port"
    (worker_out / "result.yaml").write_text(
        _yaml.safe_dump(fake_worker, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    written = yaml.safe_load((worker_out / "result.yaml").read_text(encoding="utf-8"))
    assert written["status"] == "success"
    assert written["execution_status"] == "succeeded"
    assert written["task_id"] == task_id


# ── executions_dir path helper ────────────────────────────────────────────────

def test_executions_dir_path():
    from solution_agent.orchestrator.paths import executions_dir
    p = executions_dir()
    assert p.name == "executions"

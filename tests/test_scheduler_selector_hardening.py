from __future__ import annotations

import shutil
import uuid
from unittest.mock import patch

import pytest

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import dispatch_dir, registry_path, task_dir, worktree_dir
from solution_agent.orchestrator.registry import load_registry, save_registry


STATUSES = ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed")


def _cleanup(task_id: str) -> None:
    for status in STATUSES:
        path = task_dir(status, task_id)
        if path.exists():
            shutil.rmtree(path)
    for path in (dispatch_dir(task_id), worktree_dir(task_id)):
        if path.exists():
            shutil.rmtree(path)


def _write_pending_task(
    task_id: str,
    *,
    worker: str,
    line: str = "selector_line",
    batch: str = "selector_batch",
    dag_root: str = "selector_root",
    resource_hint: str = "cpu",
    task_prefix: str = "",
    front_prefix: str = "",
) -> None:
    pending = task_dir("pending", task_id)
    pending.mkdir(parents=True, exist_ok=True)
    front_matter_parts = [
        f"task_id: {task_id}",
        f"title: {task_id}",
        "priority: normal",
        f"worker: {worker}",
        "status: pending",
        f"line: {line}",
        f"batch: {batch}",
        f"dag_root: {dag_root}",
        f"resource_hint: {resource_hint}",
    ]
    if task_prefix:
        front_matter_parts.append(f"task_prefix: {task_prefix}")
    if front_prefix:
        front_matter_parts.append(f"prefix: {front_prefix}")
    front_matter_parts.append("created_at: 2026-05-13T00:00:00+00:00")
    front_matter = "\n".join(front_matter_parts)
    (pending / "task.md").write_text(
        f"""---
{front_matter}
---

# Task

Selector hardening fixture.
""",
        encoding="utf-8",
    )


def _registry_for(*task_ids: str) -> dict:
    return {
        "tasks": {
            task_id: {
                "task_id": task_id,
                "title": task_id,
                "status": "pending",
                "worker": "materialized_execution" if "mat" in task_id else "small_task_worker",
            }
            for task_id in task_ids
        }
    }


def test_next_pending_task_id_filters_by_declared_worker():
    prefix = f"sel_{uuid.uuid4().hex[:8]}_"
    small_id = prefix + "small"
    mat_id = prefix + "mat"
    original = load_registry()
    try:
        _write_pending_task(small_id, worker="small_task_worker")
        _write_pending_task(mat_id, worker="materialized_execution")
        save_registry(_registry_for(small_id, mat_id), path=registry_path())

        selected = core.next_pending_task_id(worker="materialized_execution", prefix=prefix)

        assert selected == mat_id
    finally:
        save_registry(original, path=registry_path())
        _cleanup(small_id)
        _cleanup(mat_id)


def test_dispatch_worker_mismatch_fails_before_state_mutation():
    task_id = f"sel_mismatch_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_pending_task(task_id, worker="small_task_worker")
        save_registry(_registry_for(task_id), path=registry_path())

        with pytest.raises(ValueError, match="worker mismatch"):
            core.dispatch(task_id, worker="materialized_execution", submit=False)

        assert task_dir("pending", task_id).exists()
        assert not task_dir("running", task_id).exists()
        assert load_registry()["tasks"][task_id]["status"] == "pending"
    finally:
        save_registry(original, path=registry_path())
        _cleanup(task_id)


def test_force_worker_override_requires_reason_and_records_event():
    task_id = f"sel_force_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_pending_task(task_id, worker="small_task_worker")
        save_registry(_registry_for(task_id), path=registry_path())

        with pytest.raises(ValueError, match="requires --reason"):
            core.dispatch(
                task_id,
                worker="materialized_execution",
                submit=False,
                force_worker_override=True,
            )

        result = core.dispatch(
            task_id,
            worker="materialized_execution",
            submit=False,
            force_worker_override=True,
            override_reason="manual compatibility test",
        )

        assert result["record"]["worker"] == "materialized_execution"
        record = load_registry()["tasks"][task_id]
        assert record["worker_overrides"][-1]["declared_worker"] == "small_task_worker"
        assert record["worker_overrides"][-1]["requested_worker"] == "materialized_execution"
    finally:
        save_registry(original, path=registry_path())
        _cleanup(task_id)


def test_schedule_remote_filters_worker_and_scope_before_planning():
    prefix = f"sel_plan_{uuid.uuid4().hex[:8]}_"
    outside_id = prefix + "outside_mat"
    small_id = prefix + "small"
    mat_id = prefix + "mat"
    original = load_registry()
    try:
        _write_pending_task(outside_id, worker="materialized_execution", line="other_line")
        _write_pending_task(small_id, worker="small_task_worker", line="A_residual_correction")
        _write_pending_task(mat_id, worker="materialized_execution", line="A_residual_correction")
        save_registry(_registry_for(outside_id, small_id, mat_id), path=registry_path())

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_cpu=2,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert [item["task_id"] for item in plan["plan"]] == [mat_id]
        skipped = {item["task_id"]: item["reason"] for item in plan["skipped"] if str(item.get("task_id", "")).startswith(prefix)}
        assert skipped[outside_id] == "line_scope_mismatch"
        assert skipped[small_id] == "worker_mismatch"
    finally:
        save_registry(original, path=registry_path())
        _cleanup(outside_id)
        _cleanup(small_id)
        _cleanup(mat_id)


def test_schedule_remote_rejects_unsupported_resource_hint():
    prefix = f"sel_hint_{uuid.uuid4().hex[:8]}_"
    unknown_id = prefix + "unknown_hint"
    original = load_registry()
    try:
        _write_pending_task(unknown_id, worker="materialized_execution", resource_hint="quantum")
        save_registry(_registry_for(unknown_id), path=registry_path())

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_cpu=0,
                max_running_gpu=1,
                dry_run=True,
                line="selector_line",
                prefix=prefix,
            )

        assert not plan["plan"]
        skipped = [item for item in plan["skipped"] if str(item.get("task_id", "")).startswith(prefix)]
        assert skipped == [
            {
                "task_id": unknown_id,
                "reason": "unsupported_resource_hint",
                "resource_hint": "quantum",
            }
        ]
    finally:
        save_registry(original, path=registry_path())
        _cleanup(unknown_id)


def test_prefix_selector_matches_front_matter_prefix_field():
    expected_prefix = f"front_{uuid.uuid4().hex[:8]}_"
    no_prefix_task = f"other_{uuid.uuid4().hex[:8]}"
    matching_task = f"task_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_pending_task(no_prefix_task, worker="materialized_execution", task_prefix="")
        _write_pending_task(matching_task, worker="materialized_execution", front_prefix=expected_prefix)
        save_registry(_registry_for(no_prefix_task, matching_task), path=registry_path())

        selected = core.next_pending_task_id(worker="materialized_execution", prefix=expected_prefix)

        assert selected == matching_task
    finally:
        save_registry(original, path=registry_path())
        _cleanup(no_prefix_task)
        _cleanup(matching_task)


def test_batch_scope_matches_task_batch_alias():
    prefix = f"sel_alias_{uuid.uuid4().hex[:8]}_"
    match = prefix + "match"
    other = prefix + "other"
    target_batch = f"batch_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_pending_task(other, worker="materialized_execution", batch="other_batch")
        task_path = task_dir("pending", match)
        task_path.mkdir(parents=True, exist_ok=True)
        (task_path / "task.md").write_text(
            f"""---
task_id: {match}
title: {match}
priority: normal
worker: materialized_execution
status: pending
task_batch: {target_batch}
line: selector_line
dag_root: selector_root
resource_hint: cpu
created_at: 2026-05-13T00:00:00+00:00
---

# Task

Selector hardening fixture.
""",
            encoding="utf-8",
        )
        save_registry(_registry_for(other, match), path=registry_path())

        selected = core.next_pending_task_id(worker="materialized_execution", batch=target_batch)

        assert selected == match
    finally:
        save_registry(original, path=registry_path())
        _cleanup(other)
        _cleanup(match)


def test_dag_root_scope_matches_task_root_alias():
    prefix = f"sel_dagroot_{uuid.uuid4().hex[:8]}_"
    match = prefix + "match"
    other = prefix + "other"
    target_root = f"root_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_pending_task(other, worker="materialized_execution", dag_root="other_root")
        task_path = task_dir("pending", match)
        task_path.mkdir(parents=True, exist_ok=True)
        (task_path / "task.md").write_text(
            f"""---
task_id: {match}
title: {match}
priority: normal
worker: materialized_execution
status: pending
line: selector_line
batch: selector_batch
task_root: {target_root}
resource_hint: cpu
created_at: 2026-05-13T00:00:00+00:00
---

# Task

Selector hardening fixture.
""",
            encoding="utf-8",
        )
        save_registry(_registry_for(other, match), path=registry_path())

        selected = core.next_pending_task_id(worker="materialized_execution", dag_root=target_root)

        assert selected == match
    finally:
        save_registry(original, path=registry_path())
        _cleanup(other)
        _cleanup(match)


def test_any_hint_tasks_respect_cpu_lane_capacity():
    prefix = f"sel_any_{uuid.uuid4().hex[:8]}_"
    any_task = prefix + "any_task"
    original = load_registry()
    try:
        _write_pending_task(any_task, worker="materialized_execution", resource_hint="any")
        save_registry(_registry_for(any_task), path=registry_path())

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 1}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_cpu=1,
                dry_run=True,
                line="selector_line",
                prefix=prefix,
            )

        assert not plan["plan"]
        assert any(
            item.get("task_id") == any_task and item.get("reason") == "cpu_capacity_full"
            for item in plan["skipped"]
        )
    finally:
        save_registry(original, path=registry_path())
        _cleanup(any_task)

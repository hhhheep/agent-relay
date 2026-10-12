from __future__ import annotations

import shutil
import uuid
import json
from pathlib import Path

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import registry_path, result_latest_dir, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry


STATUSES = ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed")


def _cleanup(task_id: str) -> None:
    for status in STATUSES:
        path = task_dir(status, task_id)
        if path.exists():
            shutil.rmtree(path)
    result_path = result_latest_dir(task_id)
    if result_path.exists():
        shutil.rmtree(result_path)


def _write_task(
    task_id: str,
    *,
    status: str = "pending",
    line: str,
    batch: str,
    intent: str,
    needs_gpu: bool = False,
    depends_on: list[str] | None = None,
    requires_artifacts_from: str | None = None,
    outputs: list[str] | None = None,
    decision_contract: str = "",
) -> None:
    task_path = task_dir(status, task_id)
    task_path.mkdir(parents=True, exist_ok=True)
    deps = "\n".join(f"  - {dep}" for dep in (depends_on or []))
    outs = "\n".join(f"  - {item}" for item in (outputs or []))
    requires = f"requires_artifacts_from:\n  - {requires_artifacts_from}\n" if requires_artifacts_from else ""
    decision = decision_contract + "\n" if decision_contract else ""
    (task_path / "task.md").write_text(
        f"""---
task_id: {task_id}
title: {task_id}
priority: normal
worker: materialized_execution
status: {status}
line: {line}
batch: {batch}
intent: {intent}
needs_gpu: {str(needs_gpu).lower()}
depends_on:
{deps or "  []"}
{requires}outputs:
{outs or "  []"}
{decision}created_at: 2026-05-13T00:00:00+00:00
---

# Task

Simplified DAG fixture.
""",
        encoding="utf-8",
    )


def _save_registry(records: dict[str, dict]) -> None:
    save_registry({"tasks": records}, path=registry_path())


def test_dag_plan_metabolizes_simplified_taskbooks_into_lanes_and_waves():
    suffix = uuid.uuid4().hex[:8]
    line = f"line_{suffix}"
    batch = f"batch_{suffix}"
    root_id = f"dag_{suffix}_001_prepare"
    gpu_id = f"dag_{suffix}_002_gpu"
    cpu_id = f"dag_{suffix}_003_report"
    original = load_registry()
    try:
        _write_task(root_id, status="completed", line=line, batch=batch, intent="prepare", outputs=["inventory.json"])
        _write_task(gpu_id, line=line, batch=batch, intent="metrics_production", needs_gpu=True, depends_on=[root_id], outputs=["metrics.csv"])
        _write_task(cpu_id, line=line, batch=batch, intent="decision_synthesis", depends_on=[gpu_id], outputs=["decision_summary.json"])
        _save_registry(
            {
                root_id: {"task_id": root_id, "status": "completed", "worker": "materialized_execution"},
                gpu_id: {"task_id": gpu_id, "status": "pending", "worker": "materialized_execution"},
                cpu_id: {"task_id": cpu_id, "status": "pending", "worker": "materialized_execution"},
            }
        )

        plan = core.dag_plan(line=line, batch=batch)

        tasks = {item["task_id"]: item for item in plan["tasks"]}
        assert plan["dag_root"] == root_id
        assert tasks[gpu_id]["resource_lane"] == "gpu_run"
        assert tasks[cpu_id]["resource_lane"] == "cpu_analysis"
        assert tasks[gpu_id]["readiness"]["ready_full"] is True
        assert plan["waves"][0]["lanes"]["gpu_run"][0]["task_id"] == gpu_id
        assert plan["waves"][1]["lanes"]["cpu_analysis"][0]["task_id"] == cpu_id
    finally:
        save_registry(original, path=registry_path())
        for task_id in (root_id, gpu_id, cpu_id):
            _cleanup(task_id)


def test_dag_plan_reports_missing_artifact_blocker():
    suffix = uuid.uuid4().hex[:8]
    line = f"line_{suffix}"
    batch = f"batch_{suffix}"
    parent = f"dag_{suffix}_001_parent"
    child = f"dag_{suffix}_002_child"
    original = load_registry()
    try:
        _write_task(parent, status="completed", line=line, batch=batch, intent="prepare", outputs=["missing.csv"])
        _write_task(child, line=line, batch=batch, intent="decision_synthesis", requires_artifacts_from=parent, outputs=["decision_summary.json"])
        result_latest_dir(parent).mkdir(parents=True, exist_ok=True)
        _save_registry(
            {
                parent: {"task_id": parent, "status": "completed", "worker": "materialized_execution", "latest_result_path": str(result_latest_dir(parent))},
                child: {"task_id": child, "status": "pending", "worker": "materialized_execution"},
            }
        )

        plan = core.dag_plan(line=line, batch=batch)

        child_plan = next(item for item in plan["tasks"] if item["task_id"] == child)
        assert child_plan["readiness"]["ready"] is True

        task_path = task_dir("pending", child) / "task.md"
        text = task_path.read_text(encoding="utf-8")
        text = text.replace(f"  - {parent}", f"  - task_id: {parent}\n    artifacts:\n      - missing.csv")
        task_path.write_text(text, encoding="utf-8")

        blocked = core.dag_plan(line=line, batch=batch)
        child_blocked = next(item for item in blocked["tasks"] if item["task_id"] == child)
        assert child_blocked["readiness"]["ready"] is False
        assert child_blocked["readiness"]["blocked_by"][0]["reason"] == "required_artifact_missing"
    finally:
        save_registry(original, path=registry_path())
        for task_id in (parent, child):
            _cleanup(task_id)


def test_dag_plan_final_contract_reports_missing_required_output():
    suffix = uuid.uuid4().hex[:8]
    line = f"line_{suffix}"
    batch = f"batch_{suffix}"
    final_id = f"dag_{suffix}_999_final"
    original = load_registry()
    try:
        _write_task(
            final_id,
            status="completed",
            line=line,
            batch=batch,
            intent="decision_synthesis",
            outputs=["decision_summary.json"],
            decision_contract="decision_contract:\n  required_files:\n    - decision_summary.json\n  required_fields:\n    - recommended_use",
        )
        result_latest_dir(final_id).mkdir(parents=True, exist_ok=True)
        _save_registry(
            {
                final_id: {
                    "task_id": final_id,
                    "status": "completed",
                    "worker": "materialized_execution",
                    "latest_result_path": str(result_latest_dir(final_id)),
                }
            }
        )

        plan = core.dag_plan(line=line, batch=batch)

        final = plan["tasks"][0]
        assert final["final_contract_status"]["checked"] is True
        assert final["final_contract_status"]["ok"] is False
        assert final["final_contract_status"]["missing_files"] == ["decision_summary.json"]
    finally:
        save_registry(original, path=registry_path())
        _cleanup(final_id)


def test_dag_plan_final_contract_reports_missing_required_field():
    suffix = uuid.uuid4().hex[:8]
    line = f"line_{suffix}"
    batch = f"batch_{suffix}"
    final_id = f"dag_{suffix}_999_final"
    original = load_registry()
    try:
        _write_task(
            final_id,
            status="completed",
            line=line,
            batch=batch,
            intent="decision_synthesis",
            outputs=["decision_summary.json"],
            decision_contract="decision_contract:\n  primary_json: decision_summary.json\n  required_files:\n    - decision_summary.json\n  required_fields:\n    - recommended_use\n    - best_iou_gain",
        )
        result_latest_dir(final_id).mkdir(parents=True, exist_ok=True)
        (result_latest_dir(final_id) / "decision_summary.json").write_text(
            json.dumps({"recommended_use": "diagnostic_only"}),
            encoding="utf-8",
        )
        _save_registry(
            {
                final_id: {
                    "task_id": final_id,
                    "status": "completed",
                    "worker": "materialized_execution",
                    "latest_result_path": str(result_latest_dir(final_id)),
                }
            }
        )

        plan = core.dag_plan(line=line, batch=batch)

        final = plan["tasks"][0]
        assert final["final_contract_status"]["checked"] is True
        assert final["final_contract_status"]["ok"] is False
        assert "best_iou_gain" in "\n".join(final["final_contract_status"]["errors"])
    finally:
        save_registry(original, path=registry_path())
        _cleanup(final_id)

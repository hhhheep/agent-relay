import shutil
import unittest
import uuid

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import ensure_orchestrator_dirs, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.schema import now_iso


class DependencyReconcileTests(unittest.TestCase):
    def _write_child_task(self, task_id: str, parent_id: str) -> None:
        pending = task_dir("pending", task_id)
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "task.md").write_text(
            "\n".join(
                [
                    "---",
                    f"task_id: {task_id}",
                    "title: Analyze partial artifacts",
                    "priority: normal",
                    "worker: codex",
                    "status: pending",
                    f"created_at: {now_iso()}",
                    "resource_hint: cpu",
                    "input_requirements:",
                    f"  - from_task: {parent_id}",
                    "    resolve_strategy: latest_valid",
                    "    acceptable_status: [success, partial]",
                    "    required_artifacts:",
                    "      - _worker_out/result.yaml",
                    "      - _worker_out/metrics.json",
                    "      - _worker_out/worker_notes.md",
                    "    partial_policy: auto",
                    "    bind_on_promote: true",
                    "    mark_partial_input_in_report: true",
                    "---",
                    "",
                    "# Goal",
                    "Analyze the pinned parent run artifacts.",
                ]
            ),
            encoding="utf-8",
        )

    def test_partial_parent_artifacts_promote_child_to_ready_front(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        parent_id = f"exp_parent_partial_{suffix}"
        child_id = f"exp_child_analysis_{suffix}"
        run_id = f"{parent_id}__run"
        registry_backup = load_registry()
        try:
            with WorkspaceTempDir() as tmp:
                result_root = tmp / "result"
                result_root.mkdir(parents=True, exist_ok=True)
                (result_root / "result.yaml").write_text(
                    "\n".join(
                        [
                            f"task_id: {parent_id}",
                            f"run_id: {run_id}",
                            "status: partial",
                            "worker: codex",
                            "summary_short: partial metrics ready",
                        ]
                    ),
                    encoding="utf-8",
                )
                (result_root / "metrics.json").write_text('{"auc": 0.9}', encoding="utf-8")
                (result_root / "worker_notes.md").write_text("usable partial", encoding="utf-8")
                save_registry(
                    {
                        "tasks": {
                            parent_id: {
                                "task_id": parent_id,
                                "status": "partial",
                                "latest_run_id": run_id,
                                "latest_partial_run_id": run_id,
                                "latest_valid_run_id": run_id,
                                "valid_run_ids": [run_id],
                                "runs": [
                                    {
                                        "task_id": parent_id,
                                        "run_id": run_id,
                                        "status": "partial",
                                        "result_path": str(result_root),
                                        "artifact_contract_valid": True,
                                        "usable_as_input": True,
                                    }
                                ],
                            },
                            child_id: {"task_id": child_id, "status": "pending"},
                        }
                    }
                )
                self._write_child_task(child_id, parent_id)

                result = core.dependency_reconcile(task_id=child_id, apply_safe=True)
                record = load_registry()["tasks"][child_id]

                self.assertEqual(result["promoted"][0]["task_id"], child_id)
                self.assertEqual(record["queue_lane"], "ready_front")
                self.assertEqual(record["input_bindings"][parent_id]["run_id"], run_id)
                self.assertEqual(record["input_bindings"][parent_id]["source_status"], "partial")
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, child_id)
                if path.exists():
                    shutil.rmtree(path)

    def test_ready_front_sorts_before_normal_ready(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        front_id = f"exp_front_{suffix}"
        normal_id = f"exp_normal_{suffix}"
        registry_backup = load_registry()
        try:
            for task_id in (front_id, normal_id):
                pending = task_dir("pending", task_id)
                pending.mkdir(parents=True, exist_ok=True)
                (pending / "task.md").write_text(
                    "\n".join(
                        [
                            "---",
                            f"task_id: {task_id}",
                            f"title: {task_id}",
                            "priority: normal",
                            "worker: codex",
                            "status: pending",
                            f"created_at: {now_iso()}",
                            "---",
                            "",
                            "ready task",
                        ]
                    ),
                    encoding="utf-8",
                )
            save_registry(
                {
                    "tasks": {
                        front_id: {"task_id": front_id, "status": "pending", "queue_lane": "ready_front", "unblock_boost": 60},
                        normal_id: {"task_id": normal_id, "status": "pending"},
                    }
                }
            )

            ids = [str(item["task_id"]) for item in core.queue()]

            self.assertLess(ids.index(front_id), ids.index(normal_id))
        finally:
            save_registry(registry_backup)
            for task_id in (front_id, normal_id):
                for status in ("pending", "running", "completed", "partial", "failed"):
                    path = task_dir(status, task_id)
                    if path.exists():
                        shutil.rmtree(path)

    def test_same_parent_in_depends_on_and_input_requirements_is_conflict(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        parent_id = f"exp_parent_conflict_{suffix}"
        child_id = f"exp_child_conflict_{suffix}"
        run_id = f"{parent_id}__run"
        registry_backup = load_registry()
        try:
            with WorkspaceTempDir() as tmp:
                result_root = tmp / "result"
                result_root.mkdir(parents=True, exist_ok=True)
                (result_root / "result.yaml").write_text(
                    f"task_id: {parent_id}\nrun_id: {run_id}\nstatus: partial\nworker: codex\nsummary_short: partial\n",
                    encoding="utf-8",
                )
                (result_root / "metrics.json").write_text("{}", encoding="utf-8")
                (result_root / "worker_notes.md").write_text("notes", encoding="utf-8")
                save_registry(
                    {
                        "tasks": {
                            parent_id: {
                                "task_id": parent_id,
                                "status": "partial",
                                "latest_partial_run_id": run_id,
                                "latest_valid_run_id": run_id,
                                "runs": [
                                    {
                                        "task_id": parent_id,
                                        "run_id": run_id,
                                        "status": "partial",
                                        "result_path": str(result_root),
                                        "artifact_contract_valid": True,
                                        "usable_as_input": True,
                                    }
                                ],
                            },
                            child_id: {"task_id": child_id, "status": "pending"},
                        }
                    }
                )
                self._write_child_task(child_id, parent_id)
                task_path = task_dir("pending", child_id) / "task.md"
                text = task_path.read_text(encoding="utf-8")
                text = text.replace("resource_hint: cpu\n", f"resource_hint: cpu\ndepends_on: [{parent_id}]\n")
                task_path.write_text(text, encoding="utf-8")

                deps = core.dependency_state(core.load_task(task_path))

                self.assertFalse(deps["ready"])
                self.assertTrue(any(item.get("reason") == "dependency_semantic_conflict" for item in deps["incomplete"]))
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, child_id)
                if path.exists():
                    shutil.rmtree(path)

    def test_report_task_can_continue_past_soft_failed_parent_when_explicitly_allowed(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        parent_id = f"exp_parent_launch_failed_{suffix}"
        child_id = f"exp_child_decision_report_{suffix}"
        registry_backup = load_registry()
        try:
            child = task_dir("pending", child_id)
            child.mkdir(parents=True, exist_ok=True)
            (child / "task.md").write_text(
                "\n".join(
                    [
                        "---",
                        f"task_id: {child_id}",
                        "title: Decision report for incomplete branch",
                        "priority: normal",
                        "worker: materialized_execution",
                        "status: pending",
                        f"created_at: {now_iso()}",
                        "resource_hint: cpu",
                        "execution_stage: analysis",
                        "depends_on:",
                        f"  - {parent_id}",
                        "requires_artifacts_from:",
                        f"  - {parent_id}",
                        "allow_partial_dependencies:",
                        f"  - {parent_id}",
                        "allow_partial_artifacts_from:",
                        f"  - {parent_id}",
                        "---",
                        "",
                        "# Goal",
                        "Summarize what is available and what is missing.",
                    ]
                ),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        parent_id: {
                            "task_id": parent_id,
                            "status": "launch_failed",
                            "runs": [
                                {
                                    "task_id": parent_id,
                                    "run_id": f"{parent_id}__run",
                                    "status": "launch_failed",
                                }
                            ],
                        },
                        child_id: {"task_id": child_id, "status": "pending"},
                    }
                }
            )

            deps = core.dependency_state(core.load_task(child / "task.md"))

            self.assertTrue(deps["ready"])
            self.assertEqual(deps["missing"], [])
            self.assertEqual(deps["missing_artifacts"], [])
            self.assertEqual(deps["soft_dependencies"][0]["reason"], "dependency_terminal_incomplete_allowed_for_report")
            self.assertEqual(deps["soft_missing_artifacts"][0]["reason"], "artifact_missing_allowed_for_report")
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed", "launch_failed"):
                path = task_dir(status, child_id)
                if path.exists():
                    shutil.rmtree(path)

    def test_run_task_does_not_continue_past_soft_failed_parent(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        parent_id = f"exp_parent_failed_scale_{suffix}"
        child_id = f"exp_child_scale_run_{suffix}"
        registry_backup = load_registry()
        try:
            child = task_dir("pending", child_id)
            child.mkdir(parents=True, exist_ok=True)
            (child / "task.md").write_text(
                "\n".join(
                    [
                        "---",
                        f"task_id: {child_id}",
                        "title: Formal scale-up run",
                        "priority: normal",
                        "worker: materialized_execution",
                        "status: pending",
                        f"created_at: {now_iso()}",
                        "resource_hint: gpu",
                        "execution_stage: run",
                        "depends_on:",
                        f"  - {parent_id}",
                        "requires_artifacts_from:",
                        f"  - {parent_id}",
                        "allow_partial_dependencies:",
                        f"  - {parent_id}",
                        "allow_partial_artifacts_from:",
                        f"  - {parent_id}",
                        "---",
                        "",
                        "# Goal",
                        "Run the formal scale-up only when parent evidence is valid.",
                    ]
                ),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        parent_id: {"task_id": parent_id, "status": "launch_failed"},
                        child_id: {"task_id": child_id, "status": "pending"},
                    }
                }
            )

            deps = core.dependency_state(core.load_task(child / "task.md"))

            self.assertFalse(deps["ready"])
            self.assertEqual(deps["soft_dependencies"], [])
            self.assertEqual(deps["incomplete"][0]["task_id"], parent_id)
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed", "launch_failed"):
                path = task_dir(status, child_id)
                if path.exists():
                    shutil.rmtree(path)

    def test_requeued_pending_parent_does_not_satisfy_child_from_historical_run(self):
        ensure_orchestrator_dirs()
        suffix = uuid.uuid4().hex[:8]
        parent_id = f"exp_parent_requeued_{suffix}"
        child_id = f"exp_child_waits_for_fresh_parent_{suffix}"
        run_id = f"{parent_id}__old_run"
        registry_backup = load_registry()
        try:
            with WorkspaceTempDir() as tmp:
                result_root = tmp / "result"
                result_root.mkdir(parents=True, exist_ok=True)
                (result_root / "result.yaml").write_text(
                    f"task_id: {parent_id}\nrun_id: {run_id}\nstatus: success\nworker: materialized_execution\n",
                    encoding="utf-8",
                )
                child = task_dir("pending", child_id)
                child.mkdir(parents=True, exist_ok=True)
                (child / "task.md").write_text(
                    "\n".join(
                        [
                            "---",
                            f"task_id: {child_id}",
                            "title: Child must wait for rejudged parent",
                            "priority: normal",
                            "worker: materialized_execution",
                            "status: pending",
                            f"created_at: {now_iso()}",
                            "resource_hint: gpu",
                            "execution_stage: run",
                            "depends_on:",
                            f"  - {parent_id}",
                            "requires_artifacts_from:",
                            f"  - {parent_id}",
                            "---",
                            "",
                            "# Goal",
                            "Run only after the requeued parent completes again.",
                        ]
                    ),
                    encoding="utf-8",
                )
                save_registry(
                    {
                        "tasks": {
                            parent_id: {
                                "task_id": parent_id,
                                "status": "pending",
                                "task_status": "pending",
                                "latest_run_id": run_id,
                                "latest_success_run_id": run_id,
                                "latest_valid_run_id": run_id,
                                "latest_result_path": str(result_root),
                                "runs": [
                                    {
                                        "task_id": parent_id,
                                        "run_id": run_id,
                                        "status": "completed",
                                        "result_path": str(result_root),
                                        "artifact_contract_valid": True,
                                        "usable_as_input": True,
                                    }
                                ],
                            },
                            child_id: {"task_id": child_id, "status": "pending"},
                        }
                    }
                )

                deps = core.dependency_state(core.load_task(child / "task.md"))

                self.assertFalse(deps["ready"])
                self.assertEqual(deps["incomplete"][0]["task_id"], parent_id)
                self.assertEqual(deps["missing_artifacts"], [parent_id])
        finally:
            save_registry(registry_backup)
            for task_id in (child_id, parent_id):
                for status in ("pending", "running", "completed", "partial", "failed", "launch_failed"):
                    path = task_dir(status, task_id)
                    if path.exists():
                        shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()

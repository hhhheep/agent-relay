import shutil
import unittest
import uuid

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import ensure_orchestrator_dirs, result_latest_dir, result_run_dir, task_dir, worker_out_dir, worktree_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import render_new_task


class CollectTaskBackupTests(unittest.TestCase):
    def test_collect_copies_task_book_into_results(self):
        ensure_orchestrator_dirs()
        task_id = f"exp_collect_backup_{uuid.uuid4().hex[:8]}"
        run_id = f"{task_id}__run"
        registry_backup = load_registry()
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(render_new_task(task_id, "Collect backup scratch"), encoding="utf-8")
            out = worker_out_dir(task_id)
            out.mkdir(parents=True, exist_ok=True)
            (out / "result.yaml").write_text(
                "\n".join(
                    [
                        f"task_id: {task_id}",
                        f"run_id: {run_id}",
                        "status: success",
                        "worker: codex",
                        "summary_short: done",
                    ]
                ),
                encoding="utf-8",
            )
            (out / "worker_notes.md").write_text("notes", encoding="utf-8")
            (out / "stdout.log").write_text("", encoding="utf-8")
            (out / "stderr.log").write_text("", encoding="utf-8")
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Collect backup scratch",
                            "status": "running",
                            "latest_run_id": run_id,
                            "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                        }
                    }
                }
            )

            core.collect(task_id)

            self.assertTrue((result_latest_dir(task_id) / "task.md").exists())
            self.assertTrue((result_run_dir(task_id, run_id) / "task.md").exists())
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, task_id)
                if path.exists():
                    shutil.rmtree(path)
            for path in (worktree_dir(task_id), result_latest_dir(task_id).parent):
                if path.exists():
                    shutil.rmtree(path)

    def test_collect_does_not_snapshot_dirty_worktree_without_declared_changes(self):
        ensure_orchestrator_dirs()
        task_id = f"exp_collect_patch_hygiene_{uuid.uuid4().hex[:8]}"
        run_id = f"{task_id}__run"
        registry_backup = load_registry()
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(render_new_task(task_id, "Collect patch hygiene"), encoding="utf-8")
            wt = worktree_dir(task_id)
            wt.mkdir(parents=True, exist_ok=True)
            (wt / "dirty.txt").write_text("dirty but unrelated\n", encoding="utf-8")
            out = worker_out_dir(task_id)
            out.mkdir(parents=True, exist_ok=True)
            (out / "result.yaml").write_text(
                "\n".join(
                    [
                        f"task_id: {task_id}",
                        f"run_id: {run_id}",
                        "status: success",
                        "worker: materialized_execution",
                        "summary_short: done",
                        "changed_files: []",
                    ]
                ),
                encoding="utf-8",
            )
            (out / "worker_notes.md").write_text("notes", encoding="utf-8")
            (out / "stdout.log").write_text("", encoding="utf-8")
            (out / "stderr.log").write_text("", encoding="utf-8")
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Collect patch hygiene",
                            "status": "running",
                            "latest_run_id": run_id,
                            "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                        }
                    }
                }
            )

            core.collect(task_id)

            patch_text = (result_latest_dir(task_id) / "patch.diff").read_text(encoding="utf-8")
            report_text = (result_latest_dir(task_id) / "report.md").read_text(encoding="utf-8")
            self.assertIn("No source patch collected", patch_text)
            self.assertIn("No source changes declared by worker.", report_text)
            self.assertNotIn("dirty.txt", report_text)
        finally:
            save_registry(registry_backup)
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, task_id)
                if path.exists():
                    shutil.rmtree(path)
            for path in (worktree_dir(task_id), result_latest_dir(task_id).parent):
                if path.exists():
                    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()

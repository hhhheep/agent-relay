import shutil
import unittest
import uuid

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import ensure_orchestrator_dirs, registry_path, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import render_new_task


class ManualPauseTests(unittest.TestCase):
    def test_manual_pause_resets_task_to_pending_and_marks_run_partial(self):
        ensure_orchestrator_dirs()
        task_id = f"exp_manual_pause_{uuid.uuid4().hex[:8]}"
        registry_backup = load_registry()
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(render_new_task(task_id, "Manual pause scratch"), encoding="utf-8")
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Manual pause scratch",
                            "status": "running",
                            "latest_run_id": f"{task_id}__run",
                            "runs": [{"task_id": task_id, "run_id": f"{task_id}__run", "status": "running"}],
                        }
                    }
                },
                path=registry_path(),
            )

            with self.assertRaises(ValueError):
                core.manual_pause(task_id, reason="人工暫停")

            result = core.manual_pause(task_id, reason="人工暫停", explicit_requeue=True)
            record = load_registry()["tasks"][task_id]

            self.assertEqual(result["status"], "pending")
            self.assertTrue((task_dir("pending", task_id) / "task.md").exists())
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["runs"][0]["status"], "partial")
            self.assertEqual(record["runs"][0]["pause_reason"], "人工暫停")
            self.assertEqual(record["status_transition_source"], "manual_pause")
            self.assertEqual(record["status_transition_actor"], "user")
        finally:
            save_registry(registry_backup, path=registry_path())
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, task_id)
                if path.exists():
                    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()

import shutil
import unittest
import uuid
from unittest.mock import patch

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import ensure_orchestrator_dirs, registry_path, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import render_new_task


class RemoteStopTests(unittest.TestCase):
    def test_remote_stop_calls_ssh_and_resets_pending(self):
        ensure_orchestrator_dirs()
        task_id = f"exp_remote_stop_{uuid.uuid4().hex[:8]}"
        run_id = f"{task_id}__run"
        registry_backup = load_registry()
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(render_new_task(task_id, "Remote stop scratch"), encoding="utf-8")
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote stop scratch",
                            "status": "running",
                            "latest_run_id": run_id,
                            "remote": {
                                "host": "host",
                                "remote_root": "/remote/root",
                                "remote_task_dir": f"/remote/root/{run_id}",
                            },
                            "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                        }
                    }
                },
                path=registry_path(),
            )

            with patch("solution_agent.orchestrator.core.run_ssh", return_value={"returncode": 0, "stdout": "runner_alive:\nno\n", "stderr": ""}) as run_ssh:
                result = core.remote_stop(task_id, host="host", remote_root="/remote/root", reason="人工暫停")

            command = run_ssh.call_args.args[1]
            record = load_registry()["tasks"][task_id]
            self.assertIn("manual_pause", command)
            self.assertEqual(result["pause"]["status"], "pending")
            self.assertTrue((task_dir("pending", task_id) / "task.md").exists())
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["runs"][0]["status"], "partial")
        finally:
            save_registry(registry_backup, path=registry_path())
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, task_id)
                if path.exists():
                    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()

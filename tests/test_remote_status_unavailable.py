import shutil
import unittest
import uuid

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import ensure_orchestrator_dirs, registry_path, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import render_new_task


class RemoteStatusUnavailableTests(unittest.TestCase):
    def test_supervise_remote_does_not_infer_worker_death_from_ssh_failure(self):
        ensure_orchestrator_dirs()
        task_id = f"exp_remote_unreachable_{uuid.uuid4().hex[:8]}"
        registry_backup = load_registry()
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(render_new_task(task_id, "Remote unreachable scratch"), encoding="utf-8")
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote unreachable scratch",
                            "status": "running",
                            "remote": {"host": "host", "remote_root": "/remote/root"},
                        }
                    }
                },
                path=registry_path(),
            )

            payload = core.supervise_remote(
                task_id,
                host="host",
                remote_root="/remote/root",
                status_result={
                    "task_id": task_id,
                    "remote": "host:/remote/root/run/_worker_out",
                    "status": {"returncode": 255, "stdout": "", "stderr": "ssh: Could not resolve hostname"},
                    "cache": {"hit": False, "bulk": True},
                },
            )

            decision = payload["decision"]
            record = load_registry()["tasks"][task_id]
            self.assertEqual(decision["action"], "warn")
            self.assertEqual(decision["reason_code"], "remote_status_unavailable")
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["remote_health"], "unreachable")
            self.assertEqual(record["last_supervision"]["decision"]["reason_code"], "remote_status_unavailable")
        finally:
            save_registry(registry_backup, path=registry_path())
            for status in ("pending", "running", "completed", "partial", "failed"):
                path = task_dir(status, task_id)
                if path.exists():
                    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()

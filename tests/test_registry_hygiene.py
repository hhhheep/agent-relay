import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.core import manual_pause, registry_hygiene
from solution_agent.orchestrator.registry import load_registry, save_registry


class RegistryHygieneTests(unittest.TestCase):
    def test_reports_and_fixes_stale_collected_run_fields(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            save_registry(
                {
                    "tasks": {
                        "exp_done": {
                            "task_id": "exp_done",
                            "status": "completed",
                            "task_status": "running",
                            "run_phase": "collected",
                            "remote_state": "running",
                            "latest_run_id": "run1",
                            "runs": [
                                {
                                    "run_id": "run1",
                                    "status": "running",
                                    "task_status": "running",
                                    "run_phase": "execute_run",
                                    "state": "running",
                                    "remote_state": "running",
                                }
                            ],
                        }
                    }
                },
                path=path,
            )

            dry = registry_hygiene(path=path)
            applied = registry_hygiene(apply_safe=True, path=path)
            loaded = load_registry(path)
            run = loaded["tasks"]["exp_done"]["runs"][0]

        self.assertGreaterEqual(dry["issue_count"], 1)
        self.assertTrue(applied["applied"])
        self.assertEqual(loaded["tasks"]["exp_done"]["task_status"], "completed")
        self.assertEqual(loaded["tasks"]["exp_done"]["remote_state"], "collected")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["task_status"], "completed")
        self.assertEqual(run["run_phase"], "collected")
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["remote_state"], "collected")
        self.assertIn("collected_at", run)

    def test_pending_partial_reset_is_reported_not_auto_fixed(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            save_registry(
                {
                    "tasks": {
                        "exp_partial": {
                            "task_id": "exp_partial",
                            "status": "pending",
                            "task_status": "partial",
                            "latest_run_id": "run1",
                            "runs": [{"run_id": "run1", "status": "partial", "state": "manual_pause"}],
                        }
                    }
                },
                path=path,
            )

            report = registry_hygiene(apply_safe=True, path=path)
            loaded = load_registry(path)

        self.assertTrue(any(issue["code"] == "pending_task_has_terminal_task_status" for issue in report["issues"]))
        self.assertEqual(loaded["tasks"]["exp_partial"]["status"], "pending")
        self.assertEqual(loaded["tasks"]["exp_partial"]["task_status"], "partial")

    def test_registry_only_active_record_is_failed_by_apply_safe(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            save_registry(
                {
                    "tasks": {
                        "ghost_running_task": {
                            "task_id": "ghost_running_task",
                            "title": "Lost active registry record",
                            "status": "running",
                        }
                    }
                },
                path=path,
            )

            dry = registry_hygiene(path=path)
            applied = registry_hygiene(apply_safe=True, path=path)
            loaded = load_registry(path)
            record = loaded["tasks"]["ghost_running_task"]

        self.assertTrue(any(issue["code"] == "registry_only_active_task" for issue in dry["issues"]))
        self.assertEqual(applied["remaining_issues"], [])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["task_status"], "failed")
        self.assertEqual(record["status_transition_source"], "registry_hygiene")
        self.assertEqual(record["status_transition_reason"], "registry_only_active_task")

    def test_partial_cannot_reset_to_pending_without_explicit_requeue(self):
        with patch("solution_agent.orchestrator.core.find_task", return_value=("partial", Path("task.md"))):
            with self.assertRaises(ValueError):
                manual_pause("exp_partial", reason="test", reset_to_pending=True, explicit_requeue=False)


if __name__ == "__main__":
    unittest.main()

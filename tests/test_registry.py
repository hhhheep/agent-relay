import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.registry import load_registry, registry_journal_path, save_registry


class RegistryTests(unittest.TestCase):
    def test_registry_round_trip(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            payload = {"tasks": {"exp_test": {"task_id": "exp_test", "status": "pending"}}}
            save_registry(payload, path=path)

            loaded = load_registry(path=path)

        self.assertEqual(loaded["tasks"]["exp_test"]["status"], "pending")

    def test_registry_save_retries_transient_permission_error(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            payload = {"tasks": {"exp_test": {"task_id": "exp_test", "status": "pending"}}}
            original_replace = Path.replace
            attempts = {"count": 0}

            def flaky_replace(self, target):
                attempts["count"] += 1
                if attempts["count"] <= 3:
                    raise PermissionError("simulated transient lock")
                return original_replace(self, target)

            with patch("pathlib.Path.replace", flaky_replace), patch("solution_agent.orchestrator.registry.time.sleep", lambda _: None):
                save_registry(payload, path=path)

            loaded = load_registry(path=path)

        self.assertEqual(loaded["tasks"]["exp_test"]["status"], "pending")
        self.assertEqual(attempts["count"], 4)

    def test_registry_save_logs_final_permission_error(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            payload = {"tasks": {"exp_test": {"task_id": "exp_test", "status": "pending"}}}

            def locked_replace(self, target):
                raise PermissionError("simulated persistent lock")

            with patch("pathlib.Path.replace", locked_replace), patch("solution_agent.orchestrator.registry.time.sleep", lambda _: None):
                with self.assertRaises(PermissionError):
                    save_registry(payload, path=path)

            journal = registry_journal_path(path)
            events = journal.read_text(encoding="utf-8").splitlines()

        self.assertTrue(any("registry_save_failed" in line for line in events))


if __name__ == "__main__":
    unittest.main()

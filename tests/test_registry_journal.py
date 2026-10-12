import json
import unittest

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.registry import load_registry, mutate_registry, registry_journal_path, save_registry


class RegistryJournalTests(unittest.TestCase):
    def test_mutate_registry_writes_status_change_event(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "registry.json"
            save_registry({"tasks": {"exp_test": {"task_id": "exp_test", "status": "pending"}}}, path=path)

            def _mutate(payload):
                payload["tasks"]["exp_test"]["status"] = "running"
                payload["tasks"]["exp_test"]["remote_health"] = "reachable"

            mutate_registry(_mutate, path=path)
            journal = registry_journal_path(path)
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            loaded = load_registry(path)

        self.assertEqual(loaded["tasks"]["exp_test"]["status"], "running")
        self.assertEqual(events[-1]["task_id"], "exp_test")
        self.assertIn("status", events[-1]["changed"])
        self.assertIn("remote_health", events[-1]["changed"])


if __name__ == "__main__":
    unittest.main()

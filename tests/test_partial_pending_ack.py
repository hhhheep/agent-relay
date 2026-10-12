import unittest
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core


class PartialPendingAcknowledgmentTests(unittest.TestCase):
    def test_ack_current_makes_existing_unknown_warn_only(self):
        with WorkspaceTempDir() as tmp:
            registry_path = tmp / "registry.json"
            journal_path = tmp / "registry_events.jsonl"
            registry_path.write_text('{"schema_version": 1, "tasks": {}}\n', encoding="utf-8")
            journal_path.write_text(
                '{"ts":"2026-05-01T08:54:27+08:00","event":"task_record_changed","task_id":"exp_015","changed":["status"],"before_status":"partial","after_status":"pending"}\n',
                encoding="utf-8",
            )
            ack_path = tmp / "audits" / "acknowledged_partial_pending_audit.json"
            with patch("solution_agent.orchestrator.core.registry_journal_path", return_value=journal_path):
                ack = core.acknowledge_current_partial_pending_audit("reviewed legacy", path=registry_path, ack_path=ack_path)
                status = core.partial_pending_audit_ack_status(path=registry_path, ack_path=ack_path)

        self.assertEqual(ack["added_count"], 1)
        self.assertEqual(status["status"], "yellow")
        self.assertTrue(status["ok"])
        self.assertEqual(status["acknowledged_count"], 1)
        self.assertEqual(status["unacknowledged_count"], 0)

    def test_new_unknown_after_ack_remains_blocking(self):
        with WorkspaceTempDir() as tmp:
            registry_path = tmp / "registry.json"
            journal_path = tmp / "registry_events.jsonl"
            registry_path.write_text('{"schema_version": 1, "tasks": {}}\n', encoding="utf-8")
            journal_path.write_text(
                '{"ts":"2026-05-01T08:54:27+08:00","event":"task_record_changed","task_id":"exp_015","changed":["status"],"before_status":"partial","after_status":"pending"}\n',
                encoding="utf-8",
            )
            ack_path = tmp / "audits" / "acknowledged_partial_pending_audit.json"
            with patch("solution_agent.orchestrator.core.registry_journal_path", return_value=journal_path):
                core.acknowledge_current_partial_pending_audit("reviewed legacy", path=registry_path, ack_path=ack_path)
                journal_path.write_text(
                    journal_path.read_text(encoding="utf-8")
                    + '{"ts":"2026-05-01T11:00:00+08:00","event":"task_record_changed","task_id":"exp_016","changed":["status"],"before_status":"partial","after_status":"pending"}\n',
                    encoding="utf-8",
                )
                status = core.partial_pending_audit_ack_status(path=registry_path, ack_path=ack_path)

        self.assertEqual(status["status"], "red")
        self.assertFalse(status["ok"])
        self.assertEqual(status["acknowledged_count"], 1)
        self.assertEqual(status["unacknowledged_count"], 1)


if __name__ == "__main__":
    unittest.main()

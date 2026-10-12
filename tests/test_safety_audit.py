import unittest

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.core import _audit_result_safety


class SafetyAuditTests(unittest.TestCase):
    def test_audit_flags_destructive_command(self):
        with WorkspaceTempDir() as tmp:
            (tmp / "stderr.log").write_text("running rm -rf outputs/bad\n", encoding="utf-8")

            audit = _audit_result_safety(tmp)

        self.assertEqual(audit["high_count"], 1)
        self.assertEqual(audit["findings"][0]["code"], "destructive_rm")

    def test_audit_keeps_negated_install_as_note(self):
        with WorkspaceTempDir() as tmp:
            (tmp / "worker_notes.md").write_text("No pip install commands were run.\n", encoding="utf-8")

            audit = _audit_result_safety(tmp)

        self.assertEqual(audit["high_count"], 0)
        self.assertEqual(audit["medium_count"], 0)
        self.assertEqual(audit["findings"][0]["severity"], "note")


if __name__ == "__main__":
    unittest.main()

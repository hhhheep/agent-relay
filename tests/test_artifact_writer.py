"""Tests for artifact_writer.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.small_task_worker.artifact_writer import write_artifacts
from solution_agent.small_task_worker.executor import CommandResult, ExecutionResult
from solution_agent.small_task_worker.scope_planner import ProposedScope
from solution_agent.small_task_worker.task_contract import TaskContract


def _task(task_id="t") -> TaskContract:
    return TaskContract(task_id=task_id, title="Test")


def _scope(escalation=False, reason="") -> ProposedScope:
    return ProposedScope(
        risk="low", inspect_paths=[], modify_paths=["scripts/probes/"],
        commands_to_run=["python -m pytest"], reason="test",
        escalation_required=escalation, escalation_reason=reason,
    )


def _exec_ok() -> ExecutionResult:
    r = ExecutionResult(patch_applied=True, patch_error="")
    r.commands.append(CommandResult("python -m pytest", 0, "1 passed", "", 100))
    return r


def _exec_fail() -> ExecutionResult:
    r = ExecutionResult(patch_applied=True, patch_error="")
    r.commands.append(CommandResult("python -m pytest", 1, "", "FAILED test_x", 120))
    return r


def _exec_patch_fail() -> ExecutionResult:
    return ExecutionResult(patch_applied=False, patch_error="patch does not apply")


class TestResultYamlAlwaysWritten(unittest.TestCase):
    def test_written_on_success(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            self.assertTrue((tmp / "result.yaml").exists())

    def test_written_on_failure(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_fail(), tmp)
            self.assertTrue((tmp / "result.yaml").exists())

    def test_written_on_patch_error(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_patch_fail(), tmp)
            self.assertTrue((tmp / "result.yaml").exists())

    def test_written_on_escalation(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(escalation=True, reason="too broad"), None, tmp)
            self.assertTrue((tmp / "result.yaml").exists())

    def test_written_when_exec_result_is_none(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), None, tmp)
            self.assertTrue((tmp / "result.yaml").exists())


class TestResultYamlContent(unittest.TestCase):
    REQUIRED_KEYS = {"task_id", "status", "attempt_count", "scope",
                     "patch", "commands", "artifacts", "escalation_required", "created_at"}

    def _load(self, tmp: Path) -> dict:
        return yaml.safe_load((tmp / "result.yaml").read_text(encoding="utf-8"))

    def test_required_keys_on_success(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task("ok"), _scope(), _exec_ok(), tmp)
            data = self._load(tmp)
        for k in self.REQUIRED_KEYS:
            self.assertIn(k, data, f"missing key: {k}")

    def test_status_succeeded_on_success(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            data = self._load(tmp)
        self.assertEqual(data["status"], "succeeded")

    def test_status_failed_after_max_attempts(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_fail(), tmp, attempt_count=2)
            data = self._load(tmp)
        self.assertEqual(data["status"], "failed")

    def test_status_validation_failed_on_first_attempt(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_fail(), tmp, attempt_count=1)
            data = self._load(tmp)
        self.assertEqual(data["status"], "validation_failed")

    def test_status_escalation_required_on_high_risk(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(escalation=True), None, tmp)
            data = self._load(tmp)
        self.assertEqual(data["status"], "escalation_required")

    def test_task_id_preserved(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task("my_task"), _scope(), _exec_ok(), tmp)
            data = self._load(tmp)
        self.assertEqual(data["task_id"], "my_task")

    def test_yaml_is_safe_not_fstring(self):
        """YAML must be safe-dumped — no raw f-string serialization."""
        tricky = TaskContract(task_id="t: tricky\ninjection", title="T: test")
        with WorkspaceTempDir() as tmp:
            write_artifacts(tricky, _scope(), _exec_ok(), tmp)
            raw = (tmp / "result.yaml").read_text(encoding="utf-8")
        # safe_dump quotes the value — the colon injection must not be bare
        loaded = yaml.safe_load(raw)
        self.assertIn("task_id", loaded)

    def test_commands_list_in_result(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            data = self._load(tmp)
        self.assertIsInstance(data["commands"], list)
        self.assertEqual(len(data["commands"]), 1)
        self.assertEqual(data["commands"][0]["exit_code"], 0)


class TestWorkerNotesWritten(unittest.TestCase):
    def test_worker_notes_exists_on_success(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            self.assertTrue((tmp / "worker_notes.md").exists())

    def test_worker_notes_exists_on_failure(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_fail(), tmp)
            self.assertTrue((tmp / "worker_notes.md").exists())

    def test_worker_notes_contains_status(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            notes = (tmp / "worker_notes.md").read_text(encoding="utf-8")
        self.assertIn("succeeded", notes)

    def test_worker_notes_contains_escalation_reason(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(escalation=True, reason="scope too wide"),
                            None, tmp)
            notes = (tmp / "worker_notes.md").read_text(encoding="utf-8")
        self.assertIn("scope too wide", notes)


class TestArtifactsCopiedAccuracy(unittest.TestCase):
    def test_artifacts_list_only_present_files(self):
        with WorkspaceTempDir() as tmp:
            # write patch.diff manually to simulate executor
            (tmp / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
            (tmp / "stdout.log").write_text("ok", encoding="utf-8")
            (tmp / "stderr.log").write_text("", encoding="utf-8")
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            data = yaml.safe_load((tmp / "result.yaml").read_text(encoding="utf-8"))
        self.assertIn("patch.diff", data["artifacts"])
        self.assertIn("stdout.log", data["artifacts"])

    def test_artifacts_list_does_not_include_absent_files(self):
        with WorkspaceTempDir() as tmp:
            write_artifacts(_task(), _scope(), _exec_ok(), tmp)
            data = yaml.safe_load((tmp / "result.yaml").read_text(encoding="utf-8"))
        # feedback_packet.json was not written — must not appear
        self.assertNotIn("feedback_packet.json", data["artifacts"])


if __name__ == "__main__":
    unittest.main()

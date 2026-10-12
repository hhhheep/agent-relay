"""Tests for scope_planner.py."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.small_task_worker.task_contract import TaskContract
from solution_agent.small_task_worker.scope_planner import (
    FakeScopeAdapter, ProposedScope, _check_scope, plan_scope,
)


def _task(allowed_modify=None, allowed_commands=None) -> TaskContract:
    return TaskContract(
        task_id="t",
        title="Test",
        allowed_modify=allowed_modify or ["scripts/probes/", "tests/"],
        allowed_commands=allowed_commands or ["python -m pytest", "python -m py_compile"],
    )


class TestFakeScopeAdapter(unittest.TestCase):
    def test_returns_low_risk_by_default(self):
        t = _task()
        scope = FakeScopeAdapter().propose_scope(t, "")
        self.assertEqual(scope.risk, "low")
        self.assertFalse(scope.escalation_required)

    def test_custom_response_returned_verbatim(self):
        custom = ProposedScope(
            risk="high", inspect_paths=[], modify_paths=[],
            commands_to_run=[], reason="test", escalation_required=True,
        )
        scope = FakeScopeAdapter(custom).propose_scope(_task(), "")
        self.assertEqual(scope.risk, "high")
        self.assertTrue(scope.escalation_required)


class TestScopeCheck(unittest.TestCase):
    def test_forbidden_command_rejected(self):
        t = _task()
        scope = ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[],
            commands_to_run=["rm -rf /"], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNotNone(reason)
        self.assertIn("forbidden command", reason)

    def test_sensitive_path_rejected(self):
        t = _task()
        scope = ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[".env"],
            commands_to_run=[], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNotNone(reason)
        self.assertIn("sensitive", reason)

    def test_out_of_allowed_modify_rejected(self):
        t = _task(allowed_modify=["scripts/probes/"])
        scope = ProposedScope(
            risk="low", inspect_paths=[],
            modify_paths=["solution_agent/orchestrator/core.py"],
            commands_to_run=[], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNotNone(reason)
        self.assertIn("outside allowed_modify", reason)

    def test_allowed_modify_path_accepted(self):
        t = _task(allowed_modify=["scripts/probes/"])
        scope = ProposedScope(
            risk="low", inspect_paths=[],
            modify_paths=["scripts/probes/run_probe.py"],
            commands_to_run=[], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNone(reason)

    def test_disallowed_command_rejected(self):
        t = _task(allowed_commands=["python -m pytest"])
        scope = ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[],
            commands_to_run=["curl http://example.com"], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNotNone(reason)

    def test_allowed_command_accepted(self):
        t = _task(allowed_commands=["python -m pytest"])
        scope = ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[],
            commands_to_run=["python -m pytest tests/"], reason="test",
        )
        reason = _check_scope(scope, t)
        self.assertIsNone(reason)


class TestPlanScope(unittest.TestCase):
    def test_high_risk_scope_escalates(self):
        t = _task()
        high_risk = ProposedScope(
            risk="high", inspect_paths=[], modify_paths=[],
            commands_to_run=[], reason="too broad", escalation_required=True,
            escalation_reason="scope is too broad",
        )
        scope = plan_scope(t, FakeScopeAdapter(high_risk))
        self.assertTrue(scope.escalation_required)

    def test_forbidden_command_forces_escalation(self):
        t = _task()
        bad = ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[],
            commands_to_run=["sudo rm -rf /"], reason="test",
        )
        scope = plan_scope(t, FakeScopeAdapter(bad))
        self.assertTrue(scope.escalation_required)
        self.assertIn("forbidden command", scope.escalation_reason)

    def test_writes_proposed_scope_json(self):
        t = _task()
        with WorkspaceTempDir() as tmp:
            plan_scope(t, FakeScopeAdapter(), scope_dir=tmp)
            self.assertTrue((tmp / "proposed_scope.json").exists())
            data = json.loads((tmp / "proposed_scope.json").read_text(encoding="utf-8"))
        self.assertIn("risk", data)
        self.assertIn("modify_paths", data)

    def test_writes_proposed_scope_md(self):
        t = _task()
        with WorkspaceTempDir() as tmp:
            plan_scope(t, FakeScopeAdapter(), scope_dir=tmp)
            self.assertTrue((tmp / "proposed_scope.md").exists())

    def test_no_scope_dir_no_files_written(self):
        t = _task()
        scope = plan_scope(t, FakeScopeAdapter(), scope_dir=None)
        self.assertIsInstance(scope, ProposedScope)


if __name__ == "__main__":
    unittest.main()

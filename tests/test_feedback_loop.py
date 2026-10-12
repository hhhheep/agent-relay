"""Tests for the full runner feedback loop."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.small_task_worker.executor import FakePatchAdapter
from solution_agent.small_task_worker.feedback import build_feedback, FeedbackPacket
from solution_agent.small_task_worker.executor import CommandResult, ExecutionResult
from solution_agent.small_task_worker.runner import run_small_task_worker
from solution_agent.small_task_worker.scope_planner import FakeScopeAdapter, ProposedScope
from solution_agent.small_task_worker.task_contract import TaskContract


_TASK_MD = """\
---
task_id: loop_test
title: Feedback loop test
task_type: small_probe
worker: small_task_worker
status: pending
---

## Goal

Test the repair loop.

## Allowed Modify

- scripts/probes/

## Allowed Commands

```bash
python -m py_compile scripts/probes/noop.py
```
"""


def _write_task(tmp: Path) -> Path:
    p = tmp / "task.md"
    p.write_text(_TASK_MD, encoding="utf-8")
    return p


class _CallCountPatchAdapter:
    """Records how many times generate_patch is called and what feedbacks it received."""

    def __init__(self, patches):
        self._patches = list(patches)
        self.call_count = 0
        self.received_feedbacks: list[str] = []

    def generate_patch(self, task, scope, context, feedback):
        self.received_feedbacks.append(feedback)
        idx = min(self.call_count, len(self._patches) - 1)
        self.call_count += 1
        return self._patches[idx]


class _AlwaysFailScope(FakeScopeAdapter):
    """Returns a scope whose commands always fail (command doesn't exist)."""

    def propose_scope(self, task, context):
        return ProposedScope(
            risk="low",
            inspect_paths=[],
            modify_paths=[],
            commands_to_run=["python -m py_compile scripts/probes/noop.py"],
            reason="test",
        )


class _AlwaysPassScope(FakeScopeAdapter):
    def propose_scope(self, task, context):
        return ProposedScope(
            risk="low",
            inspect_paths=[],
            modify_paths=[],
            commands_to_run=[],
            reason="test — no commands",
        )


# ---------------------------------------------------------------------------
# result.yaml always written
# ---------------------------------------------------------------------------

class TestResultYamlAlwaysWritten(unittest.TestCase):
    def test_written_on_success_path(self):
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            run_small_task_worker(task_path, worktree, tmp / "out",
                                  scope_adapter=_AlwaysPassScope())
            self.assertTrue((tmp / "out" / "result.yaml").exists())

    def test_written_on_escalation_path(self):
        high = ProposedScope(
            risk="high", inspect_paths=[], modify_paths=[],
            commands_to_run=[], reason="too broad",
            escalation_required=True, escalation_reason="test escalation",
        )
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            run_small_task_worker(task_path, worktree, tmp / "out",
                                  scope_adapter=FakeScopeAdapter(high))
            self.assertTrue((tmp / "out" / "result.yaml").exists())


# ---------------------------------------------------------------------------
# escalation stops before patch generation
# ---------------------------------------------------------------------------

class TestEscalationStopsBeforePatch(unittest.TestCase):
    def test_patch_adapter_not_called_on_escalation(self):
        high = ProposedScope(
            risk="high", inspect_paths=[], modify_paths=[],
            commands_to_run=[], reason="too broad",
            escalation_required=True, escalation_reason="test",
        )
        patch_adapter = _CallCountPatchAdapter([""])
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            run_small_task_worker(task_path, worktree, tmp / "out",
                                  scope_adapter=FakeScopeAdapter(high),
                                  patch_adapter=patch_adapter)
        self.assertEqual(patch_adapter.call_count, 0,
                         "patch adapter must not be called when scope escalates")


# ---------------------------------------------------------------------------
# repair is bounded to max 2 attempts
# ---------------------------------------------------------------------------

class TestRepairBounded(unittest.TestCase):
    def test_at_most_two_patch_calls(self):
        patch_adapter = _CallCountPatchAdapter(["", "", "", ""])
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            run_small_task_worker(task_path, worktree, tmp / "out",
                                  scope_adapter=_AlwaysPassScope(),
                                  patch_adapter=patch_adapter)
        self.assertLessEqual(patch_adapter.call_count, 2,
                             "runner must not call patch adapter more than 2 times")

    def test_second_attempt_receives_feedback(self):
        # Use a scope that runs a command that will fail (file not found)
        failing_scope = FakeScopeAdapter(ProposedScope(
            risk="low", inspect_paths=[], modify_paths=[],
            commands_to_run=["python -m py_compile /nonexistent_file_xyz.py"],
            reason="test",
        ))
        patch_adapter = _CallCountPatchAdapter(["", ""])
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            run_small_task_worker(task_path, worktree, tmp / "out",
                                  scope_adapter=failing_scope,
                                  patch_adapter=patch_adapter)
        if patch_adapter.call_count == 2:
            self.assertNotEqual(patch_adapter.received_feedbacks[1], "",
                                "second attempt must receive non-empty feedback")

    def test_scope_cannot_expand_during_repair(self):
        """Repair patch adapter must not be able to change scope."""
        patch_adapter = _CallCountPatchAdapter(["", ""])
        # Use the exact command from _TASK_MD allowed list so scope check passes,
        # but the file doesn't exist in worktree so execution fails.
        failing_scope = FakeScopeAdapter(ProposedScope(
            risk="low", inspect_paths=[], modify_paths=["scripts/probes/"],
            commands_to_run=["python -m py_compile scripts/probes/noop.py"],
            reason="test",
        ))
        with WorkspaceTempDir() as tmp:
            task_path = _write_task(tmp)
            worktree = tmp / "worktree"
            worktree.mkdir()
            result = run_small_task_worker(task_path, worktree, tmp / "out",
                                           scope_adapter=failing_scope,
                                           patch_adapter=patch_adapter)
        # scope.modify_paths must not change between attempts
        self.assertEqual(result["scope"]["modify_paths"], ["scripts/probes/"])


# ---------------------------------------------------------------------------
# feedback packet
# ---------------------------------------------------------------------------

class TestFeedbackPacket(unittest.TestCase):
    def _failed_exec(self, stderr="FAILED test_x: AssertionError") -> ExecutionResult:
        r = ExecutionResult(patch_applied=True, patch_error="")
        r.commands.append(CommandResult("python -m pytest", 1, "", stderr, 100))
        return r

    def test_build_feedback_returns_none_on_success(self):
        r = ExecutionResult(patch_applied=True, patch_error="")
        r.commands.append(CommandResult("cmd", 0, "ok", "", 50))
        self.assertIsNone(build_feedback(r, attempt=1))

    def test_build_feedback_nonempty_on_failure(self):
        packet = build_feedback(self._failed_exec(), attempt=1)
        self.assertIsNotNone(packet)
        self.assertIsInstance(packet, FeedbackPacket)

    def test_feedback_text_contains_attempt_number(self):
        packet = build_feedback(self._failed_exec(), attempt=2)
        self.assertIn("2", packet.feedback_text)

    def test_feedback_text_contains_error(self):
        packet = build_feedback(self._failed_exec("KeyError: 'status'"), attempt=1)
        self.assertIn("KeyError", packet.feedback_text)

    def test_feedback_text_contains_safety_prohibition(self):
        packet = build_feedback(self._failed_exec(), attempt=1)
        self.assertIn("Do not modify source code outside the proposed scope", packet.feedback_text)

    def test_feedback_does_not_grant_permission(self):
        packet = build_feedback(self._failed_exec(), attempt=1)
        grants = ["you may modify", "allowed to run training", "you can execute",
                  "permission to delete", "it is ok to overwrite"]
        for phrase in grants:
            self.assertNotIn(phrase.lower(), packet.feedback_text.lower(),
                             f"feedback must not grant: {phrase!r}")

    def test_writes_feedback_packet_json(self):
        with WorkspaceTempDir() as tmp:
            build_feedback(self._failed_exec(), attempt=1, out_dir=tmp)
            self.assertTrue((tmp / "feedback_packet.json").exists())

    def test_patch_error_no_actionable_feedback_returns_none(self):
        r = ExecutionResult(patch_applied=False, patch_error="")
        # no stderr, no errors — nothing actionable
        result = build_feedback(r, attempt=1)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# no hidden model calls
# ---------------------------------------------------------------------------

class TestNoHiddenModelCalls(unittest.TestCase):
    def test_fake_adapters_do_not_call_network(self):
        """FakeScopeAdapter and FakePatchAdapter must never touch the network."""
        import socket
        original_connect = socket.socket.connect

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("network call detected in small_task_worker with fake adapters")

        socket.socket.connect = deny_connect
        try:
            with WorkspaceTempDir() as tmp:
                task_path = _write_task(tmp)
                worktree = tmp / "worktree"
                worktree.mkdir()
                run_small_task_worker(task_path, worktree, tmp / "out",
                                      scope_adapter=_AlwaysPassScope(),
                                      patch_adapter=FakePatchAdapter(""))
        finally:
            socket.socket.connect = original_connect


if __name__ == "__main__":
    unittest.main()

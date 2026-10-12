"""Tests for Phase 2C: bounded feedback retry controller.

Coverage:
  1. _build_feedback_text() — feedback packet construction
  2. RetryController — attempt loop scenarios
  3. feedback_text injection into MaterializerClient.materialize()
  4. controller.py single-attempt mode when feedback_text is provided
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.remote_materializer.controller import run_materializer
from solution_agent.remote_materializer.model_clients import (
    StaticMaterializerClient,
    StaticReaderClient,
)
from solution_agent.orchestrator.retry_controller import (
    RetryController,
    RetryResult,
    _build_feedback_text,
    _exception_to_mat_result,
)


# ---------------------------------------------------------------------------
# Helpers shared with test_remote_materializer_controller.py
# ---------------------------------------------------------------------------

_FORBIDDEN = [
    "modify_source_code", "execute_training",
    "change_dataset_split", "overwrite_checkpoint", "delete_files",
]


def _work_order(task_id: str, outputs: list[str]) -> str:
    return json.dumps({
        "status": "ready",
        "task_id": task_id,
        "task_type": "training_validation",
        "source_task_path": f"tasks/{task_id}/task.md",
        "jobs": [{"job_id": "generate", "worker": "qwen_coder_30b", "job_type": "generate_materials"}],
        "required_outputs": outputs,
        "input_files": [f"tasks/{task_id}/task.md"],
        "allowed_output_root": f"runs/{task_id}/",
        "forbidden_actions": _FORBIDDEN,
        "validation_steps": ["validate_yaml_schema", "validate_commands_allowlist"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["validator_failed_twice"]},
    })


def _task_md(task_id: str) -> str:
    return f"""---
task_id: {task_id}
title: Test
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-01T00:00:00+08:00
task_type: training_validation
---

# Goal

Retry controller test.
"""


def _valid_qwen_output(task_id: str) -> str:
    return f"""```yaml file="train_config.yaml"
task_id: {task_id}
output_dir: runs/{task_id}/generated
```
```md file="commands.md"
python scripts/run_validation.py --config runs/{task_id}/generated/train_config.yaml
```
```yaml file="output_contract.yaml"
task_id: {task_id}
required_files:
  - generated/train_config.yaml
```
"""


# ---------------------------------------------------------------------------
# 1. _build_feedback_text()
# ---------------------------------------------------------------------------

class TestBuildFeedbackText(unittest.TestCase):
    def test_returns_empty_when_no_errors(self):
        result = _build_feedback_text("validation_failed", [], attempt=1)
        self.assertEqual(result, "")

    def test_returns_empty_when_all_errors_blank(self):
        result = _build_feedback_text("validation_failed", ["", "   ", ""], attempt=1)
        self.assertEqual(result, "")

    def test_includes_attempt_number(self):
        result = _build_feedback_text("validation_failed", ["missing output_contract.yaml"], attempt=1)
        self.assertIn("attempt 1", result)

    def test_includes_status(self):
        result = _build_feedback_text("escalation_required", ["error a"], attempt=2)
        self.assertIn("escalation_required", result)

    def test_includes_all_errors(self):
        errors = ["error_a", "error_b", "error_c"]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        for err in errors:
            self.assertIn(err, result)

    def test_caps_at_max_errors(self):
        errors = [f"error_{i}" for i in range(20)]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        # Only first 10 should appear
        for i in range(10):
            self.assertIn(f"error_{i}", result)
        # Items beyond cap should not appear
        self.assertNotIn("error_10", result)

    def test_includes_safety_instructions(self):
        result = _build_feedback_text("validation_failed", ["some error"], attempt=1)
        self.assertIn("source code", result.lower())
        self.assertIn("training", result.lower())


# ---------------------------------------------------------------------------
# 2. _exception_to_mat_result()
# ---------------------------------------------------------------------------

class TestExceptionToMatResult(unittest.TestCase):
    def test_status_is_escalation_required(self):
        result = _exception_to_mat_result("task_x", RuntimeError("boom"))
        self.assertEqual(result["status"], "escalation_required")

    def test_captures_exception_type(self):
        result = _exception_to_mat_result("task_x", ValueError("bad value"))
        self.assertIn("ValueError", result["summary_short"])

    def test_validation_errors_contains_message(self):
        result = _exception_to_mat_result("task_x", RuntimeError("api down"))
        self.assertTrue(any("api down" in e for e in result["validation_errors"]))

    def test_task_id_set(self):
        result = _exception_to_mat_result("task_x", RuntimeError("x"))
        self.assertEqual(result["task_id"], "task_x")


# ---------------------------------------------------------------------------
# 3. RetryController — attempt loop scenarios (using fake run_remote_materializer)
# ---------------------------------------------------------------------------

class _FakeRetryController(RetryController):
    """Subclass that replaces run_remote_materializer with a configurable fake."""

    def __init__(self, task_id, task_path, mat_out, responses):
        super().__init__(
            task_id=task_id,
            task_path=task_path,
            mat_out=mat_out,
            policy_path=Path("/fake/policy.yaml"),
            env_file=Path("/fake/.env"),
            max_attempts=len(responses),
            stop_repeated_failure=True,
        )
        self._responses = list(responses)
        self._call_count = 0
        self.received_feedbacks: list[str] = []

    def run_attempts(self) -> RetryResult:
        # Patch run_remote_materializer locally
        import solution_agent.orchestrator.retry_controller as rc_mod
        original = rc_mod.run_remote_materializer
        controller_self = self

        def fake_run(task_path, output_root, env_file, policy_path, feedback_text=""):
            controller_self.received_feedbacks.append(feedback_text)
            idx = controller_self._call_count
            controller_self._call_count += 1
            output_root.mkdir(parents=True, exist_ok=True)
            response = controller_self._responses[min(idx, len(controller_self._responses) - 1)]
            if isinstance(response, Exception):
                raise response
            return response

        rc_mod.run_remote_materializer = fake_run
        try:
            return super().run_attempts()
        finally:
            rc_mod.run_remote_materializer = original


def _mat_result(status: str, errors: list[str] | None = None) -> dict:
    return {
        "task_id": "t",
        "status": status,
        "validation_errors": errors or [],
        "summary_short": f"status={status}",
        "generated_files": [],
        "validation_summary": {"status": "pass" if status == "materialized" else "fail", "failed_validators": []},
    }


class TestRetryControllerLoop(unittest.TestCase):
    def _controller(self, task_id, tmp, responses):
        return _FakeRetryController(
            task_id=task_id,
            task_path=tmp / "task.md",
            mat_out=tmp / "materializer",
            responses=responses,
        )

    def test_success_on_first_attempt_no_retry(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [_mat_result("materialized")])
            result = ctrl.run_attempts()
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.feedback_used)
        self.assertEqual(ctrl._call_count, 1)

    def test_fail_then_success_uses_feedback(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["missing output_contract.yaml"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(result.attempts, 2)
        self.assertTrue(result.feedback_used)
        self.assertEqual(ctrl._call_count, 2)
        # First call has no feedback; second has feedback
        self.assertEqual(ctrl.received_feedbacks[0], "")
        self.assertIn("missing output_contract.yaml", ctrl.received_feedbacks[1])

    def test_both_attempts_fail_returns_last_result(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["error a"]),
                _mat_result("validation_failed", ["error b"]),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.mat_result["status"], "validation_failed")

    def test_repeated_failure_fingerprint_stops_before_third_provider_call(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["same validator error"]),
                _mat_result("validation_failed", ["same validator error"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 2)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            result.mat_result["retry_metadata"]["halted_reason"],
            "repeated_failure_fingerprint",
        )

    def test_changed_failure_fingerprint_can_reach_third_attempt(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["missing config"]),
                _mat_result("validation_failed", ["invalid metric"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 3)
        self.assertEqual(result.mat_result["status"], "materialized")

    def test_no_retry_when_no_actionable_errors(self):
        with WorkspaceTempDir() as tmp:
            # escalation_required with empty validation_errors → feedback_text="" → no retry
            ctrl = self._controller("t", tmp, [
                _mat_result("escalation_required", []),
                _mat_result("materialized"),  # would succeed but shouldn't be called
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 1)
        self.assertFalse(result.feedback_used)

    def test_exception_on_only_attempt_gives_escalation(self):
        # max_attempts=1 (single response) → exception → no retry → escalation_required
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [RuntimeError("API down")])
            result = ctrl.run_attempts()
        self.assertEqual(result.mat_result["status"], "escalation_required")
        self.assertEqual(result.attempts, 1)

    def test_exception_then_success_retries_with_feedback(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                RuntimeError("API down"),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        # Exception has validation_errors from _exception_to_mat_result → feedback built
        self.assertEqual(ctrl._call_count, 2)

    def test_attempt_directories_are_per_attempt(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        # final_attempt_dir should point to attempt_002
        self.assertIn("attempt_002", str(result.final_attempt_dir))

    def test_retry_metadata_recorded(self):
        with WorkspaceTempDir() as tmp:
            ctrl = self._controller("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        meta = result.mat_result.get("retry_metadata", {})
        self.assertEqual(meta["total_attempts"], 2)
        self.assertIn("materialized", meta["attempt_statuses"])


# ---------------------------------------------------------------------------
# 4. feedback_text injection end-to-end through controller.py
# ---------------------------------------------------------------------------

class TestFeedbackInjectionEndToEnd(unittest.TestCase):
    """Verify that feedback_text flows from run_materializer() into materialize()."""

    def test_feedback_text_passed_to_materializer_client(self):
        received: list[str] = []

        class RecordingMaterializerClient:
            def materialize(self, work_order_json, prompt_text, context_pack_text, schemas_text, feedback_text=""):
                received.append(feedback_text)
                # Return valid output for the task
                return _valid_qwen_output("fb_test")

        outputs = ["train_config.yaml", "commands.md", "output_contract.yaml"]
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md("fb_test"), encoding="utf-8")
            run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(_work_order("fb_test", outputs)),
                RecordingMaterializerClient(),
                feedback_text="## feedback_from_previous_attempt\n\nFix the missing file.",
            )
        self.assertEqual(len(received), 1)
        self.assertIn("Fix the missing file", received[0])

    def test_single_attempt_when_feedback_provided(self):
        """When feedback_text is non-empty the internal retry loop runs exactly once."""
        call_count = [0]

        class CountingMaterializerClient:
            def materialize(self, *args, feedback_text=""):
                call_count[0] += 1
                # Always fail validation to confirm no internal retry
                return "```yaml file='train_config.yaml'\nbad: {\n```"

        outputs = ["train_config.yaml", "commands.md", "output_contract.yaml"]
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md("single_test"), encoding="utf-8")
            run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(_work_order("single_test", outputs)),
                CountingMaterializerClient(),
                feedback_text="some feedback",  # triggers single-attempt mode
            )
        self.assertEqual(call_count[0], 1)

    def test_normal_retry_without_feedback(self):
        """Without feedback_text the internal retry loop respects qwen_max_retries."""
        call_count = [0]

        class CountingMaterializerClient:
            def materialize(self, *args, feedback_text=""):
                call_count[0] += 1
                return "```yaml file='train_config.yaml'\nbad: {\n```"

        # work_order specifies qwen_max_retries=2 → 3 total attempts
        outputs = ["train_config.yaml", "commands.md", "output_contract.yaml"]
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md("retry_test"), encoding="utf-8")
            run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(_work_order("retry_test", outputs)),
                CountingMaterializerClient(),
                feedback_text="",  # no feedback → use work_order retry policy
            )
        # qwen_max_retries=2 means range(3) = 3 calls
        self.assertEqual(call_count[0], 3)


if __name__ == "__main__":
    unittest.main()

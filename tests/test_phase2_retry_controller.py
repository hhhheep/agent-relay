"""Phase 2 bounded feedback retry controller boundary tests.

P0: max_attempts must be strictly enforced — no attempt_003 when max_attempts=2.
P0: attempt artifact directories must be preserved independently — attempt_001
     must NOT be overwritten by attempt_002.
P1: LLM ignoring feedback must not cause infinite loop — stops at max_attempts.
P1: Escalation with no actionable errors must not retry.
P1: Exception artifacts are recorded and raw_exc_info captured for scrubbing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

import solution_agent.orchestrator.retry_controller as _retry_mod
from solution_agent.orchestrator.retry_controller import (
    RetryController,
    RetryResult,
    _build_feedback_text,
)


def _mat_result(status: str, errors=None) -> dict:
    return {
        "task_id": "t",
        "status": status,
        "validation_errors": list(errors or []),
        "summary_short": f"status={status}",
        "generated_files": [],
        "validation_summary": {
            "status": "pass" if status == "materialized" else "fail",
            "failed_validators": [],
        },
    }


class _FakeController(RetryController):
    """Patches run_remote_materializer for deterministic testing."""

    def __init__(self, task_id, tmp, responses):
        super().__init__(
            task_id=task_id,
            task_path=tmp / "task.md",
            mat_out=tmp / "mat_out",
            policy_path=Path("/fake/policy.yaml"),
            env_file=Path("/fake/.env"),
            max_attempts=len(responses),
        )
        self._responses = list(responses)
        self._call_count = 0
        self.received_feedbacks: list[str] = []
        self.written_markers: list[str] = []

    def run_attempts(self) -> RetryResult:
        original = _retry_mod.run_remote_materializer
        ctrl = self

        def fake_run(task_path, output_root, env_file, policy_path, feedback_text=""):
            ctrl.received_feedbacks.append(feedback_text)
            idx = ctrl._call_count
            ctrl._call_count += 1
            output_root.mkdir(parents=True, exist_ok=True)
            run_dir = output_root / ctrl.task_id
            run_dir.mkdir(parents=True, exist_ok=True)
            # Write a unique per-attempt marker so we can verify preservation
            marker = f"ATTEMPT_MARKER_{idx + 1}"
            (run_dir / "marker.txt").write_text(marker, encoding="utf-8")
            ctrl.written_markers.append(marker)
            resp = ctrl._responses[min(idx, len(ctrl._responses) - 1)]
            if isinstance(resp, Exception):
                raise resp
            return resp

        _retry_mod.run_remote_materializer = fake_run
        try:
            return super().run_attempts()
        finally:
            _retry_mod.run_remote_materializer = original


# ---------------------------------------------------------------------------
# P0: max_attempts strictly enforced
# ---------------------------------------------------------------------------

class TestMaxAttemptsEnforced(unittest.TestCase):
    def test_no_third_attempt_when_max_is_2(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["error a"]),
                _mat_result("validation_failed", ["error b"]),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 2, "Must not exceed max_attempts=2")
        self.assertEqual(result.attempts, 2)

    def test_stops_immediately_on_success(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("materialized"),
                _mat_result("materialized"),  # must not be called
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 1, "Must stop after first success")

    def test_single_attempt_max_is_respected(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["err"]),
            ])
            ctrl.max_attempts = 1
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 1)
        self.assertEqual(result.attempts, 1)


# ---------------------------------------------------------------------------
# P1: artifact preservation — attempt_001 must survive attempt_002
# ---------------------------------------------------------------------------

class TestAttemptArtifactPreservation(unittest.TestCase):
    """P1: Each attempt writes to its own subdirectory; no overwrite."""

    def test_both_attempt_dirs_exist_after_two_failures(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("validation_failed", ["e"]),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            self.assertTrue((mat_out / "attempts" / "attempt_001").exists(),
                            "attempt_001 dir must exist")
            self.assertTrue((mat_out / "attempts" / "attempt_002").exists(),
                            "attempt_002 dir must exist")

    def test_attempt_001_content_not_overwritten_by_attempt_002(self):
        """P1: ATTEMPT_MARKER_1 must still be in attempt_001 after attempt_002 runs."""
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            marker_1 = (mat_out / "attempts" / "attempt_001" / "t" / "marker.txt").read_text(encoding="utf-8")
            marker_2 = (mat_out / "attempts" / "attempt_002" / "t" / "marker.txt").read_text(encoding="utf-8")
            self.assertEqual(marker_1, "ATTEMPT_MARKER_1",
                             "attempt_001 marker must not be overwritten by attempt_002")
            self.assertEqual(marker_2, "ATTEMPT_MARKER_2")

    def test_final_attempt_dir_points_to_last_attempt(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertIn("attempt_002", str(result.final_attempt_dir))

    def test_final_attempt_dir_points_to_attempt_001_on_single_success(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [_mat_result("materialized")])
            result = ctrl.run_attempts()
        self.assertIn("attempt_001", str(result.final_attempt_dir))

    def test_attempt_dir_created_even_on_exception(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [RuntimeError("API down")])
            ctrl.max_attempts = 1
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            self.assertTrue((mat_out / "attempts" / "attempt_001").exists(),
                            "attempt_001 dir must exist even when exception occurs")


# ---------------------------------------------------------------------------
# P1: LLM ignores feedback → stop at max_attempts, not infinite loop
# ---------------------------------------------------------------------------

class TestLLMIgnoresFeedback(unittest.TestCase):
    def test_same_error_after_feedback_stops_at_max_attempts(self):
        """P1: If LLM gives the same broken output after feedback, stop at max_attempts."""
        same_error = "missing status field in result.yaml"
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", [same_error]),
                _mat_result("validation_failed", [same_error]),  # LLM ignored feedback
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 2, "Must stop at max_attempts even when LLM ignores feedback")
        self.assertNotEqual(result.mat_result["status"], "materialized")

    def test_feedback_was_sent_on_second_attempt(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["error x"]),
                _mat_result("validation_failed", ["error x"]),
            ])
            ctrl.run_attempts()
        self.assertEqual(ctrl.received_feedbacks[0], "")
        self.assertIn("error x", ctrl.received_feedbacks[1],
                      "Feedback must include the validation error")

    def test_escalation_no_errors_stops_without_retry(self):
        """Escalation with empty validation_errors must not retry — no actionable feedback."""
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("escalation_required", []),   # no errors → no feedback possible
                _mat_result("materialized"),              # must not be called
            ])
            ctrl.max_attempts = 2
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 1,
                         "Empty validation_errors must prevent retry (no feedback to send)")
        self.assertFalse(result.feedback_used)


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

class TestExceptionHandling(unittest.TestCase):
    def test_exception_captured_in_raw_exc_info(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [RuntimeError("API key=sk-secret")])
            ctrl.max_attempts = 1
            result = ctrl.run_attempts()
        self.assertIsNotNone(result.raw_exc_info)
        self.assertIn("RuntimeError", result.raw_exc_info)

    def test_exception_becomes_escalation_required(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [RuntimeError("timeout")])
            ctrl.max_attempts = 1
            result = ctrl.run_attempts()
        self.assertEqual(result.mat_result["status"], "escalation_required")

    def test_exception_then_success_uses_retry(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                RuntimeError("API down"),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertEqual(ctrl._call_count, 2)
        self.assertEqual(result.mat_result["status"], "materialized")

    def test_retry_metadata_recorded_in_result(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _FakeController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        meta = result.mat_result.get("retry_metadata", {})
        self.assertEqual(meta.get("total_attempts"), 2)
        self.assertIn("materialized", meta.get("attempt_statuses", []))


# ---------------------------------------------------------------------------
# Feedback content safety
# ---------------------------------------------------------------------------

class TestFeedbackSafetyConstraints(unittest.TestCase):
    def test_feedback_does_not_grant_forbidden_permissions(self):
        """Feedback must not grant the LLM permission to do forbidden actions."""
        feedback = _build_feedback_text(
            "validation_failed",
            ["missing status field", "forbidden command: pip install"],
            attempt=1,
        )
        # These phrases would only appear if feedback grants permission; prohibitions are OK
        permission_grants = [
            "you may modify",
            "allowed to run training",
            "you can execute",
            "permission to delete",
            "it is ok to overwrite",
            "feel free to edit source",
        ]
        for phrase in permission_grants:
            self.assertNotIn(phrase.lower(), feedback.lower(),
                             f"Feedback must not grant permission: '{phrase}'")

    def test_feedback_includes_safety_do_not_modify_source(self):
        feedback = _build_feedback_text("validation_failed", ["err"], attempt=1)
        self.assertIn("source code", feedback.lower())

    def test_feedback_includes_safety_do_not_run_training(self):
        feedback = _build_feedback_text("validation_failed", ["err"], attempt=1)
        self.assertIn("training", feedback.lower())


if __name__ == "__main__":
    unittest.main()

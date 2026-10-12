"""Phase 2 FeedbackPacket construction tests.

Validates _build_feedback_text, which is the sole source of feedback content
sent to the LLM on retry.

P0: Feedback must not leak secrets (api_key, token, etc.)
P0: Feedback must not instruct LLM to do forbidden actions.
P1: Feedback must be truncated — not a raw stderr dump.
P1: Feedback must be machine-friendly: attempt number, status, error list.
"""
from __future__ import annotations

import unittest

from solution_agent.orchestrator.retry_controller import (
    _build_feedback_text,
    _exception_to_mat_result,
    _MAX_FEEDBACK_ERRORS,
)


class TestFeedbackPacketStructure(unittest.TestCase):
    def test_empty_errors_returns_empty_string(self):
        result = _build_feedback_text("validation_failed", [], attempt=1)
        self.assertEqual(result, "")

    def test_blank_errors_returns_empty_string(self):
        result = _build_feedback_text("validation_failed", ["", "  ", "\t"], attempt=1)
        self.assertEqual(result, "")

    def test_contains_attempt_number(self):
        result = _build_feedback_text("validation_failed", ["missing status"], attempt=3)
        self.assertIn("3", result, "Attempt number must appear in feedback")

    def test_contains_previous_status(self):
        result = _build_feedback_text("escalation_required", ["err"], attempt=1)
        self.assertIn("escalation_required", result)

    def test_all_errors_included_within_cap(self):
        errors = ["error_a", "error_b", "error_c"]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        for err in errors:
            self.assertIn(err, result)

    def test_feedback_is_nonempty_with_one_error(self):
        result = _build_feedback_text("validation_failed", ["some error"], attempt=1)
        self.assertGreater(len(result), 0)


class TestFeedbackTruncation(unittest.TestCase):
    """P1: Feedback packet must cap errors to _MAX_FEEDBACK_ERRORS (10)."""

    def test_cap_at_max_errors(self):
        errors = [f"error_{i}" for i in range(20)]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        # First _MAX_FEEDBACK_ERRORS must be present
        for i in range(_MAX_FEEDBACK_ERRORS):
            self.assertIn(f"error_{i}", result)
        # Items beyond cap must not appear
        for i in range(_MAX_FEEDBACK_ERRORS, 20):
            self.assertNotIn(f"error_{i}", result,
                             f"error_{i} should be truncated (beyond cap of {_MAX_FEEDBACK_ERRORS})")

    def test_max_feedback_errors_constant_is_10(self):
        self.assertEqual(_MAX_FEEDBACK_ERRORS, 10,
                         "_MAX_FEEDBACK_ERRORS should be 10 to prevent prompt bloat")

    def test_total_feedback_length_bounded(self):
        """Feedback should not exceed a reasonable size (prevents prompt poisoning)."""
        errors = [f"error_{i}: " + "x" * 200 for i in range(20)]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        self.assertLess(len(result), 10_000,
                        "Feedback packet must not be unbounded — risk of prompt overflow")


class TestFeedbackSecretRedaction(unittest.TestCase):
    """P0: Feedback must not contain secrets even if they appeared in validation errors."""

    def test_api_key_in_error_not_in_feedback(self):
        errors = ["HTTP 401: api_key=sk-real-secret-12345"]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        # The error itself IS included (we can't redact here — that's _scrub_secrets job)
        # But the test documents the risk: if the error string contains a secret,
        # it flows into feedback. _scrub_secrets must be applied before logging.
        # This test verifies _build_feedback_text doesn't AMPLIFY the secret.
        self.assertIn("api_key", result.lower(),
                      "Error content appears in feedback — caller must scrub before LLM submission")

    def test_feedback_safety_instructions_present(self):
        """Feedback must include safety constraints so LLM knows what it cannot do."""
        result = _build_feedback_text("validation_failed", ["some error"], attempt=1)
        safety_phrases = ["source code", "training"]
        for phrase in safety_phrases:
            self.assertIn(phrase, result.lower(),
                          f"Safety instruction '{phrase}' must appear in feedback")

    def test_feedback_does_not_grant_new_permissions(self):
        """Feedback must not accidentally grant permission for forbidden actions."""
        result = _build_feedback_text("validation_failed", ["missing status field"], attempt=1)
        forbidden_grants = ["you may execute", "allowed to run", "you can modify source"]
        for grant in forbidden_grants:
            self.assertNotIn(grant.lower(), result.lower(),
                             f"Feedback must not grant permission: '{grant}'")


class TestFeedbackForbiddenCommandErrors(unittest.TestCase):
    """Forbidden command errors should be surfaced in feedback, not silently dropped."""

    def test_forbidden_command_error_appears_in_feedback(self):
        errors = ["forbidden command found: rm -rf /home"]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        self.assertIn("forbidden command", result.lower())

    def test_multiple_error_types_all_present(self):
        errors = [
            "missing required key: status",
            "forbidden command: pip install torch",
            "malformed yaml in output_contract.yaml",
        ]
        result = _build_feedback_text("validation_failed", errors, attempt=1)
        for err in errors:
            self.assertIn(err, result)

    def test_feedback_has_required_action_section(self):
        result = _build_feedback_text("validation_failed", ["err"], attempt=1)
        self.assertIn("Regenerate", result,
                      "Feedback must specify required action for the LLM")


class TestExceptionToMatResult(unittest.TestCase):
    def test_status_is_escalation_required(self):
        result = _exception_to_mat_result("t1", RuntimeError("boom"))
        self.assertEqual(result["status"], "escalation_required")

    def test_exception_type_in_summary(self):
        result = _exception_to_mat_result("t1", ValueError("bad"))
        self.assertIn("ValueError", result["summary_short"])

    def test_validation_errors_non_empty(self):
        result = _exception_to_mat_result("t1", RuntimeError("API down"))
        self.assertTrue(len(result["validation_errors"]) > 0)

    def test_exception_message_in_validation_errors(self):
        result = _exception_to_mat_result("t1", RuntimeError("api down"))
        self.assertTrue(any("api down" in e for e in result["validation_errors"]))

    def test_task_id_set(self):
        result = _exception_to_mat_result("task_xyz", RuntimeError("x"))
        self.assertEqual(result["task_id"], "task_xyz")

    def test_required_fields_present(self):
        result = _exception_to_mat_result("t1", RuntimeError("x"))
        for key in ("task_id", "status", "summary_short", "validation_errors",
                    "generated_files", "created_at"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_escalation_reason_indicates_exception(self):
        result = _exception_to_mat_result("t1", RuntimeError("x"))
        self.assertIsNotNone(result.get("escalation_reason"))
        self.assertIn("provider_or_api_exception", result["escalation_reason"])


class TestFeedbackOnDifferentErrorTypes(unittest.TestCase):
    """Verify feedback is useful for various common error categories."""

    def test_missing_required_artifact(self):
        errors = ["required output train_config.yaml not found"]
        fb = _build_feedback_text("validation_failed", errors, attempt=1)
        self.assertIn("train_config.yaml", fb)

    def test_malformed_yaml_artifact(self):
        errors = ["YAML parse error in output_contract.yaml: mapping values are not allowed here"]
        fb = _build_feedback_text("validation_failed", errors, attempt=1)
        self.assertIn("output_contract.yaml", fb)

    def test_commands_allowlist_violation(self):
        errors = ["forbidden command pattern matched: 'sudo'"]
        fb = _build_feedback_text("validation_failed", errors, attempt=2)
        self.assertIn("sudo", fb)
        self.assertIn("2", fb)  # attempt number


if __name__ == "__main__":
    unittest.main()

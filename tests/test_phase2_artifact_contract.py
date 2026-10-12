"""Phase 2 artifact contract tests.

P1: attempts/ directory structure must be created and preserved.
P1: attempt_001 artifacts must not be overwritten by attempt_002.
P1: final_attempt_dir must point to the correct last attempt.
P2 GAP: retry_summary.yaml does not exist — documented as known gap.
P2: latest/ update is NOT done by run_worker; it requires explicit collect().
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

import solution_agent.orchestrator.retry_controller as _retry_mod
from solution_agent.orchestrator.retry_controller import RetryController, RetryResult
from solution_agent.orchestrator.paths import result_latest_dir


def _mat_result(status: str, errors=None) -> dict:
    return {
        "task_id": "t",
        "status": status,
        "validation_errors": list(errors or []),
        "summary_short": f"status={status}",
        "generated_files": [],
        "validation_summary": {"status": "pass" if status == "materialized" else "fail",
                               "failed_validators": []},
    }


class _InstrumentedController(RetryController):
    """Runs real RetryController but intercepts run_remote_materializer."""

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
        self._call_idx = 0

    def run_attempts(self) -> RetryResult:
        original = _retry_mod.run_remote_materializer
        ctrl = self

        def fake_run(task_path, output_root, env_file, policy_path, feedback_text=""):
            idx = ctrl._call_idx
            ctrl._call_idx += 1
            run_dir = output_root / ctrl.task_id
            run_dir.mkdir(parents=True, exist_ok=True)
            # Write unique marker so we can verify preservation
            marker = f"MARKER_{idx + 1:03d}"
            (run_dir / "result_marker.txt").write_text(marker, encoding="utf-8")
            (run_dir / "generated").mkdir(exist_ok=True)
            (run_dir / "generated" / f"artifact_{idx + 1}.yaml").write_text(
                f"attempt: {idx + 1}\n", encoding="utf-8"
            )
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
# Attempt directory layout
# ---------------------------------------------------------------------------

class TestAttemptDirectoryLayout(unittest.TestCase):
    def test_attempt_001_dir_created_on_single_success(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [_mat_result("materialized")])
            ctrl.run_attempts()
            self.assertTrue((tmp / "mat_out" / "attempts" / "attempt_001").exists())

    def test_attempt_001_and_002_created_after_retry(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["err"]),
                _mat_result("materialized"),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            self.assertTrue((mat_out / "attempts" / "attempt_001").exists())
            self.assertTrue((mat_out / "attempts" / "attempt_002").exists())

    def test_no_attempt_003_when_max_is_2(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("validation_failed", ["e"]),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            self.assertFalse((mat_out / "attempts" / "attempt_003").exists(),
                             "attempt_003 must NOT exist when max_attempts=2")

    def test_attempt_dirs_use_zero_padded_names(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [_mat_result("materialized")])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            self.assertTrue((mat_out / "attempts" / "attempt_001").exists(),
                            "Directory must be attempt_001 (zero-padded), not attempt_1")


# ---------------------------------------------------------------------------
# P1: artifact preservation — no overwrite
# ---------------------------------------------------------------------------

class TestArtifactPreservation(unittest.TestCase):
    def test_marker_in_attempt_001_survives_attempt_002(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["err"]),
                _mat_result("materialized"),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            marker_1 = (mat_out / "attempts" / "attempt_001" / "t" / "result_marker.txt").read_text(encoding="utf-8")
            marker_2 = (mat_out / "attempts" / "attempt_002" / "t" / "result_marker.txt").read_text(encoding="utf-8")
            self.assertEqual(marker_1, "MARKER_001",
                             "attempt_001 content must not be overwritten by attempt_002")
            self.assertEqual(marker_2, "MARKER_002")

    def test_artifacts_per_attempt_are_independent(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["err"]),
                _mat_result("materialized"),
            ])
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            art_1 = (mat_out / "attempts" / "attempt_001" / "t" / "generated" / "artifact_1.yaml").read_text(encoding="utf-8")
            art_2 = (mat_out / "attempts" / "attempt_002" / "t" / "generated" / "artifact_2.yaml").read_text(encoding="utf-8")
            self.assertIn("attempt: 1", art_1)
            self.assertIn("attempt: 2", art_2)

    def test_attempt_001_still_exists_after_three_attempts(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            ctrl.max_attempts = 3
            ctrl.run_attempts()
            mat_out = tmp / "mat_out"
            for n in range(1, 4):
                self.assertTrue(
                    (mat_out / "attempts" / f"attempt_{n:03d}").exists(),
                    f"attempt_{n:03d} must exist",
                )


# ---------------------------------------------------------------------------
# final_attempt_dir accuracy
# ---------------------------------------------------------------------------

class TestFinalAttemptDir(unittest.TestCase):
    def test_final_dir_points_to_attempt_001_on_success(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [_mat_result("materialized")])
            result = ctrl.run_attempts()
        self.assertIn("attempt_001", str(result.final_attempt_dir))

    def test_final_dir_points_to_attempt_002_after_retry(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        self.assertIn("attempt_002", str(result.final_attempt_dir))

    def test_final_dir_includes_task_id_segment(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [_mat_result("materialized")])
            result = ctrl.run_attempts()
        self.assertIn("t", result.final_attempt_dir.parts,
                      "final_attempt_dir must end with task_id segment")


# ---------------------------------------------------------------------------
# P2 GAP: retry_summary.yaml not written (documented)
# ---------------------------------------------------------------------------

class TestRetrySummaryGap(unittest.TestCase):
    """P2 known gap: retry_summary.yaml is not written by RetryController.

    This test documents the gap. If retry_summary.yaml is eventually added,
    this test should be updated to assert it EXISTS and has required fields.
    """

    def test_retry_summary_yaml_does_not_exist_p2_gap(self):
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["err"]),
                _mat_result("materialized"),
            ])
            ctrl.run_attempts()
        retry_summary = tmp / "mat_out" / "retry_summary.yaml"
        self.assertFalse(retry_summary.exists(),
                         "KNOWN P2 GAP: retry_summary.yaml not yet written. "
                         "When implemented, update this test to assert it exists.")

    def test_retry_metadata_in_mat_result_as_workaround(self):
        """Retry metadata IS embedded in mat_result as workaround for missing retry_summary."""
        with WorkspaceTempDir() as tmp:
            ctrl = _InstrumentedController("t", tmp, [
                _mat_result("validation_failed", ["e"]),
                _mat_result("materialized"),
            ])
            result = ctrl.run_attempts()
        meta = result.mat_result.get("retry_metadata")
        self.assertIsNotNone(meta, "retry_metadata must be in mat_result as fallback")
        self.assertEqual(meta["total_attempts"], 2)
        self.assertIn("attempt_statuses", meta)


# ---------------------------------------------------------------------------
# P2: latest/ not updated by run_worker — requires explicit collect()
# ---------------------------------------------------------------------------

class TestLatestDirectoryBehavior(unittest.TestCase):
    """The latest/ directory is updated by collect(), not by run_worker().

    This is by design: run_worker produces artifacts in attempts/,
    and the orchestrator's collect() promotes to latest/.
    """

    def test_latest_dir_not_updated_by_retry_controller(self):
        task_id = f"lat_{uuid.uuid4().hex[:8]}"
        with WorkspaceTempDir() as tmp:
            ctrl = RetryController(
                task_id=task_id,
                task_path=tmp / "task.md",
                mat_out=tmp / "mat_out",
                policy_path=Path("/fake/policy.yaml"),
                env_file=Path("/fake/.env"),
                max_attempts=1,
            )
            original = _retry_mod.run_remote_materializer

            def fake_run(*args, **kwargs):
                output_root = args[1] if len(args) > 1 else kwargs["output_root"]
                run_dir = output_root / task_id
                run_dir.mkdir(parents=True, exist_ok=True)
                return {
                    "task_id": task_id, "status": "materialized",
                    "validation_errors": [], "generated_files": [],
                    "summary_short": "ok",
                    "validation_summary": {"status": "pass", "failed_validators": []},
                }

            _retry_mod.run_remote_materializer = fake_run
            try:
                ctrl.run_attempts()
            finally:
                _retry_mod.run_remote_materializer = original

        # RetryController writes to mat_out/attempts/attempt_001/task_id/
        # It does NOT write to result_latest_dir(task_id)
        # latest/ update happens only during orchestrator collect()
        latest = tmp / "mat_out" / "latest"
        self.assertFalse(latest.exists(),
                         "RetryController must NOT create a 'latest' directory; "
                         "that is collect()'s responsibility")


if __name__ == "__main__":
    unittest.main()

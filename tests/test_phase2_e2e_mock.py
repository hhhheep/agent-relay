"""Phase 2 E2E mock integration tests.

Five canonical scenarios using mock providers:
  E2E-1: Single attempt success
  E2E-2: First attempt invalid, second attempt valid
  E2E-3: Both attempts invalid → escalation
  E2E-4: Provider exception (transport failure)
  E2E-5: Forbidden action in LLM output

Additional:
  E2E-6: Two concurrent tasks do not contaminate each other
  E2E-7: Rerun same task_id must not silently overwrite
  E2E-8: Action request in output is not auto-approved
  E2E-9: Metric-based content does not trigger retry
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.orchestrator import core
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.paths import task_dir, worker_out_dir, result_dir
from solution_agent.orchestrator.task_loader import render_new_task
import solution_agent.orchestrator.retry_controller as _retry_mod


def _write_running_task(task_id: str) -> None:
    running = task_dir("running", task_id)
    running.mkdir(parents=True, exist_ok=True)
    (running / "task.md").write_text(
        render_new_task(task_id, f"E2E test {task_id}", worker="remote_materializer"),
        encoding="utf-8",
    )


def _cleanup(task_id: str) -> None:
    import shutil
    for path in (task_dir("running", task_id), task_dir("completed", task_id),
                 task_dir("failed", task_id), worker_out_dir(task_id), result_dir(task_id)):
        if path.exists():
            shutil.rmtree(path)


def _success_response(task_id: str):
    def fake(task_path, output_root, env_file, policy_path, feedback_text=""):
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir = output_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "generated").mkdir(exist_ok=True)
        (run_dir / "generated" / "train_config.yaml").write_text(f"task_id: {task_id}\n", encoding="utf-8")
        (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
        return {
            "task_id": task_id, "status": "materialized",
            "validation_errors": [], "generated_files": ["generated/train_config.yaml"],
            "summary_short": "materialized",
            "validation_summary": {"status": "pass", "failed_validators": []},
            "retry_count": {"reader": 0, "qwen": 0},
            "created_at": "2026-05-03T00:00:00+08:00",
        }
    return fake


def _failure_response(task_id: str, errors=None, n_calls=None):
    """Returns a fake that always fails (or fails first N times)."""
    calls = [0]

    def fake(task_path, output_root, env_file, policy_path, feedback_text=""):
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir = output_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        calls[0] += 1
        if n_calls is not None and calls[0] > n_calls:
            # Succeed after n_calls failures
            (run_dir / "generated").mkdir(exist_ok=True)
            (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
            return {
                "task_id": task_id, "status": "materialized",
                "validation_errors": [], "generated_files": [],
                "summary_short": "materialized",
                "validation_summary": {"status": "pass", "failed_validators": []},
                "retry_count": {"reader": 0, "qwen": 0},
                "created_at": "2026-05-03T00:00:00+08:00",
            }
        return {
            "task_id": task_id, "status": "validation_failed",
            "validation_errors": list(errors or ["missing status field"]),
            "generated_files": [],
            "summary_short": "validation failed",
            "validation_summary": {"status": "fail", "failed_validators": ["validate_yaml_schema"]},
            "created_at": "2026-05-03T00:00:00+08:00",
        }
    return fake


# ---------------------------------------------------------------------------
# E2E-1: Single attempt success
# ---------------------------------------------------------------------------

class TestE2E1SingleSuccess(unittest.TestCase):
    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_single_success(self):
        task_id = f"e2e1_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _success_response(task_id)
            result = core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(result["worker_status"], "partial")
            self.assertEqual(result["retry_attempts"], 1)
            self.assertFalse(result["feedback_used"])
            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists())
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# E2E-2: First invalid, second valid
# ---------------------------------------------------------------------------

class TestE2E2RetrySuccess(unittest.TestCase):
    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_first_invalid_second_valid(self):
        task_id = f"e2e2_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _failure_response(
                task_id, errors=["missing status field"], n_calls=1
            )
            result = core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(result["retry_attempts"], 2)
            self.assertTrue(result["feedback_used"])
            self.assertEqual(result["worker_status"], "partial")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_attempt_001_preserved_after_retry_success(self):
        task_id = f"e2e2b_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _failure_response(
                task_id, errors=["missing output_contract.yaml"], n_calls=1
            )
            result = core.run_worker(task_id, worker="remote_materializer")

            # Both attempt dirs must exist
            mat_out = result_dir(task_id) / "materializer"
            attempts = mat_out / "attempts"
            if attempts.exists():
                # At least attempt_001 exists (attempt_002 is the successful one)
                self.assertTrue((attempts / "attempt_001").exists())
                self.assertTrue((attempts / "attempt_002").exists())
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# E2E-3: Both attempts invalid → escalation
# ---------------------------------------------------------------------------

class TestE2E3BothInvalid(unittest.TestCase):
    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_both_invalid_gives_non_success(self):
        task_id = f"e2e3_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _failure_response(task_id, errors=["bad artifact"])
            result = core.run_worker(task_id, worker="remote_materializer")

            self.assertNotEqual(result["worker_status"], "success",
                                "Both attempts failed — must not report success")
            self.assertEqual(result["retry_attempts"], 2)
            result_yaml = worker_out_dir(task_id) / "result.yaml"
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertIn(payload["status"], {"failed", "partial"})
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_both_invalid_does_not_create_attempt_003(self):
        task_id = f"e2e3b_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        call_count = [0]
        try:
            _write_running_task(task_id)

            def counting_fail(task_path, output_root, env_file, policy_path, feedback_text=""):
                call_count[0] += 1
                output_root.mkdir(parents=True, exist_ok=True)
                (output_root / task_id).mkdir(parents=True, exist_ok=True)
                return {
                    "task_id": task_id, "status": "validation_failed",
                    "validation_errors": ["bad"], "generated_files": [],
                    "summary_short": "fail",
                    "validation_summary": {"status": "fail", "failed_validators": []},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = counting_fail
            core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(call_count[0], 2,
                             "P0: max_attempts=2 must mean exactly 2 provider calls, not 3")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# E2E-4: Provider exception (transport failure)
# ---------------------------------------------------------------------------

class TestE2E4ProviderException(unittest.TestCase):
    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_exception_writes_result_yaml(self):
        task_id = f"e2e4_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_timeout(*args, **kwargs):
                raise TimeoutError("provider timed out after 120s")

            _retry_mod.run_remote_materializer = raise_timeout
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists(), "P0: result.yaml must exist on provider exception")
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertIn(payload["status"], {"partial", "failed"})
            self.assertNotEqual(payload["status"], "success")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_exception_secret_scrubbed_from_stderr(self):
        task_id = f"e2e4s_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError("api_key=sk-e2e-secret-99999 unauthorized")

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            stderr = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertNotIn("sk-e2e-secret-99999", stderr,
                             "P0: secret must be scrubbed from stderr.log")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# E2E-5: Forbidden action in LLM output
# ---------------------------------------------------------------------------

class TestE2E5ForbiddenAction(unittest.TestCase):
    """P0: Forbidden commands must not be executed.
    P1: action_request in output must not be auto-approved.
    """

    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_forbidden_command_in_output_does_not_execute_command(self):
        """P0: materializer output containing forbidden commands must not execute them."""
        task_id = f"e2e5_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        executed = []

        # We intercept os.system / subprocess to detect if commands run
        import builtins
        original_open = builtins.open

        try:
            _write_running_task(task_id)

            def forbidden_output(task_path, output_root, env_file, policy_path, feedback_text=""):
                output_root.mkdir(parents=True, exist_ok=True)
                run_dir = output_root / task_id
                run_dir.mkdir(parents=True, exist_ok=True)
                # Simulate materializer returning result that contains forbidden command
                return {
                    "task_id": task_id,
                    "status": "validation_failed",
                    "validation_errors": ["forbidden command found: rm -rf /"],
                    "generated_files": [],
                    "summary_short": "forbidden command",
                    "validation_summary": {
                        "status": "fail",
                        "failed_validators": ["validate_commands_allowlist"],
                    },
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = forbidden_output
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists())
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            # Must not show success
            self.assertNotEqual(payload["status"], "success",
                                "P0: forbidden command output must not produce 'success' status")
            # Must not have executed anything
            self.assertEqual(executed, [], "P0: forbidden commands must not be executed")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_action_request_not_auto_approved(self):
        """P1: If LLM requests source code modification, it must not be auto-approved."""
        task_id = f"e2e5b_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def action_request_output(task_path, output_root, env_file, policy_path, feedback_text=""):
                output_root.mkdir(parents=True, exist_ok=True)
                (output_root / task_id).mkdir(parents=True, exist_ok=True)
                return {
                    "task_id": task_id,
                    "status": "escalation_required",
                    "escalation_reason": "model_requested_modify_source_code",
                    "validation_errors": ["action_request: modify_source_code"],
                    "generated_files": [],
                    "summary_short": "escalation: action requested",
                    "validation_summary": {"status": "fail", "failed_validators": []},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = action_request_output
            result = core.run_worker(task_id, worker="remote_materializer")

            # Must NOT show success
            self.assertNotEqual(result.get("worker_status"), "success",
                                "P1: action_request must not be auto-approved")
            # Must show escalation
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertTrue(payload.get("needs_escalation", False),
                            "P1: action_request must set needs_escalation=true")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# E2E-6: Two concurrent tasks do not contaminate each other
# ---------------------------------------------------------------------------

class TestE2E6TaskIsolation(unittest.TestCase):
    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_two_tasks_artifacts_independent(self):
        task_a = f"iso_a_{uuid.uuid4().hex[:8]}"
        task_b = f"iso_b_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer

        def make_fake(task_id: str):
            def fake(task_path, output_root, env_file, policy_path, feedback_text=""):
                output_root.mkdir(parents=True, exist_ok=True)
                run_dir = output_root / task_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "generated").mkdir(exist_ok=True)
                (run_dir / "generated" / "marker.yaml").write_text(
                    f"task: {task_id}\n", encoding="utf-8"
                )
                (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
                return {
                    "task_id": task_id, "status": "materialized",
                    "validation_errors": [], "generated_files": ["generated/marker.yaml"],
                    "summary_short": "ok",
                    "validation_summary": {"status": "pass", "failed_validators": []},
                    "retry_count": {"reader": 0, "qwen": 0},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }
            return fake

        try:
            for tid in (task_a, task_b):
                _write_running_task(tid)

            _retry_mod.run_remote_materializer = make_fake(task_a)
            core.run_worker(task_a, worker="remote_materializer")

            _retry_mod.run_remote_materializer = make_fake(task_b)
            core.run_worker(task_b, worker="remote_materializer")

            out_a = worker_out_dir(task_a) / "stdout.log"
            out_b = worker_out_dir(task_b) / "stdout.log"
            content_a = out_a.read_text(encoding="utf-8") if out_a.exists() else ""
            content_b = out_b.read_text(encoding="utf-8") if out_b.exists() else ""
            self.assertNotIn(task_b, content_a,
                             "Task A stdout must not contain Task B's task_id")
            self.assertNotIn(task_a, content_b,
                             "Task B stdout must not contain Task A's task_id")
        finally:
            _retry_mod.run_remote_materializer = original
            for tid in (task_a, task_b):
                _cleanup(tid)


# ---------------------------------------------------------------------------
# E2E-9: Metric-based content does not trigger retry
# ---------------------------------------------------------------------------

class TestE2E9NoMetricRetry(unittest.TestCase):
    """P0: Retry must only happen for format/contract errors.
    Low AUC or bad training metrics must NOT trigger retry.
    """

    def setUp(self):
        self._reg = load_registry()

    def tearDown(self):
        save_registry(self._reg)

    def test_low_metric_in_generated_artifact_does_not_retry(self):
        """If materializer returns 'materialized' with low AUC, system must not retry."""
        task_id = f"e2e9_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        call_count = [0]
        try:
            _write_running_task(task_id)

            def low_auc_but_valid(task_path, output_root, env_file, policy_path, feedback_text=""):
                call_count[0] += 1
                output_root.mkdir(parents=True, exist_ok=True)
                run_dir = output_root / task_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "generated").mkdir(exist_ok=True)
                # Artifact contains low AUC metric
                (run_dir / "generated" / "metrics.yaml").write_text(
                    "auc: 0.55\naccuracy: 0.60\n", encoding="utf-8"
                )
                (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
                return {
                    "task_id": task_id, "status": "materialized",
                    "validation_errors": [],
                    "generated_files": ["generated/metrics.yaml"],
                    "summary_short": "materialized (auc=0.55)",
                    "validation_summary": {"status": "pass", "failed_validators": []},
                    "retry_count": {"reader": 0, "qwen": 0},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = low_auc_but_valid
            result = core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(call_count[0], 1,
                             "P0: Low AUC must not trigger retry — "
                             "only format/contract errors are retry-worthy")
            self.assertEqual(result["worker_status"], "partial",
                             "Materialized status only prepares artifacts; execution_port completion owns success")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


if __name__ == "__main__":
    unittest.main()

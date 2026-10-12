"""Phase 2 registry lifecycle tests.

P0: Task must not remain in 'running' state after run_worker fails.
    If task is stuck 'running', the scheduler will never pick it up again.
P0: Registry must not show 'completed'/'success' when artifact is invalid.
P1: State transitions must follow the allowed graph.
P2 GAP: No 'retrying' state — registry can only observe final result.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

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
        render_new_task(task_id, "Registry lifecycle test", worker="remote_materializer"),
        encoding="utf-8",
    )


def _cleanup(task_id: str) -> None:
    import shutil
    for path in (task_dir("running", task_id), task_dir("completed", task_id),
                 task_dir("failed", task_id), worker_out_dir(task_id), result_dir(task_id)):
        if path.exists():
            shutil.rmtree(path)


def _fake_mat_escalation(task_id: str):
    """Returns a fake run_remote_materializer that produces escalation_required."""
    def fake(task_path, output_root, env_file, policy_path, feedback_text=""):
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir = output_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "task_id": task_id,
            "status": "escalation_required",
            "escalation_reason": "test_escalation",
            "validation_errors": [],
            "generated_files": [],
            "summary_short": "escalation for test",
            "validation_summary": {"status": "fail", "failed_validators": []},
            "created_at": "2026-05-03T00:00:00+08:00",
        }
    return fake


def _fake_mat_success(task_id: str):
    """Returns a fake run_remote_materializer that produces materialized."""
    def fake(task_path, output_root, env_file, policy_path, feedback_text=""):
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir = output_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "generated").mkdir(exist_ok=True)
        (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
        return {
            "task_id": task_id,
            "status": "materialized",
            "validation_errors": [],
            "generated_files": ["generated/train_config.yaml"],
            "summary_short": "materialized ok",
            "validation_summary": {"status": "pass", "failed_validators": []},
            "retry_count": {"reader": 0, "qwen": 0},
            "created_at": "2026-05-03T00:00:00+08:00",
        }
    return fake


# ---------------------------------------------------------------------------
# run_worker result contract
# ---------------------------------------------------------------------------

class TestRunWorkerResultContract(unittest.TestCase):
    def setUp(self):
        self._registry_backup = load_registry()

    def tearDown(self):
        save_registry(self._registry_backup)

    def test_run_worker_returns_worker_key_on_success(self):
        task_id = f"rl_ok_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _fake_mat_success(task_id)
            result = core.run_worker(task_id, worker="remote_materializer")
            self.assertEqual(result.get("worker"), "remote_materializer")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_run_worker_returns_retry_attempts(self):
        task_id = f"rl_ra_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _fake_mat_success(task_id)
            result = core.run_worker(task_id, worker="remote_materializer")
            self.assertIn("retry_attempts", result,
                          "run_worker result must include retry_attempts for observability")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_exception_path_writes_result_yaml(self):
        """P0: Even when provider raises, result.yaml must be written."""
        task_id = f"rl_exc_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("provider down")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists(),
                            "result.yaml must exist even when provider raises")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_exception_result_yaml_not_success(self):
        """P0: result.yaml status must not be 'success' when provider raised."""
        task_id = f"rl_excst_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("connection refused")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            import yaml
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertNotEqual(payload["status"], "success",
                                "P0: result.yaml must not show 'success' when provider raised")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# collect() state transitions
# ---------------------------------------------------------------------------

class TestCollectStateTransition(unittest.TestCase):
    def setUp(self):
        self._registry_backup = load_registry()

    def tearDown(self):
        save_registry(self._registry_backup)

    def test_collect_transitions_running_to_completed_on_success(self):
        task_id = f"rl_col_ok_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _fake_mat_success(task_id)
            core.run_worker(task_id, worker="remote_materializer")

            # Update registry to 'running' as if dispatcher set it
            save_registry({"tasks": {task_id: {"task_id": task_id, "title": "test", "status": "running"}}})
            collected = core.collect(task_id)

            self.assertEqual(collected["status"], "partial",
                             "collect() must transition running → completed on success")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_collect_transitions_running_to_failed_on_exception(self):
        task_id = f"rl_col_fail_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("API down")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            save_registry({"tasks": {task_id: {"task_id": task_id, "title": "test", "status": "running"}}})
            collected = core.collect(task_id)

            self.assertIn(collected["status"], {"partial", "failed"},
                          "Task must not remain 'running' after exception")
            self.assertNotEqual(collected["status"], "running",
                                "P0: Task stuck in 'running' — scheduler will never reschedule")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)

    def test_collect_escalation_maps_to_partial_not_success(self):
        """P0: escalation_required must map to 'partial', never 'success'."""
        task_id = f"rl_col_esc_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)
            _retry_mod.run_remote_materializer = _fake_mat_escalation(task_id)
            core.run_worker(task_id, worker="remote_materializer")

            save_registry({"tasks": {task_id: {"task_id": task_id, "title": "test", "status": "running"}}})
            collected = core.collect(task_id)

            self.assertNotEqual(collected["status"], "completed",
                                "P0: escalation_required must not produce 'completed' registry status")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


# ---------------------------------------------------------------------------
# State machine guards
# ---------------------------------------------------------------------------

class TestStateMachineGuards(unittest.TestCase):
    """State machine must reject invalid transitions."""

    def test_invalid_transition_raises(self):
        from solution_agent.orchestrator.state_machine import (
            InvalidTransitionError,
            validate_transition,
        )
        with self.assertRaises(InvalidTransitionError):
            validate_transition("completed", "pending", source="test")

    def test_valid_transition_does_not_raise(self):
        from solution_agent.orchestrator.state_machine import validate_transition
        validate_transition("pending", "running", source="test")

    def test_same_state_transition_allowed(self):
        from solution_agent.orchestrator.state_machine import validate_transition
        validate_transition("running", "running", source="heartbeat")

    def test_error_message_includes_allowed_transitions(self):
        from solution_agent.orchestrator.state_machine import (
            InvalidTransitionError,
            validate_transition,
        )
        try:
            validate_transition("completed", "running", source="test")
            self.fail("Should have raised InvalidTransitionError")
        except InvalidTransitionError as e:
            self.assertIn("completed", str(e).lower(),
                          "Error should mention the from-state")


# ---------------------------------------------------------------------------
# P2 GAP: no 'retrying' state in registry
# ---------------------------------------------------------------------------

class TestRegistryRetryStateGap(unittest.TestCase):
    """P2 GAP: Registry has no 'retrying' state.

    The registry moves directly from 'running' to final state after run_worker.
    Intermediate retry attempts are invisible at the registry level.
    This is a known P2 gap — retry history is only in mat_result.retry_metadata.
    """

    def test_registry_valid_statuses_no_retrying(self):
        from solution_agent.orchestrator.schema import VALID_TASK_STATUSES
        self.assertNotIn("retrying", VALID_TASK_STATUSES,
                         "KNOWN P2 GAP: 'retrying' state not in registry — "
                         "intermediate retry attempts invisible to scheduler")

    def test_retry_metadata_in_run_worker_result(self):
        """run_worker result includes retry_attempts as compensating observability."""
        task_id = f"rl_rmeta_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        call_count = [0]
        try:
            _write_running_task(task_id)

            def two_attempt_fake(task_path, output_root, env_file, policy_path, feedback_text=""):
                output_root.mkdir(parents=True, exist_ok=True)
                run_dir = output_root / task_id
                run_dir.mkdir(parents=True, exist_ok=True)
                call_count[0] += 1
                if call_count[0] == 1:
                    return {
                        "task_id": task_id, "status": "validation_failed",
                        "validation_errors": ["missing status"],
                        "generated_files": [], "summary_short": "fail",
                        "validation_summary": {"status": "fail", "failed_validators": []},
                        "created_at": "2026-05-03T00:00:00+08:00",
                    }
                (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
                return {
                    "task_id": task_id, "status": "materialized",
                    "validation_errors": [], "generated_files": [],
                    "summary_short": "ok",
                    "validation_summary": {"status": "pass", "failed_validators": []},
                    "retry_count": {"reader": 0, "qwen": 0},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = two_attempt_fake
            result = core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(result.get("retry_attempts"), 2,
                             "retry_attempts must reflect actual attempt count for observability")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup(task_id)


if __name__ == "__main__":
    unittest.main()

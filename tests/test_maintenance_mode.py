import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core


class MaintenanceModeTests(unittest.TestCase):
    def test_maintenance_mode_round_trip_defaults_to_read_only_actions(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "maintenance_mode.json"
            with patch("solution_agent.orchestrator.core.maintenance_mode_path", return_value=path):
                enabled = core.set_maintenance_mode(True, reason="test maintenance")
                loaded = core.load_maintenance_mode()
                cleared = core.clear_maintenance_mode()

        self.assertTrue(enabled["enabled"])
        self.assertEqual(loaded["reason"], "test maintenance")
        self.assertFalse(loaded["allow_action_requests"])
        self.assertFalse(loaded["allow_scheduler_launch"])
        self.assertFalse(cleared["enabled"])
        self.assertTrue(cleared["allow_action_requests"])
        self.assertTrue(cleared["allow_scheduler_launch"])

    def test_schedule_remote_refuses_launch_when_maintenance_blocks_scheduler(self):
        mode = {
            "enabled": True,
            "reason": "window",
            "allow_scheduler_launch": False,
            "allow_action_requests": False,
            "allow_dashboard_read": True,
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value=mode), patch(
            "solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile") as reconcile:
            result = core.schedule_remote(
                host="host",
                remote_root="/tmp/runs",
                remote_workdir="/tmp/work",
                dry_run=False,
            )

        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "maintenance_mode_disallows_scheduler_launch")
        self.assertEqual(result["started"], [])
        reconcile.assert_not_called()

    def test_schedule_remote_skips_tasks_for_other_workers(self):
        tasks = {
            "small_worker_task": SimpleNamespace(
                task_id="small_worker_task",
                title="Small worker task",
                priority="normal",
                worker="small_task_worker",
                raw_front_matter={"resource_hint": "cpu", "worker": "small_task_worker"},
            ),
            "own_agent_task": SimpleNamespace(
                task_id="own_agent_task",
                title="Own agent task",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "cpu", "worker": "materialized_execution"},
            ),
        }

        def fake_find_task(task_id):
            return "pending", Path(f"/tmp/{task_id}/task.md")

        def fake_load_task(path):
            return tasks[Path(path).parent.name]

        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value={"enabled": False}), patch(
            "solution_agent.orchestrator.core.load_drain_mode", return_value={"enabled": False}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={}), patch(
            "solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}
        ), patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}), patch(
            "solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}
        ), patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False), patch(
            "solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]
        ), patch("solution_agent.orchestrator.core.queue", return_value=[
            {"task_id": "small_worker_task", "ready": True},
            {"task_id": "own_agent_task", "ready": True},
        ]), patch("solution_agent.orchestrator.core.find_task", side_effect=fake_find_task), patch(
            "solution_agent.orchestrator.core.load_task", side_effect=fake_load_task
        ):
            result = core.schedule_remote(
                host="host",
                remote_root="/tmp/runs",
                remote_workdir="/tmp/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_cpu=2,
                max_running_gpu=0,
                dry_run=True,
            )

        self.assertEqual([item["task_id"] for item in result["plan"]], ["own_agent_task"])
        self.assertIn(
            {"task_id": "small_worker_task", "reason": "worker_mismatch", "task_worker": "small_task_worker", "selected_worker": "materialized_execution"},
            result["skipped"],
        )

    def test_schedule_remote_skips_target_workplace_mismatch(self):
        tasks = {
            "targeted_task": SimpleNamespace(
                task_id="targeted_task",
                title="Targeted task",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={
                    "resource_hint": "cpu",
                    "worker": "materialized_execution",
                    "target_workplace": {
                        "host": "host",
                        "remote_root": "/tmp/runs-b",
                        "remote_workdir": "/tmp/work",
                    },
                },
            ),
            "free_task": SimpleNamespace(
                task_id="free_task",
                title="Free task",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "cpu", "worker": "materialized_execution"},
            ),
        }

        def fake_find_task(task_id):
            return "pending", Path(f"/tmp/{task_id}/task.md")

        def fake_load_task(path):
            return tasks[Path(path).parent.name]

        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value={"enabled": False}), patch(
            "solution_agent.orchestrator.core.load_drain_mode", return_value={"enabled": False}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={}), patch(
            "solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}
        ), patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}), patch(
            "solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}
        ), patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False), patch(
            "solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]
        ), patch("solution_agent.orchestrator.core.queue", return_value=[
            {"task_id": "targeted_task", "ready": True},
            {"task_id": "free_task", "ready": True},
        ]), patch("solution_agent.orchestrator.core.find_task", side_effect=fake_find_task), patch(
            "solution_agent.orchestrator.core.load_task", side_effect=fake_load_task
        ):
            result = core.schedule_remote(
                host="host",
                remote_root="/tmp/runs-a",
                remote_workdir="/tmp/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_cpu=2,
                max_running_gpu=0,
                dry_run=True,
            )

        self.assertEqual([item["task_id"] for item in result["plan"]], ["free_task"])
        mismatch = next(item for item in result["skipped"] if item["task_id"] == "targeted_task")
        self.assertEqual(mismatch["reason"], "target_workplace_mismatch")
        self.assertEqual(mismatch["mismatches"][0]["field"], "remote_root")

    def test_schedule_remote_counts_gpu_usage_across_same_host_workplaces(self):
        tasks = {
            "running_on_root_a": SimpleNamespace(
                task_id="running_on_root_a",
                title="Running on root A",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "gpu", "worker": "materialized_execution"},
            ),
            "new_on_root_b": SimpleNamespace(
                task_id="new_on_root_b",
                title="New on root B",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "gpu", "worker": "materialized_execution"},
            ),
        }
        registry = {
            "tasks": {
                "running_on_root_a": {
                    "status": "running",
                    "latest_run_id": "run-a",
                    "remote": {
                        "host": "host",
                        "remote_root": "/tmp/runs-a",
                        "gpu_candidates": "0",
                    },
                    "runs": [
                        {
                            "run_id": "run-a",
                            "status": "running",
                            "gpu": {"requested_nvidia_smi_ids": ["0"]},
                        }
                    ],
                }
            }
        }

        def fake_find_task(task_id):
            return "pending", Path(f"/tmp/{task_id}/task.md")

        def fake_load_task(path):
            return tasks[Path(path).parent.name]

        gpu_snapshot = {
            "gpus": [
                {"index": "0", "uuid": "GPU-0", "free_gb": 80.0, "utilization_pct": 0, "has_external_compute_apps": False},
                {"index": "1", "uuid": "GPU-1", "free_gb": 80.0, "utilization_pct": 0, "has_external_compute_apps": False},
            ]
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value={"enabled": False}), patch(
            "solution_agent.orchestrator.core.load_drain_mode", return_value={"enabled": False}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={}), patch(
            "solution_agent.orchestrator.core.load_registry", return_value=registry
        ), patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value=gpu_snapshot), patch(
            "solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False
        ), patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]), patch(
            "solution_agent.orchestrator.core.queue", return_value=[{"task_id": "new_on_root_b", "ready": True}]
        ), patch("solution_agent.orchestrator.core.find_task", side_effect=fake_find_task), patch(
            "solution_agent.orchestrator.core.load_task", side_effect=fake_load_task
        ):
            result = core.schedule_remote(
                host="host",
                remote_root="/tmp/runs-b",
                remote_workdir="/tmp/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=2,
                max_running_gpu=2,
                max_running_cpu=0,
                min_free_gpu_mem_gb=10,
                allowed_gpu_ids=["0", "1"],
                max_tasks_per_gpu=1,
                dry_run=True,
            )

        self.assertEqual(result["running_gpu_counts"], {"0": 1})
        self.assertEqual(result["plan"][0]["task_id"], "new_on_root_b")
        self.assertEqual(result["plan"][0]["selected_gpu_id"], "1")

    def test_schedule_remote_does_not_count_gpu_usage_from_other_hosts(self):
        tasks = {
            "running_on_other_host": SimpleNamespace(
                task_id="running_on_other_host",
                title="Running on other host",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "gpu", "worker": "materialized_execution"},
            ),
            "new_on_host_b": SimpleNamespace(
                task_id="new_on_host_b",
                title="New on host B",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "gpu", "worker": "materialized_execution"},
            ),
        }
        registry = {
            "tasks": {
                "running_on_other_host": {
                    "status": "running",
                    "latest_run_id": "run-a",
                    "remote": {
                        "host": "host-a",
                        "remote_root": "/tmp/runs-a",
                        "gpu_candidates": "0",
                    },
                    "runs": [
                        {
                            "run_id": "run-a",
                            "status": "running",
                            "gpu": {"requested_nvidia_smi_ids": ["0"]},
                        }
                    ],
                }
            }
        }

        def fake_find_task(task_id):
            return "pending", Path(f"/tmp/{task_id}/task.md")

        def fake_load_task(path):
            return tasks[Path(path).parent.name]

        gpu_snapshot = {
            "gpus": [
                {"index": "0", "uuid": "GPU-0", "free_gb": 80.0, "utilization_pct": 0, "has_external_compute_apps": False},
                {"index": "1", "uuid": "GPU-1", "free_gb": 80.0, "utilization_pct": 0, "has_external_compute_apps": False},
            ]
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value={"enabled": False}), patch(
            "solution_agent.orchestrator.core.load_drain_mode", return_value={"enabled": False}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={}), patch(
            "solution_agent.orchestrator.core.load_registry", return_value=registry
        ), patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value=gpu_snapshot), patch(
            "solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False
        ), patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]), patch(
            "solution_agent.orchestrator.core.queue", return_value=[{"task_id": "new_on_host_b", "ready": True}]
        ), patch("solution_agent.orchestrator.core.find_task", side_effect=fake_find_task), patch(
            "solution_agent.orchestrator.core.load_task", side_effect=fake_load_task
        ):
            result = core.schedule_remote(
                host="host-b",
                remote_root="/tmp/runs-b",
                remote_workdir="/tmp/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=2,
                max_running_gpu=2,
                max_running_cpu=0,
                min_free_gpu_mem_gb=10,
                allowed_gpu_ids=["0", "1"],
                max_tasks_per_gpu=1,
                dry_run=True,
            )

        self.assertEqual(result["running_gpu_counts"], {})
        self.assertEqual(result["plan"][0]["selected_gpu_id"], "0")

    def test_schedule_remote_skips_untracked_same_user_gpu_process(self):
        tasks = {
            "new_gpu_task": SimpleNamespace(
                task_id="new_gpu_task",
                title="New GPU task",
                priority="normal",
                worker="materialized_execution",
                raw_front_matter={"resource_hint": "gpu", "worker": "materialized_execution"},
            ),
        }

        def fake_find_task(task_id):
            return "pending", Path(f"/tmp/{task_id}/task.md")

        def fake_load_task(path):
            return tasks[Path(path).parent.name]

        gpu_snapshot = {
            "gpus": [
                {
                    "index": "0",
                    "uuid": "GPU-0",
                    "free_gb": 40.0,
                    "utilization_pct": 70,
                    "has_external_compute_apps": False,
                    "own_compute_app_count": 1,
                    "external_compute_app_count": 0,
                },
                {
                    "index": "1",
                    "uuid": "GPU-1",
                    "free_gb": 30.0,
                    "utilization_pct": 0,
                    "has_external_compute_apps": False,
                    "own_compute_app_count": 0,
                    "external_compute_app_count": 0,
                },
            ]
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value={"enabled": False}), patch(
            "solution_agent.orchestrator.core.load_drain_mode", return_value={"enabled": False}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={}), patch(
            "solution_agent.orchestrator.core.load_registry", return_value={"tasks": {}}
        ), patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value=gpu_snapshot), patch(
            "solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False
        ), patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]), patch(
            "solution_agent.orchestrator.core.queue", return_value=[{"task_id": "new_gpu_task", "ready": True}]
        ), patch("solution_agent.orchestrator.core.find_task", side_effect=fake_find_task), patch(
            "solution_agent.orchestrator.core.load_task", side_effect=fake_load_task
        ):
            result = core.schedule_remote(
                host="host-b",
                remote_root="/tmp/runs-b",
                remote_workdir="/tmp/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=2,
                max_running_gpu=2,
                max_running_cpu=0,
                min_free_gpu_mem_gb=10,
                allowed_gpu_ids=["0", "1"],
                max_tasks_per_gpu=1,
                dry_run=True,
            )

        self.assertEqual(result["plan"][0]["selected_gpu_id"], "1")

    def test_startup_guard_fails_closed_on_maintenance(self):
        mode = {
            "enabled": True,
            "reason": "window",
            "allow_scheduler_launch": False,
            "allow_action_requests": False,
            "allow_dashboard_read": True,
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value=mode), patch(
            "solution_agent.orchestrator.core.registry_hygiene", return_value={"issue_count": 0}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"candidate_actions": []}), patch(
            "solution_agent.orchestrator.core.input_binding_audit", return_value={"issue_count": 0, "issues": []}
        ), patch("solution_agent.orchestrator.core.partial_pending_audit", return_value={"unknown_count": 0, "unknown": []}), patch(
            "solution_agent.orchestrator.core.schedule_remote", return_value={"started": [], "plan": []}
        ), patch("solution_agent.orchestrator.core.state_dir") as state_dir:
            with WorkspaceTempDir() as tmp:
                state_dir.return_value = tmp
                result = core.scheduler_startup_guard(
                    host="host",
                    remote_root="/tmp/runs",
                    remote_workdir="/tmp/work",
                    max_starts=1,
                    max_running_total=1,
                    max_running_gpu=1,
                    max_running_cpu=1,
                    min_free_gpu_mem_gb=10,
                    allowed_gpu_ids=["0"],
                    max_tasks_per_gpu=1,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["code"], "maintenance_mode_disallows_scheduler_launch")

    def test_startup_guard_warns_on_acknowledged_partial_pending(self):
        mode = {
            "enabled": False,
            "reason": "",
            "allow_scheduler_launch": True,
            "allow_action_requests": True,
            "allow_dashboard_read": True,
        }
        ack_status = {
            "ok": True,
            "status": "yellow",
            "unknown_count": 1,
            "acknowledged_count": 1,
            "unacknowledged_count": 0,
            "message": "1 acknowledged legacy partial->pending unknown transitions",
        }
        with patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value=mode), patch(
            "solution_agent.orchestrator.core.registry_hygiene", return_value={"issue_count": 0}
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"candidate_actions": []}), patch(
            "solution_agent.orchestrator.core.input_binding_audit", return_value={"issue_count": 0, "issues": []}
        ), patch("solution_agent.orchestrator.core.partial_pending_audit", return_value={"unknown_count": 1, "unknown": [{}]}), patch(
            "solution_agent.orchestrator.core.partial_pending_audit_ack_status", return_value=ack_status
        ), patch("solution_agent.orchestrator.core.schedule_remote", return_value={"started": [], "plan": []}), patch(
            "solution_agent.orchestrator.core.state_dir"
        ) as state_dir:
            with WorkspaceTempDir() as tmp:
                state_dir.return_value = tmp
                result = core.scheduler_startup_guard(
                    host="host",
                    remote_root="/tmp/runs",
                    remote_workdir="/tmp/work",
                    max_starts=1,
                    max_running_total=1,
                    max_running_gpu=1,
                    max_running_cpu=1,
                    min_free_gpu_mem_gb=10,
                    allowed_gpu_ids=["0"],
                    max_tasks_per_gpu=1,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "yellow")
        self.assertEqual(result["warnings"][0]["code"], "partial_to_pending_acknowledged_legacy")


if __name__ == "__main__":
    unittest.main()

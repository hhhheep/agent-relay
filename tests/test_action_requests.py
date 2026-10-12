import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core
from solution_agent.orchestrator.registry import load_registry, save_registry


class ActionRequestTests(unittest.TestCase):
    def _patch_paths(self, tmp: Path):
        return patch.multiple(
            "solution_agent.orchestrator.core",
            state_dir=lambda: tmp / "state",
            registry_path=lambda: tmp / "registry.json",
            root=lambda: tmp,
        )

    def test_create_and_list_action_request(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            created = core.create_action_request("dependency_reconcile_apply_safe", reason="test")
            listed = core.list_action_requests(status="pending")

        self.assertTrue(created["created"])
        self.assertEqual(listed["counts"]["pending"], 1)
        self.assertEqual(listed["requests"][0]["action_id"], created["action_id"])

    def test_create_action_request_is_idempotent(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            first = core.create_action_request(
                "artifact_followup",
                task_id="exp_partial",
                payload={"kind": "request_probe", "missing_or_needed": "residual_report.csv"},
                reason="test",
            )
            second = core.create_action_request(
                "artifact_followup",
                task_id="exp_partial",
                payload={"kind": "request_probe", "missing_or_needed": "residual_report.csv"},
                reason="test",
            )
            listed = core.list_action_requests(status="pending")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["reason"], "already_exists")
        self.assertEqual(listed["counts"]["pending"], 1)
        self.assertEqual(listed["requests"][0]["action_id"], first["action_id"])

    def test_apply_safe_processes_safe_request(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ), patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"ok": True}) as reconcile:
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            core.create_action_request("dependency_reconcile_apply_safe", reason="test")
            result = core.process_action_requests(apply_safe=True)
            listed = core.list_action_requests()

        self.assertEqual(result["processed"][0]["status"], "done")
        self.assertEqual(listed["counts"]["done"], 1)
        reconcile.assert_called_with(apply_safe=True)

    def test_missing_capability_failure_action_request_is_idempotent(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_missing_cap": {"task_id": "exp_missing_cap", "status": "partial"}}}, path=tmp / "registry.json")
            first = core.create_failure_action_request(
                "exp_missing_cap",
                run_id="run1",
                failure_type="missing_capability",
                recommended_action={"type": "create_action_request", "message": "need wrapper"},
                result_path="results/exp_missing_cap/run1/result.yaml",
                reason="need wrapper",
            )
            second = core.create_failure_action_request(
                "exp_missing_cap",
                run_id="run1",
                failure_type="missing_capability",
                recommended_action={"type": "create_action_request", "message": "need wrapper"},
                result_path="results/exp_missing_cap/run1/result.yaml",
                reason="need wrapper",
            )
            listed = core.list_action_requests(status="pending")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["reason"], "already_exists")
        self.assertEqual(listed["counts"]["pending"], 1)
        self.assertEqual(listed["requests"][0]["action_type"], "missing_capability_followup")
        self.assertEqual(listed["requests"][0]["payload"]["failure_type"], "missing_capability")
        self.assertEqual(listed["requests"][0]["payload"]["run_id"], "run1")

    def test_artifact_action_requests_are_imported_and_idempotent(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            result_path = tmp / "results" / "exp_partial" / "latest"
            artifacts = result_path / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "action_requests.json").write_text(
                """
{
  "task_id": "exp_partial",
  "action_requests": [
    {
      "kind": "request_bounded_probe",
      "missing_or_needed": "context_size_sensitivity.csv",
      "why_needed": "Needed to verify whether the leakage claim survives crop/context changes.",
      "next_owner": "scheduler",
      "blocks": "claim",
      "blocks_claim_not_execution": true,
      "blocks_execution": false,
      "suggested_next_step": "Run the bounded context-size sensitivity probe."
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            first = core.create_artifact_action_requests("exp_partial", run_id="run1", result_path=result_path)
            second = core.create_artifact_action_requests("exp_partial", run_id="run1", result_path=result_path)
            listed = core.list_action_requests(status="pending")
            registry_exists = (tmp / "state" / "action_requests" / "action_request_registry.jsonl").exists()
            status_text = (tmp / "state" / "action_requests" / "action_request_status.yaml").read_text(encoding="utf-8")

        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(first["created"][0]["missing_or_needed"], "context_size_sensitivity.csv")
        self.assertEqual(len(second["existing"]), 1)
        self.assertEqual(listed["counts"]["pending"], 1)
        request = listed["requests"][0]
        self.assertEqual(request["action_type"], "artifact_followup")
        self.assertEqual(request["payload"]["kind"], "request_bounded_probe")
        self.assertEqual(request["payload"]["source_run_id"], "run1")
        self.assertEqual(request["safe_level"], "safe_after_revalidation")
        self.assertEqual(request["lifecycle_status"], "open")
        self.assertEqual(request["next_owner"], "scheduler")
        self.assertTrue(registry_exists)
        self.assertIn("context_size_sensitivity.csv", status_text)

    def test_artifact_action_request_codex_owner_is_diagnostic(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            result_path = tmp / "results" / "exp_partial" / "latest"
            artifacts = result_path / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "action_requests.json").write_text(
                """
{
  "action_requests": [
    {
      "kind": "request_codex_review",
      "missing_or_needed": "maskout_clean_token_probe.csv",
      "why_needed": "The follow-up requires source-code review before execution.",
      "next_owner": "codex",
      "blocks": "execution",
      "blocks_claim_not_execution": false,
      "blocks_execution": true,
      "suggested_next_step": "Review and implement the bounded probe wrapper."
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            created = core.create_artifact_action_requests("exp_partial", run_id="run1", result_path=result_path)
            listed = core.list_action_requests(status="pending")

        self.assertEqual(len(created["created"]), 1)
        self.assertEqual(listed["requests"][0]["safe_level"], "diagnostic")
        self.assertTrue(listed["requests"][0]["payload"]["blocks_execution"])

    def test_artifact_action_request_normalizes_l72_missing_capability_shape(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"b14_l72": {"task_id": "b14_l72", "status": "partial"}}}, path=tmp / "registry.json")
            result_path = tmp / "results" / "b14_l72" / "latest"
            artifacts = result_path / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "action_requests.json").write_text(
                """
[
  {
    "kind": "missing_capability",
    "message": "Missing optional --large-image-mode CLI flag from L71.",
    "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics."
  }
]
""".strip(),
                encoding="utf-8",
            )

            result = core.create_artifact_action_requests("b14_l72", run_id="run_l72", result_path=result_path)
            listed = core.list_action_requests(status="pending")

        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["created"]), 1)
        created = result["created"][0]
        self.assertEqual(created["missing_or_needed"], "Missing optional --large-image-mode CLI flag from L71.")
        request = listed["requests"][0]
        self.assertEqual(request["payload"]["kind"], "missing_capability")
        self.assertEqual(request["payload"]["next_owner"], "codex")
        self.assertEqual(request["payload"]["blocks"], "execution")
        self.assertTrue(request["payload"]["blocks_execution"])
        self.assertFalse(request["payload"]["blocks_claim_not_execution"])

    def test_artifact_action_request_quality_gate_rejects_generic_context(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            result_path = tmp / "results" / "exp_partial" / "latest"
            artifacts = result_path / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "action_requests.json").write_text(
                """
{
  "action_requests": [
    {
      "kind": "request_context",
      "missing_or_needed": "context",
      "why_needed": "need more context",
      "next_owner": "scheduler",
      "blocks": "claim",
      "blocks_claim_not_execution": true,
      "blocks_execution": false,
      "suggested_next_step": "Review."
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            result = core.create_artifact_action_requests("exp_partial", run_id="run1", result_path=result_path)
            listed = core.list_action_requests(status="pending")

        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"][0]["reason"], "invalid_action_request")
        self.assertIn("why_needed_too_generic", result["skipped"][0]["errors"])
        self.assertEqual(listed["counts"]["pending"], 0)

    def test_artifact_action_request_rejects_unknown_unlock_target(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            result_path = tmp / "results" / "exp_partial" / "latest"
            artifacts = result_path / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "action_requests.json").write_text(
                """
{
  "action_requests": [
    {
      "kind": "request_bounded_probe",
      "missing_or_needed": "detector_calibration_metrics.json",
      "why_needed": "Needed to decide whether detector_validated can be true.",
      "next_owner": "scheduler",
      "blocks": "claim",
      "blocks_claim_not_execution": true,
      "blocks_execution": false,
      "suggested_next_step": "Run the bounded detector calibration task.",
      "unlocks": ["missing_downstream_task"]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            result = core.create_artifact_action_requests("exp_partial", run_id="run1", result_path=result_path)
            listed = core.list_action_requests(status="pending")

        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"][0]["reason"], "invalid_action_request")
        self.assertIn("unlock_target_unknown:missing_downstream_task", result["skipped"][0]["errors"])
        self.assertEqual(listed["counts"]["pending"], 0)

    def test_action_request_resolution_event_records_unlocks(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            created = core.create_action_request(
                "artifact_followup",
                task_id="exp_partial",
                payload={
                    "kind": "request_artifact",
                    "missing_or_needed": "feature_cache_inventory.json",
                    "why_needed": "Needed to unblock the spatial inventory node.",
                    "next_owner": "scheduler",
                    "blocks": "claim",
                    "blocks_claim_not_execution": True,
                    "blocks_execution": False,
                    "suggested_next_step": "Run the bounded inventory task.",
                    "unlocks": ["spatial_node_type_audit_probe"],
                },
                reason="feature cache inventory",
                safe_level="diagnostic",
                requires_scheduler_validation=False,
            )
            result = core.process_action_requests(apply_selected=created["action_id"])
            listed = core.list_action_requests()
            event_text = (tmp / "state" / "action_requests" / "action_resolution_event.jsonl").read_text(encoding="utf-8")
            status_text = (tmp / "state" / "action_requests" / "action_request_status.yaml").read_text(encoding="utf-8")

        self.assertEqual(result["processed"][0]["status"], "done")
        self.assertEqual(listed["counts"]["done"], 1)
        done_request = listed["requests"][0]
        self.assertEqual(done_request["lifecycle_status"], "resolved")
        self.assertEqual(done_request["unlocks"], ["spatial_node_type_audit_probe"])
        self.assertIn("spatial_node_type_audit_probe", event_text)
        self.assertIn("done: 1", status_text)

    def test_artifact_followup_processes_as_noted(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            core.create_action_request(
                "artifact_followup",
                task_id="exp_partial",
                payload={"kind": "request_bounded_probe", "missing_or_needed": "followup.csv"},
                reason="test artifact followup",
                safe_level="diagnostic",
                requires_scheduler_validation=False,
            )
            result = core.process_action_requests(apply_safe=True)
            listed = core.list_action_requests()

        self.assertEqual(result["processed"][0]["status"], "done")
        self.assertEqual(result["processed"][0]["response"]["result"]["noted"], True)
        self.assertEqual(listed["counts"]["done"], 1)

    def test_missing_capability_followup_processes_as_diagnostic(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_missing_cap": {"task_id": "exp_missing_cap", "status": "partial"}}}, path=tmp / "registry.json")
            created = core.create_failure_action_request(
                "exp_missing_cap",
                run_id="run1",
                failure_type="missing_capability",
                recommended_action={"type": "create_action_request", "message": "need wrapper"},
                result_path="results/exp_missing_cap/run1/result.yaml",
                reason="need wrapper",
            )
            result = core.process_action_requests(apply_safe=True)
            listed = core.list_action_requests()

        self.assertTrue(created["created"])
        self.assertEqual(result["processed"][0]["status"], "done")
        self.assertEqual(result["processed"][0]["response"]["result"]["noted"], True)
        self.assertEqual(listed["counts"]["done"], 1)

    def test_apply_safe_rejects_dangerous_request(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ):
            save_registry({"tasks": {"exp_partial": {"task_id": "exp_partial", "status": "partial"}}}, path=tmp / "registry.json")
            core.create_action_request(
                "requeue_partial",
                task_id="exp_partial",
                reason="test",
                safe_level="dangerous_requires_explicit_approval",
                expected_task_status="partial",
            )
            result = core.process_action_requests(apply_safe=True)
            listed = core.list_action_requests()
            registry = load_registry(tmp / "registry.json")

        self.assertEqual(result["processed"][0]["status"], "rejected")
        self.assertEqual(listed["counts"]["rejected"], 1)
        self.assertEqual(registry["tasks"]["exp_partial"]["status"], "partial")

    def test_maintenance_blocks_request_creation(self):
        mode = {"enabled": True, "allow_action_requests": False, "allow_scheduler_launch": False}
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch("solution_agent.orchestrator.core.load_maintenance_mode", return_value=mode):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            result = core.create_action_request("dependency_reconcile_apply_safe", reason="test")

        self.assertFalse(result["created"])
        self.assertIn("maintenance", result["error"])

    def test_schedule_once_request_requires_startup_guard_ok(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ), patch("solution_agent.orchestrator.core.scheduler_status", return_value={"running": False}), patch(
            "solution_agent.orchestrator.core.scheduler_startup_guard",
            return_value={"ok": False, "failures": [{"code": "registry_hygiene_issues"}]},
        ), patch("solution_agent.orchestrator.core.schedule_remote") as schedule_remote:
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            created = core.create_action_request(
                "schedule_once",
                reason="test launch",
                safe_level="requires_launch_approval",
                payload={
                    "host": "host",
                    "remote_root": "/tmp/runs",
                    "remote_workdir": "/tmp/work",
                    "allowed_gpu_ids": ["0"],
                },
            )
            result = core.process_action_requests(apply_selected=created["action_id"], allow_launch=True)
            listed = core.list_action_requests()

        self.assertEqual(result["processed"][0]["status"], "failed")
        self.assertEqual(listed["counts"]["failed"], 1)
        schedule_remote.assert_not_called()

    def test_schedule_once_request_passes_selected_worker(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), patch(
            "solution_agent.orchestrator.core.load_maintenance_mode",
            return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
        ), patch("solution_agent.orchestrator.core.scheduler_status", return_value={"running": False}), patch(
            "solution_agent.orchestrator.core.scheduler_startup_guard",
            return_value={"ok": True},
        ), patch("solution_agent.orchestrator.core.acquire_scheduler_lock", return_value={"acquired": True}), patch(
            "solution_agent.orchestrator.core.release_scheduler_lock", return_value=None
        ), patch("solution_agent.orchestrator.core.remote_reconcile_running_tasks", return_value={"checked": []}), patch(
            "solution_agent.orchestrator.core.schedule_remote", return_value={"started": []}
        ) as schedule_remote:
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            created = core.create_action_request(
                "schedule_once",
                reason="test selected worker",
                safe_level="requires_launch_approval",
                payload={
                    "host": "host",
                    "remote_root": "/tmp/runs",
                    "remote_workdir": "/tmp/work",
                    "allowed_gpu_ids": ["0"],
                    "worker": "materialized_execution",
                },
            )
            result = core.process_action_requests(apply_selected=created["action_id"], allow_launch=True)

        self.assertEqual(result["processed"][0]["status"], "done")
        self.assertEqual(schedule_remote.call_args.kwargs["worker"], "materialized_execution")


if __name__ == "__main__":
    unittest.main()

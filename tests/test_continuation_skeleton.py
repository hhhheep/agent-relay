from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import shutil
import yaml

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core
from solution_agent.continuation.audit import append_audit_event
from solution_agent.continuation.bounded_validation_bucket import (
    ARTIFACT_READINESS_REPORT_NAME,
    write_b14_l74_artifact_readiness_report,
)
from solution_agent.continuation.gate import (
    review_draft_text_stub,
    review_plan_stub,
    review_staging_manifest_stub,
    review_draft_payload_stub,
    scan_static_draft_text,
)
from solution_agent.continuation.interpreter import discover_result_artifacts, interpret_result_dir_stub, interpret_stub
from solution_agent.continuation.eval_runner import run_fixture_contract_eval, write_fixture_contract_eval_report
from solution_agent.continuation.lifecycle import (
    assert_no_promotion_in_p0,
    is_terminal_state,
    approve_record_stub,
    reject_record_stub,
    expire_record_stub,
    revoke_record_stub,
)
from solution_agent.continuation.pipeline import (
    run_local_continuation_pipeline,
    _parse_front_matter,
)
from solution_agent.continuation.paths import (
    continuation_audit_events_path,
    continuation_proposed_root,
    continuation_task_inbox_proposed_root,
)
from solution_agent.continuation.planner import plan_deterministic_stub, plan_stub
from solution_agent.continuation.policy import load_planner_policy
from solution_agent.continuation.schema import AuditEvent, ProposedTaskRecord, ProposalManifest, ResultInterpretation
from solution_agent.continuation.writer import stage_plan_stub


class ContinuationSkeletonTests(TestCase):
    @contextmanager
    def _patch_workspace(self, tmp: Path):
        repo_continuation = Path.cwd() / ".solution" / "continuation"
        with ExitStack() as stack:
            stack.enter_context(patch("solution_agent.orchestrator.paths.project_root", return_value=tmp))
            stack.enter_context(patch("solution_agent.continuation.paths.root", return_value=tmp))
            solution_dir = tmp / ".solution" / "continuation"
            solution_dir.mkdir(parents=True, exist_ok=True)
            for source in repo_continuation.glob("*.yaml"):
                (solution_dir / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            yield

    def _bundle_draft_jsons(self, path: str | Path):
        staged_dir = Path(path)
        excluded = {"manifest.json", "proposal_dry_run_manifest.json", "gate_report.json", "proposal.json"}
        return sorted(path for path in staged_dir.glob("*.json") if path.name not in excluded)

    def _assert_inbox_proposed_only(self, root: Path) -> None:
        inbox_root = root / "tasks" / "inbox"
        if not inbox_root.exists():
            return
        children = {path.name for path in inbox_root.iterdir()}
        self.assertIn("_proposed", children)
        self.assertEqual({name for name in children if name != "_proposed"}, set())

    def test_p0_skeleton_runs_without_inbox_or_registry_side_effects(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            policy = load_planner_policy()
            interpretation = interpret_stub("source_task_001")
            plan = plan_stub(interpretation)
            staged = stage_plan_stub(plan)
            report = review_plan_stub(plan.plan_id, interpretation.source_task_id, interpretation.source_result_hash)
            event = AuditEvent(
                event_id="event_001",
                timestamp="2026-05-16T01:25:52+08:00",
                actor="cli",
                action="stage",
                target=staged["path"],
                idempotency_key=plan.idempotency_key,
                outcome="success",
                source_task_id=interpretation.source_task_id,
                prompt_versions={},
                policy_version=str(policy["policy_version"]),
                inputs_hash="inputs_hash",
                outputs_hash="outputs_hash",
            )
            audit = append_audit_event(event)

            self.assertEqual(interpretation.category, "unknown")
            self.assertEqual(plan.proposed_next_steps, [])
            self.assertTrue(staged["dry_run"])
            self.assertEqual(report.gate_status, "manual_review")
            self.assertFalse(assert_no_promotion_in_p0()["promotion_allowed"])
            self.assertTrue(is_terminal_state("rejected"))

            staged_root = Path(staged["path"])
            self.assertTrue(staged_root.exists())
            self.assertTrue((staged_root / "manifest.json").exists())
            self.assertTrue(continuation_audit_events_path().exists())
            self.assertTrue(audit["written"])
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_stage_plan_stub_writes_structured_dry_run_drafts_only(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = interpret_stub("source_task_001")
            plan = plan_stub(interpretation)
            staged = stage_plan_stub(plan)

            manifest_path = Path(staged["manifest_path"])
            manifest = ProposalManifest(**json.loads(manifest_path.read_text(encoding="utf-8")))
            staged_dir = Path(staged["path"])
            draft_files = self._bundle_draft_jsons(staged_dir)

            self.assertTrue(manifest.dry_run)
            self.assertEqual(manifest.plan_id, plan.plan_id)
            self.assertEqual(manifest.source_task_id, plan.source_task_id)
            self.assertEqual(manifest.draft_count, 0)
            self.assertIn(
                f"tasks/inbox/_proposed/{plan.source_task_id}/{plan.plan_id}/",
                manifest.writes_allowed,
            )
            self.assertIn("proposal.json", manifest.proposed_paths)
            self.assertIn("tasks/inbox/", manifest.writes_forbidden)
            self.assertIn("registry.json", manifest.writes_forbidden)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())
            self.assertEqual(len(draft_files), 0)

    def test_static_gate_reviews_structured_dry_run_manifest(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = interpret_stub("source_task_001")
            plan = plan_stub(interpretation)
            staged = stage_plan_stub(plan)
            report = review_staging_manifest_stub(Path(staged["manifest_path"]))

            self.assertEqual(report.gate_status, "manual_review")
            self.assertEqual(report.violations, [])
            self.assertIn({"name": "staging_manifest_schema", "status": "passed"}, report.checks)
            self.assertIn({"name": "manifest_forbidden_writes", "status": "passed"}, report.checks)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_static_gate_rejects_invalid_staging_manifest(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            manifest_path = tmp / "continuation" / "proposed" / "bad" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text('{"dry_run": false}\n', encoding="utf-8")
            report = review_staging_manifest_stub(manifest_path)

            self.assertEqual(report.gate_status, "failed")
            self.assertEqual(report.violations[0]["rule_id"], "invalid_staging_manifest")
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_static_gate_flags_forbidden_draft_language(self):
        draft = """
# Proposed continuation

Dispatch GPU job with run-next, write guessed cache path /ssd6/fake/cache,
apply patch to source, mutate registry.json, then auto-promote to inbox.
Also use Line B artifacts.
"""
        scan = scan_static_draft_text(draft)
        report = review_draft_text_stub(
            proposed_task_id="proposed_unsafe",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )

        rule_ids = {item["rule_id"] for item in scan["violations"]}
        self.assertIn("direct_dispatch", rule_ids)
        self.assertIn("remote_path_guess", rule_ids)
        self.assertIn("source_edit", rule_ids)
        self.assertIn("registry_mutation", rule_ids)
        self.assertIn("cross_line_continuation", rule_ids)
        self.assertIn("auto_promotion", rule_ids)
        self.assertEqual(report.gate_status, "failed")

    def test_static_gate_rejects_direct_dispatch_language(self):
        draft = """
# Proposed continuation

Please dispatch GPU job now.
"""
        report = review_draft_text_stub(
            proposed_task_id="proposed_direct_dispatch",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )
        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(report.violations[0]["rule_id"], "direct_dispatch")

    def test_static_gate_rejects_run_next_and_scheduler_submit_language(self):
        draft = """
# Proposed continuation

Please run-next this draft and schedule_remote the batch immediately.
"""
        report = review_draft_text_stub(
            proposed_task_id="proposed_run_next_scheduler",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )

        rule_ids = {item["rule_id"] for item in report.violations}
        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(rule_ids, {"direct_dispatch"})

    def test_static_gate_allows_negated_dispatch_or_submit_language(self):
        draft = """
# Proposed continuation

Do not dispatch or run-next; also do not schedule_remote.
"""
        scan = scan_static_draft_text(draft)
        report = review_draft_text_stub(
            proposed_task_id="proposed_safe_negation",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )

        self.assertEqual(scan["violations"], [])
        self.assertEqual(report.gate_status, "manual_review")

    def test_static_gate_ignores_forbidden_actions_front_matter(self):
        draft = """---
forbidden_actions:
  - run-next
  - dispatch
  - scheduler_submit
---
# Proposed continuation

Claim-limited proposal markdown with no command text.
"""
        scan = scan_static_draft_text(draft)
        report = review_draft_text_stub(
            proposed_task_id="proposed_front_matter_safe",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )

        self.assertEqual(scan["violations"], [])
        self.assertEqual(report.gate_status, "manual_review")

    def test_static_gate_rejects_guessed_remote_path_phrases(self):
        draft = """
# Proposed continuation

Write intermediate artifacts to /ssd6/guessed/cache/path and then inspect C:\\\\Temp\\\\run_cache\\\\next.
"""
        report = review_draft_text_stub(
            proposed_task_id="proposed_path_guess",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )
        rule_ids = {item["rule_id"] for item in report.violations}
        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(rule_ids, {"remote_path_guess"})

    def test_static_gate_rejects_mnt_and_unc_path_phrases(self):
        draft = """
# Proposed continuation

Write temporary outputs to /mnt/exp/cache/ and then open \\\\fileserver\\share\\run_dir.
"""
        report = review_draft_text_stub(
            proposed_task_id="proposed_remote_path_guess_2",
            source_task_id="source_task_002",
            source_result_hash="hash_002",
            draft_text=draft,
        )
        rule_ids = {item["rule_id"] for item in report.violations}
        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(rule_ids, {"remote_path_guess"})

    def test_static_gate_rejects_windows_drive_guess(self):
        draft = """
# Proposed continuation

Use Z:\\run_cache\\next for local scratch output.
"""
        report = review_draft_text_stub(
            proposed_task_id="proposed_windows_drive_guess",
            source_task_id="source_task_003",
            source_result_hash="hash_003",
            draft_text=draft,
        )
        rule_ids = {item["rule_id"] for item in report.violations}
        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(rule_ids, {"remote_path_guess"})

    def test_static_gate_allows_claim_limited_local_draft(self):
        draft = """
# Proposed continuation

Create a local claim-limited summary draft under continuation/staging.
Do not dispatch, do not edit source, do not mutate registry, and do not guess paths.
"""
        scan = scan_static_draft_text(draft)
        report = review_draft_text_stub(
            proposed_task_id="proposed_safe",
            source_task_id="source_task_001",
            source_result_hash="hash_001",
            draft_text=draft,
        )

        self.assertEqual(scan["violations"], [])
        self.assertEqual(report.gate_status, "manual_review")

    def test_static_gate_rejects_draft_payload_missing_required_fields(self):
        draft_payload = {
            "draft_id": "source_task_001__bounded_probe_intent",
            "source_task_id": "source_task_001",
            # intentionally omit required source_result_hash
            "reason_for_continuation": "Need a bounded follow-up due to weak evidence.",
            "goal": "collect a targeted local artifact",
            "scope": "local_staging_only",
            "allowed_actions": ["inspect_local_artifacts"],
            "forbidden_actions": ["source_edit"],
            "expected_outputs": ["action_requests.json"],
            "claim_limitation": "Intent only; staging-only no writes.",
            "idempotency_key": "source_task_001:hash_001:0.1.0:stage:bounded_probe",
        }

        report = review_draft_payload_stub(draft_payload=draft_payload)

        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(report.violations, [{"rule_id": "draft_required_fields", "matched": "source_result_hash", "severity": "error"}])

    def test_static_gate_rejects_draft_payload_missing_claim_limitation(self):
        draft_payload = {
            "draft_id": "source_task_001__bounded_probe_intent",
            "source_task_id": "source_task_001",
            "source_result_hash": "hash_001",
            "reason_for_continuation": "Need a bounded follow-up due to weak evidence.",
            "goal": "collect a targeted local artifact",
            "scope": "local_staging_only",
            "allowed_actions": ["inspect_local_artifacts"],
            "forbidden_actions": ["source_edit"],
            "expected_outputs": ["action_requests.json"],
            # intentionally omit required claim_limitation
            "idempotency_key": "source_task_001:hash_001:0.1.0:stage:bounded_probe",
        }

        report = review_draft_payload_stub(draft_payload=draft_payload)

        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(report.violations, [{"rule_id": "draft_required_fields", "matched": "claim_limitation", "severity": "error"}])

    def test_static_gate_rejects_draft_payload_missing_forbidden_actions(self):
        draft_payload = {
            "draft_id": "source_task_001__bounded_probe_intent",
            "source_task_id": "source_task_001",
            "source_result_hash": "hash_001",
            "reason_for_continuation": "Need a bounded follow-up due to weak evidence.",
            "goal": "collect a targeted local artifact",
            "scope": "local_staging_only",
            "allowed_actions": ["inspect_local_artifacts"],
            # intentionally omit required forbidden_actions
            "expected_outputs": ["action_requests.json"],
            "claim_limitation": "Intent only; staging-only no writes.",
            "idempotency_key": "source_task_001:hash_001:0.1.0:stage:bounded_probe",
        }

        report = review_draft_payload_stub(draft_payload=draft_payload)

        self.assertEqual(report.gate_status, "failed")
        self.assertEqual(report.violations, [{"rule_id": "draft_required_fields", "matched": "forbidden_actions", "severity": "error"}])

    def test_reject_and_revoke_lifecycle_stubs_write_audit_only(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            record = ProposedTaskRecord(
                proposed_task_id="proposed_001",
                state="drafted",
                source_task_id="source_task_001",
                source_result_hash="hash_001",
                idempotency_key="source_task_001:hash_001:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )

            rejected = reject_record_stub(record, reason="operator rejected unsafe continuation")
            revoked = revoke_record_stub(record, reason="operator revoked stale continuation")

            self.assertEqual(rejected["record"].state, "rejected")
            self.assertEqual(rejected["event"].action, "reject")
            self.assertTrue(rejected["audit"]["written"])
            self.assertEqual(len(rejected["record"].rejection_history), 1)

            self.assertEqual(revoked["record"].state, "revoked")
            self.assertEqual(revoked["event"].action, "revoke")
            self.assertTrue(revoked["audit"]["written"])

            events_path = continuation_audit_events_path()
            self.assertTrue(events_path.exists())
            events_text = events_path.read_text(encoding="utf-8")
            self.assertIn('"action":"reject"', events_text)
            self.assertIn('"action":"revoke"', events_text)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_approve_and_expire_lifecycle_stubs_do_not_promote_active_inbox(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            active_inbox_target = tmp / "tasks" / "inbox" / "proposed_direct_promotion.md"
            active_inbox_target.parent.mkdir(parents=True, exist_ok=True)
            active_inbox_target.write_text("# existing\n", encoding="utf-8")

            record = ProposedTaskRecord(
                proposed_task_id="proposed_002",
                state="drafted",
                source_task_id="source_task_002",
                source_result_hash="hash_002",
                idempotency_key="source_task_002:hash_002:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )

            approved = approve_record_stub(record, reason="operator approved staged proposal")
            expired = expire_record_stub(record, reason="proposal context expired")

            self.assertEqual(approved["record"].state, "staging_passed")
            self.assertEqual(expired["record"].state, "expired")
            self.assertEqual(approved["event"].action, "approve")
            self.assertEqual(expired["event"].action, "expire")
            self.assertTrue(approved["audit"]["written"])
            self.assertTrue(expired["audit"]["written"])
            self.assertEqual(len(approved["record"].approval_history), 1)
            self.assertEqual(len(expired["record"].expiry_history), 1)

            events_text = Path(continuation_audit_events_path()).read_text(encoding="utf-8")
            self.assertIn('"action":"approve"', events_text)
            self.assertIn('"action":"expire"', events_text)
            self.assertEqual(active_inbox_target.read_text(encoding="utf-8"), "# existing\n")

    def test_interpreter_discovers_local_result_artifacts_without_inference(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_001"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: partial\n", encoding="utf-8")
            (result_dir / "decision_summary.json").write_text('{"recommended_use": "none"}\n', encoding="utf-8")

            discovery = discover_result_artifacts(result_dir)
            interpretation = interpret_result_dir_stub("source_task_001", result_dir)

            discovered = {item["relative_path"] for item in discovery["discovered"]}
            self.assertIn("_worker_out/result.yaml", discovered)
            self.assertIn("decision_summary.json", discovered)
            self.assertEqual(interpretation.category, "needs_user_decision")
            self.assertEqual(interpretation.recommended_continuation_kinds, ["no_safe_continuation"])
            self.assertTrue(interpretation.evidence["deterministic_only"])
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_interpreter_marks_missing_artifact_when_no_local_candidates_exist(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_002"
            interpretation = interpret_result_dir_stub("source_task_002", result_dir)

            self.assertEqual(interpretation.category, "missing_artifact")
            self.assertEqual(interpretation.recommended_continuation_kinds, ["inventory_audit"])
            self.assertTrue(interpretation.needs_user_decision)
            self.assertEqual(interpretation.evidence["artifact_discovery"]["discovered"], [])
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_interpreter_deterministic_missing_artifact_from_local_summary(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_artifact_missing"
            result_dir.mkdir(parents=True)
            (result_dir / "decision_summary.json").write_text(
                json.dumps(
                    {
                        "category": "missing_artifact",
                        "evidence": {"missing_artifact_detected": True},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            interpretation = interpret_result_dir_stub("source_task_artifact_missing", result_dir)

            self.assertEqual(interpretation.category, "missing_artifact")
            self.assertTrue(interpretation.needs_user_decision)
            self.assertEqual(interpretation.recommended_continuation_kinds, ["inventory_audit"])
            self.assertTrue(interpretation.evidence.get("missing_artifact_detected"))

    def test_interpreter_deterministic_missing_capability_from_local_summary(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_missing_capability"
            result_dir.mkdir(parents=True)
            (result_dir / "decision.json").write_text(
                json.dumps(
                    {
                        "missing_capability_detected": True,
                        "details": {
                            "reason": "dependency unavailable",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            interpretation = interpret_result_dir_stub("source_task_missing_capability", result_dir)

            self.assertEqual(interpretation.category, "missing_capability")
            self.assertTrue(interpretation.evidence.get("missing_capability_detected"))
            self.assertEqual(interpretation.recommended_continuation_kinds, ["missing_capability_request"])

    def test_interpreter_deterministic_weak_evidence_success_from_local_artifact_text(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_weak_evidence"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text(
                "status: success\nboundary_f1_gain: 0.12\n",
                encoding="utf-8",
            )
            (result_dir / "artifact_summary.md").write_text(
                "Boundary F1 gain observed, weak evidence remains.",
                encoding="utf-8",
            )

            interpretation = interpret_result_dir_stub("source_task_weak_evidence", result_dir)

            self.assertEqual(interpretation.category, "success_but_weak_evidence")
            self.assertTrue(interpretation.execution_success)
            self.assertEqual(
                interpretation.recommended_continuation_kinds,
                ["bounded_probe", "decision_synthesis"],
            )

    def test_interpreter_deterministic_source_code_required_from_local_artifacts(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_source_code"
            result_dir.mkdir(parents=True)
            (result_dir / "decision_summary.json").write_text(
                "source code update required to implement requested path",
                encoding="utf-8",
            )

            interpretation = interpret_result_dir_stub("source_task_source_code", result_dir)

            self.assertEqual(interpretation.category, "source_code_required")
            self.assertTrue(interpretation.needs_codex)
            self.assertIn("source-update", interpretation.claim_limitations[-1])

    def test_deterministic_planner_maps_missing_artifact_to_inventory_audit_intent_only(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = interpret_result_dir_stub("source_task_002", tmp / "results" / "source_task_002")
            plan = plan_deterministic_stub(interpretation)

            self.assertEqual(len(plan.proposed_next_steps), 1)
            step = plan.proposed_next_steps[0]
            self.assertEqual(step.kind, "inventory_audit")
            self.assertFalse(step.can_stage)
            self.assertIn("remote_path_guess", step.forbidden_actions)
            self.assertIn("Intent only", step.claim_limitation)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_deterministic_planner_suppresses_duplicate_inventory_audit_by_hash_and_kind(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_013",
                source_result_hash="hash_013",
                idempotency_key="source_task_013:hash_013:0.1.0:interpret",
                category="missing_artifact",
                evidence={
                    "prior_proposals": [
                        {
                            "source_task_id": "source_task_013",
                            "source_result_hash": "hash_013",
                            "category": "missing_artifact",
                            "next_step_kind": "inventory_audit",
                        },
                        {
                            "source_task_id": "source_task_013",
                            "source_result_hash": "hash_013",
                            "category": "missing_artifact",
                            "next_step_kind": "inventory_audit",
                        },
                    ],
                    "repeated_proposal_detected": True,
                },
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            self.assertEqual(plan.proposed_next_steps, [])
            self.assertEqual(plan.no_safe_continuation_reason, "Duplicate proposal detected; suppressing repeated stageable inventory_audit draft.")
            self.assertEqual(staged["proposed_task_count"], 0)
            self.assertEqual(staged["draft_count"], 0)
            self.assertEqual(staged["drafts"], [])
            staged_dir = Path(staged["path"])
            self.assertEqual(
                set(path.name for path in staged_dir.glob("*.json")),
                {"manifest.json", "proposal_dry_run_manifest.json", "proposal.json", "gate_report.json"},
            )
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_deterministic_planner_keeps_unknown_as_no_safe_continuation(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_001"
            result_dir.mkdir(parents=True)
            (result_dir / "decision_summary.json").write_text("{}\n", encoding="utf-8")
            interpretation = interpret_result_dir_stub("source_task_001", result_dir)
            plan = plan_deterministic_stub(interpretation)

            self.assertEqual(plan.proposed_next_steps, [])
            self.assertIn("unknown", plan.no_safe_continuation_reason)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_deterministic_interpreter_planner_matrix_from_explicit_category_tags(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            cases = [
                (
                    "missing_artifact",
                    "m",
                    ["inventory_audit"],
                    {"result.yaml": "status: failed\n", "decision_summary.json": json.dumps({
                        "artifact_text": "artifact is missing detector_threshold_sweep.csv."
                    }) + "\n"},
                ),
                (
                    "missing_capability",
                    "mc",
                    ["missing_capability_request"],
                    {"decision_summary.json": json.dumps({"category": "missing_capability"}) + "\n"},
                ),
                (
                    "source_code_required",
                    "sr",
                    ["codex_debug_request"],
                    {"decision_summary.json": json.dumps({"category": "source_code_required"}) + "\n"},
                ),
                (
                    "success_but_weak_evidence",
                    "wb",
                    ["bounded_probe", "decision_synthesis"],
                    {"result.yaml": "status: success\nboundary_f1_gain: 0.11\n"},
                ),
                (
                    "unknown",
                    "u",
                    [],
                    {"decision_summary.json": '{"category":"not_a_known_category"}\n'},
                ),
            ]
            for idx, (category, source_task_id, expected_kinds, payloads) in enumerate(cases):
                with self.subTest(case=category, index=idx):
                    result_dir = tmp / "results" / f"{source_task_id}"
                    worker_out = result_dir / "_worker_out"
                    worker_out.mkdir(parents=True, exist_ok=True)
                    for file_name, content in payloads.items():
                        (result_dir / file_name).write_text(content, encoding="utf-8")

                    interpretation = interpret_result_dir_stub(source_task_id, result_dir)
                    self.assertEqual(interpretation.category.value, category)
                    plan = plan_deterministic_stub(interpretation)

                    if expected_kinds:
                        self.assertEqual([step.kind.value for step in plan.proposed_next_steps], expected_kinds)
                        staged = stage_plan_stub(plan)
                        staged_dir = Path(staged["path"])
                        draft_files = self._bundle_draft_jsons(staged_dir)
                        self.assertEqual(len(draft_files), len(expected_kinds))
                        for draft_file in draft_files:
                            draft_payload = json.loads(draft_file.read_text(encoding="utf-8"))
                            markdown_text = Path(staged["path"]).joinpath(f"{Path(draft_file).stem}.md").read_text(encoding="utf-8")
                            self.assertNotIn("/ssd", markdown_text)
                            self.assertNotIn("/mnt", markdown_text)
                            self.assertNotIn("\\\\", markdown_text)
                            self.assertIn("expected_outputs", draft_payload)
                    else:
                        self.assertEqual(plan.proposed_next_steps, [])
                        self.assertEqual(plan.no_safe_continuation_reason, "No deterministic P0 planner mapping for category: unknown")

    def test_deterministic_planner_maps_source_code_required_to_codex_debug_intent_only(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_003",
                source_result_hash="hash_003",
                idempotency_key="source_task_003:hash_003:0.1.0:interpret",
                category="source_code_required",
                needs_codex=True,
                recommended_continuation_kinds=["codex_debug_request"],
                evidence={"reason": "source behavior change required"},
            )
            plan = plan_deterministic_stub(interpretation)

            self.assertEqual(len(plan.proposed_next_steps), 1)
            step = plan.proposed_next_steps[0]
            self.assertEqual(step.kind, "codex_debug_request")
            self.assertEqual(step.owner, "codex")
            self.assertFalse(step.can_stage)
            self.assertIn("direct_dispatch", step.forbidden_actions)
            self.assertIn("scheduler_submit", step.forbidden_actions)
            self.assertIn("does not edit source code", step.claim_limitation)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_deterministic_planner_maps_missing_capability_to_request_intent_only(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_004",
                source_result_hash="hash_004",
                idempotency_key="source_task_004:hash_004:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={
                    "reason": "worker cannot access required detector cache capability",
                    "missing_capability_detected": True,
                },
            )
            plan = plan_deterministic_stub(interpretation)

            self.assertEqual(len(plan.proposed_next_steps), 1)
            step = plan.proposed_next_steps[0]
            self.assertEqual(step.kind, "missing_capability_request")
            self.assertEqual(step.owner, "continuation_planner")
            self.assertFalse(step.can_stage)
            self.assertIn("action_requests.json", step.expected_outputs)
            self.assertIn("direct_dispatch", step.forbidden_actions)
            self.assertIn("registry_mutation", step.forbidden_actions)
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_stage_plan_draft_writer_emits_missing_capability_request_draft(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_004",
                source_result_hash="hash_004",
                idempotency_key="source_task_004:hash_004:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            staged_dir = Path(staged["path"])
            draft_files = self._bundle_draft_jsons(staged_dir)
            self.assertEqual(len(draft_files), 1)

            draft_payload = json.loads(draft_files[0].read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged["manifest_path"]).read_text(encoding="utf-8"))

            self.assertEqual(draft_payload["source_task_id"], "source_task_004")
            self.assertEqual(draft_payload["source_result_hash"], "hash_004")
            self.assertIn("action_requests.json", draft_payload["expected_outputs"])
            self.assertIn("missing_capability_request_intent", draft_payload["draft_id"])
            self.assertIn("direct_dispatch", draft_payload["forbidden_actions"])
            self.assertIn("registry_mutation", draft_payload["forbidden_actions"])
            self.assertIn("Intent only", draft_payload["claim_limitation"])
            self.assertIn(f"{draft_files[0].name}", manifest["proposed_paths"])

    def test_stage_plan_draft_writer_emits_bounded_probe_draft(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_005",
                source_result_hash="hash_005",
                idempotency_key="source_task_005:hash_005:0.1.0:interpret",
                category="success_but_weak_evidence",
                execution_success=True,
                recommended_continuation_kinds=["bounded_probe", "decision_synthesis"],
                evidence={"reason": "confidence around boundary_f1_gain"},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            staged_dir = Path(staged["path"])
            draft_files = self._bundle_draft_jsons(staged_dir)
            self.assertEqual(len(draft_files), 2)

            bounded_path = next(path for path in draft_files if "bounded_probe" in path.name)
            bounded_payload = json.loads(bounded_path.read_text(encoding="utf-8"))

            self.assertEqual(bounded_payload["source_task_id"], "source_task_005")
            self.assertEqual(bounded_payload["source_result_hash"], "hash_005")
            self.assertEqual(bounded_payload["goal"], "bound confidence and collect additional local artifacts")
            self.assertIn("source_edit", bounded_payload["forbidden_actions"])
            self.assertIn("action_requests.json", bounded_payload["expected_outputs"])
            self.assertIn("local_staging_only", bounded_payload["scope"])
            self.assertIn("local_artifact_generation", bounded_payload["allowed_actions"])
            self.assertIn("non-executing", bounded_payload["claim_limitation"])

    def test_stage_plan_draft_writer_emits_decision_synthesis_draft(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_005",
                source_result_hash="hash_005",
                idempotency_key="source_task_005:hash_005:0.1.0:interpret",
                category="success_but_weak_evidence",
                execution_success=True,
                recommended_continuation_kinds=["bounded_probe", "decision_synthesis"],
                evidence={"reason": "confidence around boundary_f1_gain"},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            staged_dir = Path(staged["path"])
            draft_files = self._bundle_draft_jsons(staged_dir)
            self.assertEqual(len(draft_files), 2)

            decision_path = next(path for path in draft_files if "decision_synthesis" in path.name)
            decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))

            self.assertEqual(decision_payload["source_task_id"], "source_task_005")
            self.assertEqual(decision_payload["goal"], "synthesize a bounded continuation decision before execution")
            self.assertIn("forbidden_actions", decision_payload)
            self.assertIn("source_edit", decision_payload["forbidden_actions"])
            self.assertIn("artifact_inventory_report.md", decision_payload["expected_outputs"])
            self.assertIn("manifest", staged["manifest_path"])

    def test_stage_plan_draft_writer_emits_codex_debug_request_draft(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_006",
                source_result_hash="hash_006",
                idempotency_key="source_task_006:hash_006:0.1.0:interpret",
                category="source_code_required",
                needs_codex=True,
                recommended_continuation_kinds=["codex_debug_request"],
                evidence={"reason": "source change required"},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            staged_dir = Path(staged["path"])
            draft_files = self._bundle_draft_jsons(staged_dir)
            self.assertEqual(len(draft_files), 1)

            codex_payload = json.loads(draft_files[0].read_text(encoding="utf-8"))
            self.assertEqual(codex_payload["goal"], "request Codex-assisted debugging for source-level work")
            self.assertEqual(codex_payload["source_task_id"], "source_task_006")
            self.assertEqual(codex_payload["source_result_hash"], "hash_006")
            self.assertIn("codex_debug_request.md", codex_payload["expected_outputs"])
            self.assertIn("direct_dispatch", codex_payload["forbidden_actions"])
            self.assertIn("registry_mutation", codex_payload["forbidden_actions"])
            self.assertIn("source_edit", codex_payload["forbidden_actions"])
            self.assertIn("local_staging_only", codex_payload["scope"])

    def test_stage_plan_draft_writer_idempotency_rewrites_skipped(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_007",
                source_result_hash="hash_007",
                idempotency_key="source_task_007:hash_007:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)

            first = stage_plan_stub(plan)
            second = stage_plan_stub(plan)

            self.assertEqual(first["drafts"][0]["outcome"], "written")
            self.assertEqual(second["drafts"][0]["outcome"], "skip")
            self.assertEqual(first["drafts"][0]["status"], "written")
            self.assertEqual(second["drafts"][0]["status"], "skipped")

    def test_stage_plan_draft_writer_collision_rewrites_fail_safe(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_008",
                source_result_hash="hash_008",
                idempotency_key="source_task_008:hash_008:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)
            staged_dir = Path(staged["path"])
            draft_path = self._bundle_draft_jsons(staged_dir)[0]
            proposal_path = staged_dir / "proposal.json"
            proposal_report_path = staged_dir / "proposal_dry_run_manifest.json"
            proposal_text_original = proposal_path.read_text(encoding="utf-8")
            proposal_report_original = proposal_report_path.read_text(encoding="utf-8")
            collision_payload = json.loads(draft_path.read_text(encoding="utf-8"))
            collision_payload["goal"] = "Mutated conflicting goal that should trigger collision detection."
            draft_path.write_text(json.dumps(collision_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            rerun = stage_plan_stub(plan)

            self.assertEqual(rerun["drafts"][0]["outcome"], "collision")
            self.assertEqual(rerun["drafts"][0]["status"], "collision")
            self.assertEqual(rerun["drafts"][0]["reason"], "same draft id with different content")
            self.assertEqual(draft_path.read_text(encoding="utf-8"), json.dumps(collision_payload, indent=2, sort_keys=True) + "\n")
            self.assertEqual(proposal_path.read_text(encoding="utf-8"), proposal_text_original)
            self.assertEqual(proposal_report_path.read_text(encoding="utf-8"), proposal_report_original)

    def test_stage_plan_draft_writer_repeated_writes_changed_content_collision(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_011",
                source_result_hash="hash_011",
                idempotency_key="source_task_011:hash_011:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            first = stage_plan_stub(plan)
            staged_dir = Path(first["path"])
            draft_file = self._bundle_draft_jsons(staged_dir)[0]
            manifest_path = staged_dir / "manifest.json"
            manifest_original = manifest_path.read_text(encoding="utf-8")
            collision_payload = json.loads(draft_file.read_text(encoding="utf-8"))
            collision_payload["goal"] = "changed content should fail safe"
            draft_file.write_text(json.dumps(collision_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            rerun = stage_plan_stub(plan)
            self.assertEqual(rerun["drafts"][0]["outcome"], "collision")
            self.assertEqual(rerun["drafts"][0]["status"], "collision")
            self.assertEqual(rerun["drafts"][0]["reason"], "same draft id with different content")
            self.assertEqual(draft_file.read_text(encoding="utf-8"), json.dumps(collision_payload, indent=2, sort_keys=True) + "\n")
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), manifest_original)

    def test_stage_plan_draft_writer_rejects_invalid_existing_json_without_overwrite(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_020",
                source_result_hash="hash_020",
                idempotency_key="source_task_020:hash_020:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)
            staged_dir = Path(staged["path"])
            draft_path = self._bundle_draft_jsons(staged_dir)[0]
            proposal_path = staged_dir / "proposal.json"
            proposal_text_original = proposal_path.read_text(encoding="utf-8")
            draft_path.write_text("not-json\n", encoding="utf-8")

            rerun = stage_plan_stub(plan)
            self.assertEqual(rerun["drafts"][0]["outcome"], "collision")
            self.assertEqual(rerun["drafts"][0]["reason"], "existing draft payload is not valid for replay-safe read")
            self.assertEqual(draft_path.read_text(encoding="utf-8"), "not-json\n")
            self.assertEqual(proposal_path.read_text(encoding="utf-8"), proposal_text_original)

    def test_stage_plan_draft_writer_rejects_missing_markdown_without_overwrite(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_021",
                source_result_hash="hash_021",
                idempotency_key="source_task_021:hash_021:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)
            staged_dir = Path(staged["path"])
            draft_json = self._bundle_draft_jsons(staged_dir)[0]
            draft_markdown = staged_dir / f"{draft_json.stem}.md"
            proposal_path = staged_dir / "proposal.json"
            proposal_text_original = proposal_path.read_text(encoding="utf-8")
            draft_markdown.unlink()

            rerun = stage_plan_stub(plan)
            self.assertEqual(rerun["drafts"][0]["outcome"], "collision")
            self.assertEqual(rerun["drafts"][0]["reason"], "existing draft bundle missing companion markdown")
            self.assertFalse(draft_markdown.exists())
            self.assertEqual(proposal_path.read_text(encoding="utf-8"), proposal_text_original)

    def test_stage_plan_draft_writer_rejects_partial_snapshot_without_overwrite(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_022",
                source_result_hash="hash_022",
                idempotency_key="source_task_022:hash_022:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)
            staged_dir = Path(staged["path"])
            draft_json = self._bundle_draft_jsons(staged_dir)[0]
            proposal_path = staged_dir / "proposal.json"
            proposal_text_original = proposal_path.read_text(encoding="utf-8")
            manifest_path = staged_dir / "manifest.json"

            manifest_path.unlink()

            rerun = stage_plan_stub(plan)
            self.assertEqual(rerun["drafts"][0]["outcome"], "collision")
            self.assertEqual(rerun["drafts"][0]["reason"], "existing partial snapshot detected; refusing overwrite for safety")
            self.assertFalse(manifest_path.exists())
            self.assertEqual(draft_json.exists(), True)
            self.assertEqual(proposal_path.read_text(encoding="utf-8"), proposal_text_original)
            self.assertFalse(rerun["gate_report"]["gate_status"] == "passed")

    def test_stage_plan_draft_writer_repeated_writes_same_key_and_content_skip(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_010",
                source_result_hash="hash_010",
                idempotency_key="source_task_010:hash_010:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            first = stage_plan_stub(plan)
            second = stage_plan_stub(plan)

            self.assertEqual(first["drafts"][0]["outcome"], "written")
            self.assertEqual(second["drafts"][0]["outcome"], "skip")
            self.assertEqual(first["drafts"][0]["status"], "written")
            self.assertEqual(second["drafts"][0]["status"], "skipped")
            staged_dir = Path(first["path"])
            draft_file = self._bundle_draft_jsons(staged_dir)[0]
            second_result = second["drafts"][0]
            self.assertEqual(second_result["path"], str(draft_file))

    def test_stage_plan_replay_is_stable_bundle_and_manifest(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_023",
                source_result_hash="hash_023",
                idempotency_key="source_task_023:hash_023:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            first = stage_plan_stub(plan)
            second = stage_plan_stub(plan)

            self.assertTrue(first["staged"])
            self.assertFalse(second["staged"])
            self.assertTrue(first["manifest_written"])
            self.assertTrue(second["proposal_bundle_reused"])
            self.assertFalse(second["manifest_written"])

            self.assertEqual(first["manifest"], second["manifest"])
            self.assertEqual(first["gate_report"], second["gate_report"])
            self.assertEqual(first["bundle"], second["bundle"])

            first_events = continuation_audit_events_path().read_text(encoding="utf-8").splitlines()
            second_events = continuation_audit_events_path().read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(second_events), len(first_events))
            self.assertEqual(len(first_events), 2)
            event = json.loads(second_events[-1])
            self.assertEqual(event["idempotency_key"], plan.idempotency_key)
            self.assertEqual(event["action"], "write")
            self.assertEqual(event["outcome"], "success")
            self.assertEqual(event["target"], first["path"])

            proposal_json_path = Path(first["proposal_json_path"])
            self.assertTrue(proposal_json_path.exists())
            self.assertTrue(Path(first["manifest_path"]).exists())
            self.assertEqual(
                proposal_json_path.read_text(encoding="utf-8"),
                Path(second["proposal_json_path"]).read_text(encoding="utf-8"),
            )

    def test_stage_plan_draft_writer_records_audit_event_stub(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_009",
                source_result_hash="hash_009",
                idempotency_key="source_task_009:hash_009:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            self.assertTrue(staged["audit"]["written"])
            events_text = Path(continuation_audit_events_path()).read_text(encoding="utf-8")
            self.assertIn('"action":"write"', events_text)
            self.assertIn("draft_write", events_text)

    def test_stage_plan_draft_writer_audit_only_writes_to_continuation_audit(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_012",
                source_result_hash="hash_012",
                idempotency_key="source_task_012:hash_012:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            events_path = Path(continuation_audit_events_path())
            self.assertTrue(events_path.exists())
            self.assertEqual(str(events_path), str(tmp / "continuation" / "audit" / "events.jsonl"))
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())
            self.assertIn("tasks/inbox/", staged["manifest"]["writes_forbidden"][0])

    def test_deterministic_planner_maps_weak_metric_success_to_bounded_followups(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_005",
                source_result_hash="hash_005",
                idempotency_key="source_task_005:hash_005:0.1.0:interpret",
                category="success_but_weak_evidence",
                execution_success=True,
                recommended_continuation_kinds=["bounded_probe", "decision_synthesis"],
                evidence={"reason": "boundary_f1_gain is weak"},
            )
            plan = plan_deterministic_stub(interpretation)
            kinds = [step.kind for step in plan.proposed_next_steps]

            self.assertEqual(len(plan.proposed_next_steps), 2)
            self.assertEqual(kinds, ["bounded_probe", "decision_synthesis"])
            for step in plan.proposed_next_steps:
                self.assertFalse(step.can_stage)
                self.assertIn("non-executing", step.claim_limitation)
                self.assertIn("source_edit", step.forbidden_actions)
                self._assert_inbox_proposed_only(tmp)
                self.assertFalse((tmp / "registry.json").exists())

    def test_deterministic_intents_emit_drafts_with_claim_limitation_and_forbidden_actions(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            source_base = "src"
            missing_capability = ResultInterpretation(
                source_task_id=f"{source_base}_001",
                source_result_hash="hash_001",
                idempotency_key=f"{source_base}_001:hash_001:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            weak = ResultInterpretation(
                source_task_id=f"{source_base}_002",
                source_result_hash="hash_002",
                idempotency_key=f"{source_base}_002:hash_002:0.1.0:interpret",
                category="success_but_weak_evidence",
                execution_success=True,
                recommended_continuation_kinds=["bounded_probe", "decision_synthesis"],
                evidence={"reason": "bounded + synthesis policy path"},
            )
            codex = ResultInterpretation(
                source_task_id=f"{source_base}_003",
                source_result_hash="hash_003",
                idempotency_key=f"{source_base}_003:hash_003:0.1.0:interpret",
                category="source_code_required",
                recommended_continuation_kinds=["codex_debug_request"],
                needs_codex=True,
                evidence={"source_change_required": True},
            )

            interpretations = [missing_capability, weak, codex]
            observed_kinds = set()
            for interpretation in interpretations:
                plan = plan_deterministic_stub(interpretation)
                staged = stage_plan_stub(plan)
                staged_dir = Path(staged["path"])
                for payload_path in self._bundle_draft_jsons(staged_dir):
                    payload = json.loads(payload_path.read_text(encoding="utf-8"))
                    self.assertIn("claim_limitation", payload)
                    self.assertIn("forbidden_actions", payload)
                    self.assertTrue(payload["claim_limitation"])
                    self.assertTrue(payload["forbidden_actions"])
                    file_text = payload_path.name
                    if "_missing_capability_request_intent" in file_text:
                        observed_kinds.add("missing_capability_request")
                    if "_bounded_probe_intent" in file_text:
                        observed_kinds.add("bounded_probe")
                    if "_decision_synthesis_intent" in file_text:
                        observed_kinds.add("decision_synthesis")
                    if "_codex_debug_intent" in file_text:
                        observed_kinds.add("codex_debug_request")
            self.assertEqual(
                observed_kinds,
                {"missing_capability_request", "bounded_probe", "decision_synthesis", "codex_debug_request"},
            )
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_static_gate_integration_over_actual_generated_drafts(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_014",
                source_result_hash="hash_014",
                idempotency_key="source_task_014:hash_014:0.1.0:interpret",
                category="success_but_weak_evidence",
                execution_success=True,
                recommended_continuation_kinds=["bounded_probe", "decision_synthesis"],
                evidence={"reason": "intents must be verified through draft gate"},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            staged_dir = Path(staged["path"])
            draft_paths = self._bundle_draft_jsons(staged_dir)
            self.assertEqual(len(draft_paths), 2)

            for draft_path in draft_paths:
                payload = json.loads(draft_path.read_text(encoding="utf-8"))
                review = review_draft_payload_stub(draft_payload=payload)
                self.assertEqual(review.gate_status, "manual_review")
                self.assertEqual(review.violations, [])

    def test_full_pipeline_interpret_plan_stage_gate_report_has_no_inbox_registry_side_effects(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_015"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: partial\n", encoding="utf-8")
            (result_dir / "decision_summary.json").write_text('{"recommended_steps": []}\n', encoding="utf-8")

            interpretation = interpret_result_dir_stub("source_task_015", result_dir)
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            manifest = review_staging_manifest_stub(Path(staged["manifest_path"]))
            self.assertEqual(manifest.gate_status, "manual_review")

            staged_dir = Path(staged["path"])
            draft_paths = self._bundle_draft_jsons(staged_dir)
            for draft_path in draft_paths:
                payload = json.loads(draft_path.read_text(encoding="utf-8"))
                gate = review_draft_payload_stub(draft_payload=payload)
                self.assertEqual(gate.gate_status, "manual_review")
                self.assertEqual(gate.violations, [])

            eval_root = tmp / "continuation"
            shutil.copytree(Path(".solution/continuation/evals"), eval_root / "evals")
            report = run_fixture_contract_eval(eval_root)
            report_path = eval_root / "reports" / "p0b_pipeline_report.json"
            write_result = write_fixture_contract_eval_report(report, report_path)
            self.assertTrue(write_result["written"])
            self.assertEqual(report_path.read_text(encoding="utf-8").splitlines()[0][0], "{")
            self.assertTrue(write_result["status"] in {"passed", "failed"})
            self._assert_inbox_proposed_only(tmp)
            self.assertFalse((tmp / "registry.json").exists())

    def test_active_inbox_remains_isolated_across_pipeline_and_lifecycle_paths(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            active_inbox_target = tmp / "tasks" / "inbox" / "active_task.md"
            active_inbox_target.parent.mkdir(parents=True, exist_ok=True)
            active_inbox_target.write_text("# stable active inbox target\n", encoding="utf-8")

            # Success pipeline path with staged drafts.
            result_selected = tmp / "results" / "source_task_active_success"
            worker_out = result_selected / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: success\nboundary_f1_gain: 0.12\n", encoding="utf-8")
            (result_selected / "decision_summary.json").write_text(
                '{"category":"success_but_weak_evidence"}\n',
                encoding="utf-8",
            )
            pipeline_selected = run_local_continuation_pipeline("source_task_active_success", result_selected)
            self.assertEqual(pipeline_selected["status"], "selected")

            # No-action pipeline path when planner should not propose continuations.
            result_no_action = tmp / "results" / "source_task_active_no_action"
            result_no_action.mkdir(parents=True)
            (result_no_action / "result.yaml").write_text("status: failed\n", encoding="utf-8")
            pipeline_no_action = run_local_continuation_pipeline("source_task_active_no_action", result_no_action)
            self.assertEqual(pipeline_no_action["status"], "no_action")

            # Blocked path after operator reject.
            record = ProposedTaskRecord(
                proposed_task_id="proposed_blocked_active",
                state="drafted",
                source_task_id="source_task_active_blocked",
                source_result_hash="hash_blocked",
                idempotency_key="source_task_active_blocked:hash_blocked:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            reject_record_stub(record, reason="operator rejected previous proposal")
            result_blocked = tmp / "results" / "source_task_active_blocked"
            result_blocked.mkdir(parents=True)
            (result_blocked / "result.yaml").write_text("status: failed\n", encoding="utf-8")
            pipeline_blocked = run_local_continuation_pipeline(
                "source_task_active_blocked",
                result_blocked,
                force_reproposal=False,
            )
            self.assertEqual(pipeline_blocked["status"], "blocked")
            self.assertIn("blocked_by_terminal_lifecycle_action", pipeline_blocked["blocked_reasons"])

            # Approve/revoke/expire lifecycle should only emit audit entries and avoid active inbox writes.
            record_for_lifecycle = ProposedTaskRecord(
                proposed_task_id="proposed_lifecycle_active",
                state="drafted",
                source_task_id="source_task_active_lifecycle",
                source_result_hash="hash_active_lifecycle",
                idempotency_key="source_task_active_lifecycle:hash_active_lifecycle:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            approved = approve_record_stub(record_for_lifecycle, reason="operator approved staging")
            revoked = revoke_record_stub(record_for_lifecycle, reason="operator revoked stale lifecycle record")
            expired = expire_record_stub(record_for_lifecycle, reason="proposal context expired")
            self.assertTrue(approved["audit"]["written"])
            self.assertTrue(revoked["audit"]["written"])
            self.assertTrue(expired["audit"]["written"])

            active_files = {
                path.relative_to(tmp / "tasks" / "inbox").as_posix()
                for path in (tmp / "tasks" / "inbox").rglob("*.md")
            }
            self.assertIn("active_task.md", active_files)
            self.assertTrue(all(
                item.startswith("_proposed/")
                or item == "active_task.md"
                for item in active_files
            ))

            self.assertFalse((tmp / "tasks" / "inbox" / "active_task.md").read_text(encoding="utf-8") == "")

    def test_local_pipeline_composes_interpret_plan_stage_gate_without_active_inbox_writes(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_016"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: success\nboundary_f1_gain: 0.12\n", encoding="utf-8")
            (result_dir / "decision_summary.json").write_text('{"category":"success_but_weak_evidence"}\n', encoding="utf-8")

            pipeline = run_local_continuation_pipeline("source_task_016", result_dir, force_reproposal=False)
            stage = pipeline["stage"]

            self.assertEqual(pipeline["status"], "selected")
            self.assertFalse(pipeline["blocked_reasons"])
            self.assertIn("bounded_probe", pipeline["selected"])
            self.assertIn("decision_synthesis", pipeline["selected"])
            self.assertIn("manifest_path", stage)
            self.assertIn("proposal_json_path", stage)
            self.assertIn("report_path", pipeline)

            self.assertTrue(Path(stage["manifest_path"]).exists())
            self.assertTrue(Path(stage["proposal_markdown_path"]).exists())
            self.assertTrue(Path(stage["proposal_json_path"]).exists())

            self._assert_inbox_proposed_only(tmp)
            active_inbox_target = tmp / "tasks" / "inbox" / "unexpected.md"
            self.assertFalse(active_inbox_target.exists())
            self.assertFalse((tmp / "registry.json").exists())

    def test_pipeline_blocks_source_repair_bounded_staging_when_progression_disallows(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_repair_blocked"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "action_requests.json").write_text(
                json.dumps(
                    [
                        {
                            "kind": "missing_capability",
                            "message": "Missing utility module required for this path.",
                            "next_owner": "codex",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (result_dir / "decision_summary.json").write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "reason": "missing_capability",
                        "next_action": "repair_missing_module_then_rerun",
                        "codex_remote_ready": False,
                        "workdir_is_git_worktree": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            pipeline = run_local_continuation_pipeline(
                "source_task_repair_blocked",
                result_dir,
                force_reproposal=False,
            )

            self.assertEqual(pipeline["status"], "blocked")
            self.assertEqual(pipeline["selected"], [])
            self.assertIn("line_progression_blocked", pipeline["blocked_reasons"])
            self.assertFalse(pipeline["written"])
            self.assertIsNone(pipeline["stage"])
            self.assertIsNone(pipeline["manifest_path"])
            self.assertIsNone(pipeline["proposal_json_path"])
            self.assertIsNone(pipeline["proposal_md_path"])
            self.assertFalse(pipeline["next_step_readiness"]["can_stage"])
            self.assertIn("remote_codex_unavailable", pipeline["next_step_readiness"]["blockers"])
            self.assertIn("remote_workdir_not_git_worktree", pipeline["next_step_readiness"]["blockers"])
            self.assertEqual(pipeline["proposal_count"], 0)
            self.assertEqual(pipeline["draft_count"], 0)
            self.assertFalse((tmp / "continuation" / "proposed").exists())
            self.assertFalse((tmp / "registry.json").exists())

    def test_pipeline_blocks_source_repair_when_repair_artifacts_are_invalid(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_repair_artifact_blocked"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "action_requests.json").write_text(
                json.dumps(
                    [
                        {
                            "kind": "missing_capability",
                            "message": "Missing utility module required for this path.",
                            "next_owner": "codex",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (result_dir / "decision_summary.json").write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "reason": "missing_capability",
                        "next_action": "repair_missing_module_then_rerun",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (result_dir / "repair_result.yaml").write_text(
                json.dumps(
                    {
                        "artifact_type": "repair_result",
                        "repair_id": "repair_001",
                        "patch_intent_validation_errors": ["required_test_downgrade"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (result_dir / "rerun_intent.yaml").write_text(
                json.dumps(
                    {
                        "artifact_type": "rerun_intent",
                        "repair_id": "repair_001",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            pipeline = run_local_continuation_pipeline("source_task_repair_artifact_blocked", result_dir, force_reproposal=False)

            self.assertEqual(pipeline["status"], "blocked")
            self.assertEqual(pipeline["selected"], [])
            self.assertIn("source_repair_artifact_invalid", pipeline["line_progression"]["reasons"])
            self.assertIn("repair_result_patch_intent_validation_errors", pipeline["line_progression"]["reasons"])
            self.assertIn("repair_result_patch_intent_validation:required_test_downgrade", pipeline["next_step_readiness"]["blockers"])
            self.assertIn("source_repair_artifact_invalid", pipeline["next_step_readiness"]["blockers"])
            self.assertFalse(pipeline["written"])
            self.assertIsNone(pipeline["stage"])
            self.assertIsNone(pipeline["manifest_path"])
            self.assertIsNone(pipeline["proposal_json_path"])
            self.assertFalse((tmp / "continuation" / "proposed").exists())
            self.assertFalse((tmp / "registry.json").exists())

    def test_pipeline_surfaces_b14_l74_artifact_readiness_summary_when_report_present(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            result_dir = tmp / "results" / "source_task_b14_readiness"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / "action_requests.json").write_text("[]\n", encoding="utf-8")
            (result_dir / "bbox_predictions.jsonl").write_text('{"image_id":"img_001","bbox":[0,0,1,1]}\n', encoding="utf-8")
            (result_dir / "evidence_payload.jsonl").write_text('{"image_id":"img_001","evidence":"sidecar"}\n', encoding="utf-8")
            (result_dir / "metrics.json").write_text("[]\n", encoding="utf-8")
            write_b14_l74_artifact_readiness_report(result_dir)
            self.assertTrue((result_dir / ARTIFACT_READINESS_REPORT_NAME).exists())
            (result_dir / "decision_summary.json").write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "reason": "missing_capability",
                        "next_action": "repair_missing_module_then_rerun",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            pipeline = run_local_continuation_pipeline(
                "source_task_b14_readiness",
                result_dir,
                force_reproposal=False,
            )

            readiness = pipeline["bounded_validation_readiness"]
            self.assertEqual(readiness["status"], "repair_once")
            self.assertEqual(readiness["present_valid_artifact_ids"], [
                "action_requests",
                "bbox_predictions",
                "evidence_payload",
            ])
            self.assertEqual(readiness["missing_artifact_ids"], ["closure_decision"])
            self.assertEqual(readiness["invalid_artifact_ids"], ["metrics"])
            self.assertEqual(
                pipeline["line_progression"]["bounded_validation_readiness"],
                readiness,
            )
            self.assertEqual(
                pipeline["next_step_readiness"]["bounded_validation"],
                readiness,
            )

    def test_pipeline_blocks_reproposal_after_reject_without_force(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            record = ProposedTaskRecord(
                proposed_task_id="proposed_block_001",
                state="drafted",
                source_task_id="source_task_017",
                source_result_hash="hash_017",
                idempotency_key="source_task_017:hash_017:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            reject_record_stub(record, reason="operator rejected unsafe proposal")

            result_dir = tmp / "results" / "source_task_017"
            result_dir.mkdir(parents=True)
            (result_dir / "decision_summary.json").write_text('{"category":"missing_artifact"}\n', encoding="utf-8")

            pipeline = run_local_continuation_pipeline("source_task_017", result_dir, force_reproposal=False)
            self.assertEqual(pipeline["status"], "blocked")
            self.assertIn("blocked_by_terminal_lifecycle_action", pipeline["blocked_reasons"])
            self.assertFalse(pipeline["written"])
            self._assert_inbox_proposed_only(tmp)

    def test_pipeline_reports_selected_no_action_and_blocked_paths_without_registry_or_active_inbox_writes(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            # selected path: success but weak evidence should route into bounded follow-up/intents.
            selected_root = tmp / "results" / "sel"
            worker_out = selected_root / "_worker_out"
            worker_out.mkdir(parents=True)
            (selected_root / "result.yaml").write_text("status: success\nboundary_f1_gain: 0.12\n", encoding="utf-8")

            selected_pipeline = run_local_continuation_pipeline("sel", selected_root, force_reproposal=False)
            self.assertEqual(selected_pipeline["status"], "selected")
            self.assertEqual(selected_pipeline["selected"], ["bounded_probe", "decision_synthesis"])
            self.assertEqual(selected_pipeline["blocked_reasons"], [])
            self.assertTrue(Path(selected_pipeline["manifest_path"]).exists())
            self.assertIn("report_path", selected_pipeline)
            self.assertTrue(selected_pipeline["report_path"].endswith("sel_pipeline_report.json"))
            self.assertTrue(any(item["name"] == "scheduler_dry_run_no_dispatch" for item in selected_pipeline["checks"]))
            self.assertTrue(selected_pipeline["written"])

            # no-action path: unknown category should return no action without block reasons.
            no_action_root = tmp / "results" / "na"
            no_action_root.mkdir(parents=True)
            (no_action_root / "decision_summary.json").write_text('{"category":"not_a_known_category"}\n', encoding="utf-8")

            no_action_pipeline = run_local_continuation_pipeline("na", no_action_root, force_reproposal=False)
            self.assertEqual(no_action_pipeline["status"], "no_action")
            self.assertEqual(no_action_pipeline["selected"], [])
            self.assertEqual(no_action_pipeline["blocked_reasons"], [])
            self.assertTrue(no_action_pipeline["written"])

            # blocked path: terminal lifecycle action should hard-block reproposal.
            blocked_record = ProposedTaskRecord(
                proposed_task_id="proposed_block_004",
                state="drafted",
                source_task_id="blk",
                source_result_hash="hash_blk",
                idempotency_key="blk:hash_blk:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            reject_record_stub(blocked_record, reason="operator manually rejected prior proposal")

            blocked_root = tmp / "results" / "blk"
            blocked_root.mkdir(parents=True)
            (blocked_root / "decision_summary.json").write_text('{"category":"missing_artifact"}\n', encoding="utf-8")
            blocked_pipeline = run_local_continuation_pipeline("blk", blocked_root, force_reproposal=False)
            self.assertEqual(blocked_pipeline["status"], "blocked")
            self.assertIn("blocked_by_terminal_lifecycle_action", blocked_pipeline["blocked_reasons"])
            self.assertIn("report_path", blocked_pipeline)

            self.assertFalse((tmp / "registry.json").exists())
            active_inbox = tmp / "tasks" / "inbox"
            active_markdowns = [
                path.relative_to(active_inbox).as_posix()
                for path in active_inbox.rglob("*.md")
                if path.is_file()
            ]
            self.assertTrue(all(item.startswith("_proposed/") for item in active_markdowns))

    def test_pipeline_force_reproposal_regenerates_after_reject(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            record = ProposedTaskRecord(
                proposed_task_id="proposed_block_002",
                state="drafted",
                source_task_id="source_task_018",
                source_result_hash="hash_018",
                idempotency_key="source_task_018:hash_018:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            reject_record_stub(record, reason="operator rejected then forced reset")

            result_dir = tmp / "results" / "source_task_018"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: partial\n", encoding="utf-8")

            pipeline = run_local_continuation_pipeline("source_task_018", result_dir, force_reproposal=True)
            self.assertEqual(pipeline["status"], "no_action")
            self.assertFalse(pipeline["blocked_reasons"])
            self.assertTrue(pipeline["written"])
            self._assert_inbox_proposed_only(tmp)

    def test_pipeline_force_reproposal_regenerates_after_revoke(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            record = ProposedTaskRecord(
                proposed_task_id="proposed_block_003",
                state="drafted",
                source_task_id="source_task_019",
                source_result_hash="hash_019",
                idempotency_key="source_task_019:hash_019:0.1.0:stage",
                prompt_versions={"continuation_planner": "0.1.0"},
                policy_version="0.1.0",
                created_at="2026-05-17T09:47:21+08:00",
                created_by="continuation_planner",
            )
            revoke_record_stub(record, reason="operator revoked stale proposal")

            result_dir = tmp / "results" / "source_task_019"
            worker_out = result_dir / "_worker_out"
            worker_out.mkdir(parents=True)
            (worker_out / "result.yaml").write_text("status: partial\n", encoding="utf-8")

            pipeline = run_local_continuation_pipeline("source_task_019", result_dir, force_reproposal=True)
            self.assertEqual(pipeline["status"], "no_action")
            self.assertFalse(pipeline["blocked_reasons"])
            self.assertTrue(pipeline["written"])
            self._assert_inbox_proposed_only(tmp)

    def test_proposal_bundle_markdown_has_metabolizer_front_matter_shape(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="source_task_019",
                source_result_hash="hash_019",
                idempotency_key="source_task_019:hash_019:0.1.0:interpret",
                category="missing_capability",
                recommended_continuation_kinds=["missing_capability_request"],
                evidence={"missing_capability_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            metadata = _parse_front_matter(Path(staged["proposal_markdown_path"]).read_text(encoding="utf-8"))
            required_front = {
                "task_id",
                "title",
                "status",
                "priority",
                "worker",
                "created_at",
                "source_task_id",
                "source_result_hash",
                "plan_id",
                "proposal_id",
                "bundle_id",
                "forbidden_actions",
                "outputs",
                "claim_limitation",
                "drafts_in_bundle",
            }
            self.assertTrue(required_front <= set(metadata))
            self.assertEqual(metadata["status"], "pending")
            self.assertEqual(metadata["priority"], "normal")
            self.assertEqual(metadata["source_task_id"], "source_task_019")
            self.assertIn("task_id", metadata["drafts_in_bundle"][0])
            self._assert_inbox_proposed_only(tmp)

    def test_proposal_front_matter_is_parseable_and_bundle_stays_invisible_until_promoted(self):
        with WorkspaceTempDir() as tmp, self._patch_workspace(tmp):
            interpretation = ResultInterpretation(
                source_task_id="invisible_001",
                source_result_hash="hash_invisible_001",
                idempotency_key="invisible_001:hash_invisible_001:0.1.0:interpret",
                category="missing_artifact",
                recommended_continuation_kinds=["inventory_audit"],
                evidence={"missing_artifact_detected": True},
            )
            plan = plan_deterministic_stub(interpretation)
            staged = stage_plan_stub(plan)

            proposal_markdown = Path(staged["proposal_markdown_path"]).read_text(encoding="utf-8")
            metadata = _parse_front_matter(proposal_markdown)
            self.assertIsInstance(metadata, dict)
            required_front = {
                "task_id",
                "title",
                "status",
                "priority",
                "worker",
                "source_task_id",
                "source_result_hash",
                "plan_id",
                "proposal_id",
                "bundle_id",
                "forbidden_actions",
                "outputs",
                "claim_limitation",
                "drafts_in_bundle",
            }
            self.assertTrue(required_front <= set(metadata))

            scan = core.scan_inbox_staging()
            paths = [str(Path(item["path"]).resolve()) for item in scan.get("lines", [])]
            self.assertNotIn(str(Path(staged["path"]).resolve()), paths)
            self.assertTrue(all("_proposed" not in path for path in paths))

            # Simulate explicit promotion by copying bundle markdown into a regular inbox container.
            promoted = tmp / "tasks" / "inbox" / "visible_batch"
            promoted.mkdir(parents=True)
            (promoted / "TASK_BATCH_INDEX.md").write_text("# visible batch", encoding="utf-8")
            visible_task = promoted / "proposed_bundle.md"
            visible_task.write_text(
                "---\n"
                "task_id: visible_bundle\n"
                "title: Visible proposal\n"
                "status: pending\n"
                "priority: normal\n"
                "worker: continuation_planner\n"
                "source_task_id: invisible_001\n"
                "source_result_hash: hash_invisible_001\n"
                "plan_id: invisible_001__deterministic_plan\n"
                "proposal_id: invisible_001__proposals\n"
                "bundle_id: invisible_001__invisible_001__deterministic_plan\n"
                "forbidden_actions: [direct_dispatch]\n"
                "outputs: [proposal.json]\n"
                "claim_limitation: placeholder\n"
                "drafts_in_bundle: []\n"
                "---\n\n# Visible task\n",
                encoding="utf-8",
            )
            scan_after_promotion = core.scan_inbox_staging()
            promoted_paths = [str(Path(item["path"]).resolve()) for item in scan_after_promotion.get("lines", [])]
            self.assertIn(str(promoted.resolve()), promoted_paths)


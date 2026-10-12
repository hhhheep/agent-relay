from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation import knowledge_to_proposal, proposal_staging, paths as continuation_paths


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    source_root = Path(".solution/continuation")
    if source_root.exists():
        for name in [
            "planner_policy.yaml",
            "p0c_policy.yaml",
            "run_ledger_schema.yaml",
            "permission_matrix.yaml",
            "knowledge_pack_policy.yaml",
            "knowledge_pack_schema.yaml",
            "knowledge_pack_manifest_schema.yaml",
            "api_council_policy.yaml",
            "api_council_schema.yaml",
        ]:
            source = source_root / name
            if source.exists():
                target = tmp_path / ".solution" / "continuation" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _candidate(
    *,
    kind: str = "bounded_probe",
    candidate_id: str | None = "ps_candidate_01",
    can_stage: bool = True,
    requires_human_approval: bool = False,
    side_effects: dict[str, bool] | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "source_council_id": "ac_test_01",
        "source_task_id": "task_001",
        "source_knowledge_pack_id": "kp_001",
        "kind": kind,
        "worker": "small_task_worker",
        "line": "L1",
        "workplace_profile": "line-a",
        "reason": f"{kind} review request",
        "risk_flags": [],
        "claim_limitations": ["Bounded advisory follow-up only."],
        "expected_outputs": ["bounded_probe_report.md", "action_requests.json"],
        "forbidden_actions": ["direct_dispatch", "scheduler_submit"],
        "status": "proposal_candidate_created",
        "can_stage": can_stage,
        "requires_human_approval": requires_human_approval,
        "side_effects": {
            "dispatch": False,
            "promote": False,
            "registry_mutation": False,
            "remote_execution": False,
            "running_state": False,
            "subprocess": False,
            "scheduler_submit": False,
            "remote_write": False,
            "source_edit": False,
            "write_inbox": False,
        },
    }
    if side_effects:
        payload["side_effects"].update(side_effects)
    candidate = knowledge_to_proposal.validate_proposal_candidate(payload)
    if side_effects:
        candidate["side_effects"].update(side_effects)
    return candidate


def test_valid_bounded_probe_candidate_stages_to_staging_path(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert result["staged"] is True
    assert result["created"] is True
    assert result["replayed"] is False
    assert result["staging_report_path"].endswith("staging_report.json")
    assert Path(result["staging_report_path"]).exists()
    assert result["draft_path"] is not None and Path(result["draft_path"]).exists()
    assert result["markdown_path"] is not None and Path(result["markdown_path"]).exists()
    report_path = Path(result["staging_report_path"])
    assert report_path.parent == continuation_paths.continuation_staged_proposal_candidate_dir(
        result["proposal_run_id"], result["candidate_id"]
    )


def test_no_safe_continuation_is_skipped_without_stage_bundle(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="no_safe_continuation", can_stage=False)
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] in {"manual_review", "blocked"}
    assert result["staged"] is False
    assert result["draft_path"] is None or not Path(result["draft_path"]).exists()
    assert Path(result["staging_report_path"]).exists()
    assert "no_safe_continuation_not_stageable" in result["reasons"]


def test_next_step_readiness_blockers_prevent_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["next_step_readiness"] = {
        "can_stage": False,
        "blockers": ["source_repair_plan_missing"],
    }
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert not result["draft_path"] or not Path(result["draft_path"]).exists()
    assert any(
        reason.startswith("next_step_readiness_blocker:source_repair_plan_missing")
        for reason in result["reasons"]
    )


def test_staging_preserves_b14_l74_bounded_validation_summary_on_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["next_step_readiness"] = {
        "schema_version": 1,
        "can_stage": True,
        "route": "BOUNDED_SELF_REPAIR",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "REPAIR_ONCE",
            "next_recommendation": "REPAIR_ONCE",
            "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["closure_decision"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "VALID",
                "closure_decision": "INVALID",
            },
        },
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert result["candidate"]["next_step_readiness"]["can_stage"] is True
    assert result["candidate"]["next_step_readiness"]["bounded_validation"] == {
        "bucket_id": "B14-L74",
        "status": "repair_once",
        "next_recommendation": "repair_once",
        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
        "missing_artifact_ids": ["action_requests"],
        "invalid_artifact_ids": ["closure_decision"],
        "route_reasons": ["artifact_repair_once_possible"],
        "artifact_statuses": {
            "metrics": "valid",
            "closure_decision": "invalid",
        },
    }


def test_repair_result_validation_errors_block_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["repair_result"] = {
        "artifact_type": "repair_result",
        "repair_id": "red_b14_l72_001",
        "patch_intent_validation_errors": ["patch_intent_validation:target_file_is_required"],
    }
    candidate["rerun_intent"] = {
        "artifact_type": "rerun_intent",
        "repair_id": "red_b14_l72_001",
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert not result["draft_path"] or not Path(result["draft_path"]).exists()
    assert "candidate_repair_result_has_patch_intent_validation_errors" in result["reasons"]
    assert any(
        reason.startswith("repair_result_patch_intent_validation:patch_intent_validation:target_file_is_required")
        for reason in result["reasons"]
    )


def test_repair_result_execution_blocked_blocks_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["repair_result"] = {
        "artifact_type": "repair_result",
        "repair_id": "red_b14_l72_001",
        "execution_status": "blocked",
        "execution_blockers": ["self_repair_apply_validation:anchor_not_found"],
    }
    candidate["rerun_intent"] = {
        "artifact_type": "rerun_intent",
        "repair_id": "red_b14_l72_001",
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert not result["draft_path"] or not Path(result["draft_path"]).exists()
    assert "candidate_repair_result_execution_blocked" in result["reasons"]
    assert "repair_result_execution:self_repair_apply_validation:anchor_not_found" in result["reasons"]


def test_repair_result_execution_status_invalid_blocks_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["repair_result"] = {
        "artifact_type": "repair_result",
        "repair_id": "red_b14_l72_001",
        "execution_status": "Not_Supported",
        "execution_blockers": ["self_repair_apply_validation:unsupported_route"],
    }
    candidate["rerun_intent"] = {
        "artifact_type": "rerun_intent",
        "repair_id": "red_b14_l72_001",
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert not result["draft_path"] or not Path(result["draft_path"]).exists()
    assert "candidate_repair_result_execution_status_invalid:not_supported" in result["reasons"]
    assert "repair_result_execution_status:not_supported" in result["reasons"]


def test_apply_readiness_blocked_prevents_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["apply_readiness"] = {
        "route": "bounded_self_repair",
        "status": "blocked",
        "can_apply": False,
        "blockers": ["patch_intent_validation:target_file_is_required"],
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert not result["draft_path"] or not Path(result["draft_path"]).exists()
    assert "candidate_apply_readiness_blocked" in result["reasons"]
    assert any(
        reason.startswith("candidate_apply_readiness_blocker:patch_intent_validation:target_file_is_required")
        for reason in result["reasons"]
    )


def test_apply_readiness_route_is_normalized_case(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["apply_readiness"] = {
        "route": "BOUNDED_SELF_REPAIR",
        "status": "ready",
        "can_apply": True,
        "blockers": [],
    }
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert result["candidate"]["apply_readiness"]["route"] == "bounded_self_repair"
    assert not any(reason.startswith("candidate_apply_readiness") for reason in result["reasons"])


def test_repair_artifact_fields_are_normalized_before_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["next_step_readiness"] = {
        "schema_version": 1,
        "can_stage": True,
        "route": "BOUNDED_SELF_REPAIR",
        "blockers": [],
    }
    candidate["repair_result"] = {
        "artifact_type": "repair_result",
        "repair_id": "red_mixed_case_001",
        "execution_status": "Applied",
        "execution_blockers": [],
        "route": "BOUNDED_SELF_REPAIR",
        "repair_type": "Registry_Expected_Metrics_Missing",
    }
    candidate["rerun_intent"] = {
        "artifact_type": "rerun_intent",
        "repair_id": "red_mixed_case_001",
        "route": "BOUNDED_SELF_REPAIR",
        "repair_type": "Registry_Expected_Metrics_Missing",
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert result["candidate"]["repair_result"]["route"] == "bounded_self_repair"
    assert result["candidate"]["repair_result"]["repair_type"] == "registry_expected_metrics_missing"
    assert result["candidate"]["repair_result"]["execution_status"] == "applied"
    assert result["candidate"]["rerun_intent"]["route"] == "bounded_self_repair"
    assert result["candidate"]["rerun_intent"]["repair_type"] == "registry_expected_metrics_missing"


def test_repair_artifact_paths_are_normalized_before_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    candidate = _candidate(kind="bounded_probe")
    candidate["next_step_readiness"] = {
        "schema_version": 1,
        "can_stage": True,
        "route": "BOUNDED_SELF_REPAIR",
        "blockers": [],
    }

    repair_result_path = Path("repair_result.json")
    rerun_intent_path = Path("rerun_intent.json")
    repair_result_path.write_text(
        json.dumps(
            {
                "artifact_type": "repair_result",
                "repair_id": "red_path_case_001",
                "execution_status": "Applied",
                "execution_blockers": [],
                "route": "BOUNDED_SELF_REPAIR",
                "repair_type": "Registry_Expected_Metrics_Missing",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    rerun_intent_path.write_text(
        json.dumps(
            {
                "artifact_type": "rerun_intent",
                "repair_id": "red_path_case_001",
                "route": "BOUNDED_SELF_REPAIR",
                "repair_type": "Registry_Expected_Metrics_Missing",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    candidate["repair_result"] = str(repair_result_path)
    candidate["rerun_intent"] = str(rerun_intent_path)

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert result["candidate"]["repair_result"]["route"] == "bounded_self_repair"
    assert result["candidate"]["repair_result"]["repair_type"] == "registry_expected_metrics_missing"
    assert result["candidate"]["repair_result"]["execution_status"] == "applied"
    assert result["candidate"]["rerun_intent"]["route"] == "bounded_self_repair"
    assert result["candidate"]["rerun_intent"]["repair_type"] == "registry_expected_metrics_missing"


def test_repair_artifacts_require_readiness_before_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["repair_result"] = {
        "artifact_type": "repair_result",
        "repair_id": "red_missing_readiness_001",
    }
    candidate["rerun_intent"] = {
        "artifact_type": "rerun_intent",
        "repair_id": "red_missing_readiness_001",
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert result["staged"] is False
    assert "candidate_missing_next_step_readiness_for_repair_artifacts" in result["reasons"]


def test_direct_forbidden_action_blocks_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["dispatch"] = True
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert "forbidden_field:dispatch" in result["reasons"]
    assert result["staged"] is False
    assert result["draft_path"] is None or not Path(result["draft_path"]).exists()


def test_side_effect_true_blocks_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(
        kind="bounded_probe",
        side_effects={"dispatch": True},
    )
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "blocked"
    assert any("forbidden_side_effect_true:dispatch" in reason for reason in result["reasons"])
    assert result["staged"] is False
    assert result["draft_path"] is None or not Path(result["draft_path"]).exists()


def test_codex_debug_request_is_draft_only_and_no_source_edit_required(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="codex_debug_request", can_stage=False)
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] in {"draft_written", "replayed"}
    assert result["assessment"]["draft_only"] is True
    assert result["assessment"]["requires_human_approval"] is True
    assert result["staged"] is True
    assert result["draft"] is not None
    assert result["draft"]["worker"] == "codex"
    assert result["draft"]["forbidden_actions"]
    assert "source_edit" in result["draft"]["forbidden_actions"] or result["draft"]["requires_human_approval"]


def test_missing_expected_outputs_or_reason_or_claim_limits_manual_review(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = {
        "schema_version": 1,
        "source_task_id": "task_001",
        "kind": "bounded_probe",
        "worker": "small_task_worker",
        "line": "L1",
        "workplace_profile": "line-a",
        "status": "proposal_candidate_created",
        "can_stage": True,
        "requires_human_approval": False,
    }
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] in {"manual_review", "blocked"}
    assert any(reason.startswith("candidate_missing_reason") for reason in result["reasons"])
    assert any(reason.startswith("candidate_missing_expected_outputs") for reason in result["reasons"])
    assert any(reason.startswith("candidate_missing_claim_limitations") for reason in result["reasons"])
    assert result["staged"] is False


def test_staging_never_writes_active_tasks_inbox_md(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    inbox_root = tmp_path / "tasks" / "inbox"
    assert not any(path.suffix == ".md" for path in inbox_root.rglob("*"))
    assert not result["staging_report_path"].startswith(str(inbox_root))
    assert not (result["draft_path"] or "").startswith(str(inbox_root))


def test_night_auto_candidate_requires_mission_context_and_gate_pass(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"

    blocked = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")
    assert blocked["status"] == "blocked"
    assert "candidate_missing_mission_id" in blocked["reasons"]
    assert "candidate_missing_parent_decision_id" in blocked["reasons"]
    assert "candidate_night_auto_requires_mission_gate_pass" in blocked["reasons"]
    assert blocked.get("night_auto_work_order_path") is None

    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True
    allowed = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert allowed["status"] == "staged"
    assert allowed["staged"] is True
    assert allowed["assessment"]["candidate"]["mission_gate_pass"] is True
    assert allowed["night_auto_work_order_status"] in {"created", "replayed"}
    assert allowed["night_auto_work_order_path"] is not None
    assert Path(allowed["night_auto_work_order_path"]).exists()


def test_night_auto_missing_each_mission_gate_requirement_blocks_work_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    base_candidate = _candidate(kind="bounded_probe")
    base_candidate["continuation_lane"] = "night_auto"
    base_candidate["mission_id"] = "B14_night_research_v1"
    base_candidate["parent_decision_id"] = "B14_night_research_v1:root"
    base_candidate["mission_gate_pass"] = True

    missing_mission_id = dict(base_candidate)
    missing_mission_id.pop("mission_id")
    mission_missing = proposal_staging.stage_proposal_candidate_dry_run(
        missing_mission_id,
        actor="system",
    )
    assert mission_missing["status"] == "blocked"
    assert "candidate_missing_mission_id" in mission_missing["reasons"]
    assert mission_missing.get("night_auto_work_order_path") is None

    missing_parent = dict(base_candidate)
    missing_parent.pop("parent_decision_id")
    parent_missing = proposal_staging.stage_proposal_candidate_dry_run(
        missing_parent,
        actor="system",
    )
    assert parent_missing["status"] == "blocked"
    assert "candidate_missing_parent_decision_id" in parent_missing["reasons"]
    assert parent_missing.get("night_auto_work_order_path") is None

    mission_gate_blocked = dict(base_candidate)
    mission_gate_blocked["mission_gate_pass"] = False
    gate_blocked = proposal_staging.stage_proposal_candidate_dry_run(
        mission_gate_blocked,
        actor="system",
    )
    assert gate_blocked["status"] == "blocked"
    assert "candidate_night_auto_requires_mission_gate_pass" in gate_blocked["reasons"]
    assert gate_blocked.get("night_auto_work_order_path") is None


def test_night_auto_candidates_stay_in_dry_run_staging_area(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True

    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")
    assert result["status"] == "staged"
    assert result["staging_report_path"] is not None

    inbox_root = str((tmp_path / "tasks" / "inbox").resolve())
    report_path = Path(result["staging_report_path"]).resolve().as_posix()
    assert not report_path.startswith(inbox_root)
    assert "tasks/inbox/_night_auto" not in report_path


def test_night_auto_bounded_inventory_smoke_candidate_writes_dry_run_work_order(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True
    candidate["line"] = "L10"
    candidate["workplace_profile"] = "B14_profile_01"

    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_b14",
    )

    assert result["status"] == "staged"
    assert result["candidate"]["mission_id"] == "B14_night_research_v1"
    assert result["candidate"]["parent_decision_id"] == "B14_night_research_v1:root"
    assert result["candidate"]["mission_gate_pass"] is True
    assert result["night_auto_work_order_status"] in {"created", "replayed"}
    assert result["night_auto_work_order"] is not None
    work_order = result["night_auto_work_order"]
    assert work_order["mission_id"] == "B14_night_research_v1"
    assert work_order["parent_decision_id"] == "B14_night_research_v1:root"
    assert work_order["mission_gate_pass"] is True
    assert work_order["continuation_lane"] == "night_auto"
    assert work_order["intended_execution_type"] == "bounded_probe"
    assert work_order["claim_limits"]
    for limit in ("no_scheduler_dispatch", "no_active_inbox_write", "no_active_kb_mutation", "no_registry_mutation", "no_remote_api_call", "no_dataset_or_checkpoint_mutation"):
        assert limit in work_order["claim_limits"]
    assert all(not value for value in work_order["side_effects"].values())
    assert work_order["line"] == "L10"
    assert work_order["workplace_profile"] == "B14_profile_01"
    assert result["night_auto_work_order_path"] is not None
    assert Path(result["night_auto_work_order_path"]).exists()

    expected_dir = tmp_path / "outputs" / "night_runs" / "B14_night_research_v1" / "dry_run" / "pr_night_auto_b14" / result["candidate_id"]
    assert Path(result["night_auto_work_order_path"]).parent == expected_dir
    work_order_payload = json.loads(Path(result["night_auto_work_order_path"]).read_text(encoding="utf-8"))
    assert work_order_payload["work_order_id"] == work_order["work_order_id"]


def test_night_auto_work_order_preserves_custom_claim_limits_and_source_decision_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True
    candidate["source_decision_id"] = "B14_night_research_v1:decision:smoke"
    candidate["claim_limits"] = [
        "mission_metadata_required",
        "traceable_metadata_required",
    ]

    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_claim_limits",
    )

    assert result["status"] == "staged"
    assert result["night_auto_work_order"] is not None
    work_order = result["night_auto_work_order"]

    assert set(work_order["claim_limits"]) == set(candidate["claim_limits"])
    assert work_order["source_decision"]["decision_id"] == candidate["source_decision_id"]


def test_night_auto_work_order_preserves_b14_l74_next_step_readiness_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True
    candidate["next_step_readiness"] = {
        "schema_version": 1,
        "can_stage": True,
        "route": "BOUNDED_SELF_REPAIR",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "REPAIR_ONCE",
            "next_recommendation": "REPAIR_ONCE",
            "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["closure_decision"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "VALID",
                "closure_decision": "INVALID",
            },
        },
    }

    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_readiness",
    )

    assert result["status"] == "staged"
    work_order = result["night_auto_work_order"]
    assert work_order is not None
    assert work_order["source_decision"]["next_step_readiness"] == {
        "schema_version": 1,
        "can_stage": True,
        "route": "bounded_self_repair",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once",
            "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["closure_decision"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "valid",
                "closure_decision": "invalid",
            },
        },
    }


def test_night_auto_candidate_writes_not_scheduler_visible_pending_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True

    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_pending",
    )

    assert result["status"] == "staged"
    assert result["night_auto_pending_status"] in {"created", "replayed"}
    assert result["night_auto_pending_artifact_path"] is not None
    pending_path = Path(result["night_auto_pending_artifact_path"])
    assert pending_path.exists()
    pending = json.loads(pending_path.read_text(encoding="utf-8"))

    assert pending["mission_id"] == "B14_night_research_v1"
    assert pending["parent_decision_id"] == "B14_night_research_v1:root"
    assert pending["scheduler_visible"] is False
    assert pending["side_effects"]["scheduler_dispatch"] is False
    assert pending["side_effects"]["active_inbox_write"] is False
    assert pending["side_effects"]["registry_mutation"] is False
    assert pending["source_decision"]["mission_id"] == result["night_auto_work_order"]["source_decision"]["mission_id"]
    assert pending["work_order_id"] == result["night_auto_work_order"]["work_order_id"]
    expected_dir = (
        tmp_path
        / "outputs"
        / "night_runs"
        / "B14_night_research_v1"
        / "night_auto_pending"
        / "pr_night_auto_pending"
        / result["candidate_id"]
    )
    assert pending_path.parent == expected_dir


def test_night_auto_missing_mission_metadata_blocks_pending_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True
    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_no_mission_id",
    )

    assert result["status"] == "blocked"
    assert result["night_auto_work_order_path"] is None
    assert result["night_auto_pending_artifact_path"] is None
    assert "candidate_missing_mission_id" in result["reasons"]


def test_night_auto_active_inbox_path_not_used_for_pending_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True

    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_inbox",
    )

    assert result["status"] == "staged"
    assert result["night_auto_pending_artifact_path"] is not None
    assert "tasks/inbox/_night_auto" not in result["night_auto_pending_artifact_path"]
    inbox_root = (tmp_path / "tasks" / "inbox").resolve()
    assert not any(path.suffix == ".json" for path in inbox_root.rglob("*"))


def test_night_auto_pending_artifact_idempotency_and_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    candidate["continuation_lane"] = "night_auto"
    candidate["mission_id"] = "B14_night_research_v1"
    candidate["parent_decision_id"] = "B14_night_research_v1:root"
    candidate["mission_gate_pass"] = True

    first = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_collision_01",
    )
    second = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_night_auto_collision_01",
    )

    assert first["night_auto_pending_status"] in {"created", "replayed"}
    assert second["night_auto_pending_status"] == "replayed"
    assert second["night_auto_pending_artifact"] == first["night_auto_pending_artifact"]
    assert second["night_auto_pending_artifact_path"] == first["night_auto_pending_artifact_path"]

    candidate_alt = dict(candidate)
    candidate_alt["line"] = "L99"
    candidate_alt = knowledge_to_proposal.validate_proposal_candidate(candidate_alt)
    blocked = proposal_staging.stage_proposal_candidate_dry_run(
        candidate_alt,
        actor="system",
        proposal_run_id="pr_night_auto_collision_01",
    )
    assert blocked["status"] == "blocked"
    assert blocked["night_auto_pending_status"] == "blocked"


def test_staging_replay_with_same_candidate_is_safe(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")
    first = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system", proposal_run_id="pr_replay_01")
    second = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_replay_01",
    )

    assert first["created"] is True
    assert first["replayed"] is False
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["status"] == "replayed"
    assert second["staging_report_path"] == first["staging_report_path"]
    assert first["status"] == "staged"


def test_same_candidate_path_with_different_content_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe", candidate_id="stable_candidate_id")
    first = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system", proposal_run_id="pr_collision_01")
    assert first["created"] is True

    candidate_alt = dict(candidate)
    candidate_alt["reason"] = "Mutated reason triggers idempotency collision"
    candidate_alt = knowledge_to_proposal.validate_proposal_candidate(candidate_alt)
    blocked = proposal_staging.stage_proposal_candidate_dry_run(
        candidate_alt,
        actor="system",
        proposal_run_id="pr_collision_01",
    )

    assert blocked["status"] == "blocked"
    assert "idempotency_collision" in blocked["reasons"]
    assert blocked["created"] is False
    assert blocked["replayed"] is False
    assert json.loads(Path(first["staging_report_path"]).read_text(encoding="utf-8"))["candidate"]["reason"] != candidate_alt["reason"]


def test_staged_report_records_human_approval_requirement(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="codex_debug_request", can_stage=False)
    result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    report = json.loads(Path(result["staging_report_path"]).read_text(encoding="utf-8"))
    assert report["requires_human_approval"] is True


def test_no_subprocess_network_api_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    candidate = _candidate(kind="bounded_probe")

    with (
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.run") as kt_run_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.check_output") as kt_check_output_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.Popen") as kt_popen_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.socket.create_connection") as kt_create_connection_mock,
    ):
        kt_run_mock.side_effect = AssertionError("knowledge_to_proposal subprocess should not be called")
        kt_check_output_mock.side_effect = AssertionError("knowledge_to_proposal subprocess should not be called")
        kt_popen_mock.side_effect = AssertionError("knowledge_to_proposal subprocess should not be called")
        kt_create_connection_mock.side_effect = AssertionError("knowledge_to_proposal network should not be called")

        result = proposal_staging.stage_proposal_candidate_dry_run(candidate, actor="system")

    assert result["status"] == "staged"
    assert not kt_run_mock.called
    assert not kt_check_output_mock.called
    assert not kt_popen_mock.called
    assert not kt_create_connection_mock.called


def test_optional_ledger_stage_support_for_proposal_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    stageable = yaml.safe_load(
        Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8")
    )
    allowed_stages = set(stageable.get("allowed_stages", []))
    if "proposal_staging" not in allowed_stages:
        return

    candidate = _candidate(kind="bounded_probe")
    result = proposal_staging.stage_proposal_candidate_dry_run(
        candidate,
        actor="system",
        proposal_run_id="pr_ledger_01",
    )
    assert result["created"] is True
    ledger_path = tmp_path / "continuation" / "run_ledger" / "pr_ledger_01.jsonl"
    assert ledger_path.exists()
    events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(event.get("stage") == "proposal_staging" for event in events)

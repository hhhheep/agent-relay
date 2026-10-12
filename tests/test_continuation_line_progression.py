from __future__ import annotations

from pathlib import Path
import json
from solution_agent.continuation import line_progression
from solution_agent.continuation.line_progression import assess_line_progression


def test_missing_capability_continues_with_bounded_repair_then_rerun() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        action_requests=[
            {
                "kind": "missing_capability",
                "message": "Missing utils/b14_bbox_sidecar.py and --enable-bbox-sidecar.",
            },
        ],
        source_repair_plan={"needs_source_repair": True},
    )

    assert result["continue_allowed"] is True
    assert result["next_step"] == "continue_with_bounded_source_repair_then_rerun"
    assert result["next_step_readiness"]["can_stage"] is True
    assert result["next_step_readiness"]["blockers"] == []
    assert "source_repair_required" in result["reasons"]
    assert result["side_effects"]["source_edit"] is False
    assert result["side_effects"]["dispatch"] is False


def test_blocked_source_repair_plan_prompts_human_and_blocks_staging() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        source_repair_plan={
            "needs_source_repair": True,
            "blocked_by": ["remote_codex_unavailable", "remote_workdir_not_git_worktree"],
        },
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert result["requires_human"] is True
    assert result["next_step_readiness"]["can_stage"] is False
    assert result["next_step_readiness"]["blockers"] == [
        "remote_codex_unavailable",
        "remote_workdir_not_git_worktree",
    ]
    assert "source_repair_plan_blocked" in result["reasons"]


def test_request_codex_review_without_source_repair_plan_generates_repair_planning() -> None:
    result = assess_line_progression(
        source_task_id="exp_request_codex_review",
        decision_summary={"status": "partial", "reason": "request_codex_review", "next_action": "ask Codex"},
        action_requests=[{"kind": "request_codex_review", "message": "Source-code review needed before safe execution."}],
    )

    assert result["continue_allowed"] is True
    assert result["next_step"] == "continue_with_source_repair_planning"
    assert result["requires_human"] is False
    assert result["next_step_readiness"]["can_stage"] is True
    assert result["next_step_readiness"]["blockers"] == ["source_repair_plan_missing"]
    assert "source_repair_plan_missing" in result["reasons"]


def test_missing_repair_plan_continues_with_repair_planning() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        action_requests=[{"kind": "missing_capability", "message": "Missing module and flags."}],
    )

    assert result["continue_allowed"] is True
    assert result["next_step"] == "continue_with_source_repair_planning"
    assert result["next_step_readiness"]["can_stage"] is True
    assert result["next_step_readiness"]["blockers"] == ["source_repair_plan_missing"]
    assert "source_repair_plan_missing" in result["reasons"]


def test_repair_result_validation_errors_block_line_progression_readiness() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        source_repair_plan={"needs_source_repair": True},
        repair_result={"artifact_type": "repair_result", "repair_id": "red_001", "patch_intent_validation_errors": ["target_file_is_required"]},
        rerun_intent={"artifact_type": "rerun_intent", "repair_id": "red_001"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert result["next_step_readiness"]["can_stage"] is False
    assert "source_repair_artifact_invalid" in result["reasons"]
    assert "repair_result_patch_intent_validation_errors" in result["reasons"]
    assert "repair_result_patch_intent_validation:target_file_is_required" in result["reasons"]
    assert "repair_result_patch_intent_validation_errors" in result["next_step_readiness"]["blockers"]


def test_repair_artifact_id_mismatch_blocks_line_progression_readiness() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        source_repair_plan={"needs_source_repair": True},
        repair_result={"artifact_type": "repair_result", "repair_id": "red_001"},
        rerun_intent={"artifact_type": "rerun_intent", "repair_id": "red_002"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert "source_repair_artifact_invalid" in result["reasons"]
    assert "repair_result_rerun_id_mismatch" in result["reasons"]
    assert result["next_step_readiness"]["can_stage"] is False
    assert "repair_result_rerun_id_mismatch" in result["next_step_readiness"]["blockers"]


def test_repair_execution_blocked_status_blocks_line_progression_readiness() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        source_repair_plan={"needs_source_repair": True},
        repair_result={
            "artifact_type": "repair_result",
            "repair_id": "red_001",
            "route": "bounded_self_repair",
            "execution_status": "blocked",
            "execution_blockers": ["self_repair_apply_validation:anchor_not_found"],
            "patch_intent_validation_errors": [],
        },
        rerun_intent={"artifact_type": "rerun_intent", "repair_id": "red_001"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert result["next_step_readiness"]["can_stage"] is False
    assert "source_repair_artifact_invalid" in result["reasons"]
    assert "repair_result_execution_blocked" in result["reasons"]
    assert "repair_result_execution:self_repair_apply_validation:anchor_not_found" in result["reasons"]
    assert "repair_result_execution_blocked" in result["next_step_readiness"]["blockers"]
    assert "repair_result_execution:self_repair_apply_validation:anchor_not_found" in result["next_step_readiness"]["blockers"]


def test_repair_execution_invalid_status_blocks_line_progression_readiness() -> None:
    result = assess_line_progression(
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        decision_summary={"status": "partial", "reason": "missing_capability"},
        source_repair_plan={"needs_source_repair": True},
        repair_result={
            "artifact_type": "repair_result",
            "repair_id": "red_001",
            "route": "bounded_self_repair",
            "execution_status": "Not_Supported",
            "execution_blockers": ["self_repair_apply_validation:unsupported_route"],
            "patch_intent_validation_errors": [],
        },
        rerun_intent={"artifact_type": "rerun_intent", "repair_id": "red_001"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert result["next_step_readiness"]["can_stage"] is False
    assert "source_repair_artifact_invalid" in result["reasons"]
    assert "repair_result_execution_status_invalid:not_supported" in result["reasons"]
    assert "repair_result_execution_status:not_supported" in result["reasons"]
    assert "repair_result_execution_status_invalid:not_supported" in result["next_step_readiness"]["blockers"]
    assert "repair_result_execution_status:not_supported" in result["next_step_readiness"]["blockers"]


def test_repair_artifact_metadata_is_normalized_by_coercion() -> None:
    reasons: list[str] = []
    normalized = line_progression._coerce_repair_artifact(
        {
            "artifact_type": "repair_result",
            "route": "BOUNDED_SELF_REPAIR",
            "repair_type": "Wrapper_Missing_With_Template",
            "execution_status": "NOT_ATTEMPTED",
        },
        context="repair_result",
        reasons=reasons,
    )

    assert reasons == []
    assert normalized is not None
    assert normalized["route"] == "bounded_self_repair"
    assert normalized["repair_type"] == "wrapper_missing_with_template"
    assert normalized["execution_status"] == "not_attempted"


def test_repair_artifact_path_is_normalized_by_coercion(tmp_path: Path) -> None:
    repair_result_path = tmp_path / "repair_result.json"
    rerun_intent_path = tmp_path / "rerun_intent.json"

    repair_result_path.write_text(
        json.dumps(
            {
                "artifact_type": "repair_result",
                "route": "BOUNDED_SELF_REPAIR",
                "repair_type": "Wrapper_Missing_With_Template",
                "execution_status": "NOT_ATTEMPTED",
                "repair_id": "red_001",
                "execution_blockers": [],
                "patch_intent_validation_errors": [],
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
                "route": "BOUNDED_SELF_REPAIR",
                "repair_type": "Wrapper_Missing_With_Template",
                "repair_id": "red_001",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    repair_result = line_progression._coerce_repair_artifact(
        str(repair_result_path),
        context="repair_result",
        reasons=[],
    )
    rerun_intent = line_progression._coerce_repair_artifact(
        str(rerun_intent_path),
        context="rerun_intent",
        reasons=[],
    )

    assert repair_result is not None
    assert rerun_intent is not None
    assert repair_result["route"] == "bounded_self_repair"
    assert repair_result["repair_type"] == "wrapper_missing_with_template"
    assert repair_result["execution_status"] == "not_attempted"
    assert rerun_intent["route"] == "bounded_self_repair"
    assert rerun_intent["repair_type"] == "wrapper_missing_with_template"


def test_missing_artifact_continues_with_inventory_not_failure() -> None:
    result = assess_line_progression(
        source_task_id="a_rescorr_001_prepare_contract_inventory",
        decision_summary={"status": "partial", "reason": "missing_artifact"},
        action_requests=[{"kind": "missing_artifact", "message": "Detector cache missing."}],
    )

    assert result["continue_allowed"] is True
    assert result["next_step"] == "continue_with_inventory_or_metric_completion"
    assert "artifact_or_inventory_completion_required" in result["reasons"]


def test_bounded_validation_readiness_preserves_compact_artifact_summary() -> None:
    result = assess_line_progression(
        source_task_id="b14_l74_collect_judge",
        decision_summary={"status": "completed", "reason": "weak_metric", "next_action": "inspect readiness"},
        validation_readiness_report={
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once_collect_missing_artifacts",
            "reasons": [
                "artifact_repair_once_possible",
                "metric_evidence_missing_or_invalid",
                "metrics:invalid",
            ],
            "summary": {
                "present_valid_artifact_ids": ["bbox_predictions", "evidence_payload"],
                "missing_artifact_ids": ["closure_decision"],
                "invalid_artifact_ids": ["metrics"],
                "non_valid_artifact_ids": ["closure_decision", "metrics"],
                "route_reasons": [
                    "metric_evidence_missing_or_invalid",
                    "closure_decision_missing_or_invalid",
                ],
                "artifact_statuses": {
                    "metrics": "invalid",
                    "bbox_predictions": "present_valid",
                    "evidence_payload": "present_valid",
                    "closure_decision": "missing",
                    "action_requests": "present_valid",
                },
                "reason_count": 3,
            },
        },
    )

    readiness = result["bounded_validation_readiness"]
    assert readiness["bucket_id"] == "B14-L74"
    assert readiness["status"] == "repair_once"
    assert readiness["next_recommendation"] == "repair_once_collect_missing_artifacts"
    assert readiness["present_valid_artifact_ids"] == ["bbox_predictions", "evidence_payload"]
    assert readiness["missing_artifact_ids"] == ["closure_decision"]
    assert readiness["invalid_artifact_ids"] == ["metrics"]
    assert readiness["non_valid_artifact_ids"] == ["closure_decision", "metrics"]
    assert readiness["route_reasons"] == [
        "closure_decision_missing_or_invalid",
        "metric_evidence_missing_or_invalid",
    ]
    assert readiness["artifact_statuses"]["metrics"] == "invalid"
    assert readiness["artifact_statuses"]["closure_decision"] == "missing"
    assert readiness["reason_count"] == 3
    assert result["next_step_readiness"]["bounded_validation"] == readiness


def test_stageable_candidate_allows_bounded_continuation() -> None:
    result = assess_line_progression(
        source_task_id="line_task_001",
        decision_summary={"status": "completed", "reason": "weak_metric"},
        continuation_candidates=[
            {"kind": "bounded_probe", "can_stage": True},
            {"kind": "no_safe_continuation", "can_stage": False},
        ],
    )

    assert result["continue_allowed"] is True
    assert result["next_step"] == "continue_with_staged_continuation_candidate"


def test_completed_without_next_action_stops_as_line_complete_or_exhausted() -> None:
    result = assess_line_progression(
        source_task_id="line_task_final",
        decision_summary={"status": "completed", "recommended_use": "do_not_use"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "stop_line_complete_or_theory_exhausted"
    assert result["requires_human"] is False


def test_needs_user_decision_holds_for_human() -> None:
    result = assess_line_progression(
        source_task_id="line_task_unclear",
        decision_summary={"status": "partial", "reason": "needs_user_decision"},
    )

    assert result["continue_allowed"] is False
    assert result["next_step"] == "ask_human"
    assert result["requires_human"] is True

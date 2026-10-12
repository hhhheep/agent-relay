from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from solution_agent.cli import cli
from solution_agent.continuation.bounded_validation_bucket import (
    ARTIFACT_READINESS_REPORT_NAME,
    FIXTURE_PATH,
    SCHEMA_PATH,
    BoundedValidationBucket,
    assess_b14_l74_artifact_readiness,
    build_b14_l74_bounded_validation_bucket,
    load_bounded_validation_bucket,
    load_bounded_validation_bucket_schema,
    write_b14_l74_artifact_readiness_report,
    write_bounded_validation_bucket,
)


def test_bounded_validation_bucket_requires_contract_fields() -> None:
    with pytest.raises(ValidationError):
        BoundedValidationBucket(
            bucket_id="B14-L74",
            project_id="B14",
            mission_id="B14_night_research_v1",
            research_question="question",
        )


def test_b14_l74_bucket_fixture_matches_contract_and_claim_limits() -> None:
    schema = load_bounded_validation_bucket_schema(SCHEMA_PATH)
    bucket = load_bounded_validation_bucket(FIXTURE_PATH)

    payload = bucket.model_dump(mode="json")
    for field in schema["required_fields"]:
        assert field in payload

    assert bucket.bucket_id == "B14-L74"
    assert bucket.scope.max_images == 50
    assert bucket.budget.max_images == 50
    assert bucket.budget.timeout_minutes == 90
    assert bucket.baseline.path == "single_resize_448_current_path"
    assert bucket.variant.path == "sidecar_crop_retoken_path"
    assert "no_formal_iou_claim" in bucket.claim_limits
    assert "weak_metric_not_execution_failure" in bucket.claim_limits
    assert bucket.side_effects == schema["required_side_effects"]
    assert bucket.shared_inputs.p2_inputs_if_available is True
    assert bucket.shared_inputs.mask_inputs_if_available is True


def test_b14_l74_bucket_builder_round_trips_without_side_effect_expansion(tmp_path: Path) -> None:
    bucket = build_b14_l74_bounded_validation_bucket()

    written = write_bounded_validation_bucket(bucket, tmp_path / "bucket.json")

    assert written["status"] == "written"
    assert written["bucket"]["bucket_id"] == "B14-L74"
    assert written["bucket"]["side_effects"] == {
        "remote_execution": False,
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "canonical_kb_mutation": False,
    }
    assert "metrics.json" in written["bucket"]["expected_outputs"]
    assert "bbox_predictions.jsonl" in written["bucket"]["expected_outputs"]


def test_b14_l74_artifact_readiness_reports_complete_collect_judge_bundle(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.bucket_id == "B14-L74"
    assert report.status == "artifact_complete"
    assert report.next_recommendation == "continue_with_collect_judge_comparison"
    assert report.summary["non_valid_artifact_ids"] == []
    assert report.summary["route_reasons"] == []
    assert all(item.status == "present_valid" for item in report.artifact_checks)


def test_b14_l74_artifact_readiness_accepts_raw_list_action_requests_payload(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"completed"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text("[]\n", encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "artifact_complete"
    assert report.next_recommendation == "continue_with_collect_judge_comparison"
    action_check = next(item for item in report.artifact_checks if item.artifact_id == "action_requests")
    assert action_check.status == "present_valid"
    assert action_check.present_filename == "action_requests.json"


def test_b14_l74_artifact_readiness_accepts_l72_smoke_aliases_without_overclaiming(tmp_path: Path) -> None:
    (tmp_path / "large_image_smoke_metrics.csv").write_text(
        "variant,status,reason,max_images,candidate_count,formal_claim_allowed\n"
        "report_only_sidecar_interface,partial,report_only_sidecar_smoke_failed,300,0,False\n",
        encoding="utf-8",
    )
    (tmp_path / "bbox_predictions.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "bbox_evidence_payload.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial","next_action":"complete_l71"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text(
        '[{"kind":"needs_bounded_smoke_runner","missing_or_needed":"bounded smoke runner","why_needed":"sidecar smoke failed"}]\n',
        encoding="utf-8",
    )

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "pivot"
    assert report.next_recommendation == "pivot_without_sidecar_mechanism_evidence"
    metrics_check = next(item for item in report.artifact_checks if item.artifact_id == "metrics")
    bbox_check = next(item for item in report.artifact_checks if item.artifact_id == "bbox_predictions")
    evidence_check = next(item for item in report.artifact_checks if item.artifact_id == "evidence_payload")
    assert metrics_check.status == "present_valid"
    assert metrics_check.present_filename == "large_image_smoke_metrics.csv"
    assert bbox_check.status == "invalid"
    assert evidence_check.status == "invalid"
    assert evidence_check.present_filename == "bbox_evidence_payload.jsonl"
    assert "no_core_validation_artifacts_present" not in report.reasons
    assert "sidecar_evidence_payload_invalid" in report.reasons


def test_b14_l74_artifact_readiness_routes_malformed_closure_payload_to_repair_once(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"note":"diagnostic only"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "repair_once"
    assert report.next_recommendation == "repair_once_collect_missing_artifacts"
    assert "closure_decision:invalid" in report.reasons
    closure_check = next(item for item in report.artifact_checks if item.artifact_id == "closure_decision")
    assert closure_check.status == "invalid"
    assert closure_check.present_filename == "decision_summary.json"


def test_b14_l74_artifact_readiness_routes_missing_closure_to_repair_once(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "repair_once"
    assert report.next_recommendation == "repair_once_collect_missing_artifacts"
    assert "closure_decision:missing" in report.reasons
    assert "action_requests:missing" in report.reasons


def test_b14_l74_artifact_readiness_routes_missing_sidecar_evidence_to_pivot(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.yaml").write_text("status: partial\n", encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "pivot"
    assert report.next_recommendation == "pivot_without_sidecar_mechanism_evidence"
    assert "sidecar_evidence_missing_or_invalid" in report.reasons
    assert "evidence_payload:missing" in report.reasons
    assert report.summary["missing_artifact_ids"] == ["evidence_payload"]
    assert report.summary["invalid_artifact_ids"] == []
    assert report.summary["non_valid_artifact_ids"] == ["evidence_payload"]
    assert report.summary["route_reasons"] == ["sidecar_evidence_missing_or_invalid"]


def test_b14_l74_artifact_readiness_flags_invalid_metrics_payload_for_repair_once(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "repair_once"
    assert report.next_recommendation == "repair_once_collect_missing_artifacts"
    assert "artifact_repair_once_possible" in report.reasons
    assert "metric_evidence_missing_or_invalid" in report.reasons
    assert "metrics_payload_invalid" in report.reasons
    assert "metrics:invalid" in report.reasons
    assert report.summary["missing_artifact_ids"] == []
    assert report.summary["invalid_artifact_ids"] == ["metrics"]
    assert report.summary["non_valid_artifact_ids"] == ["metrics"]
    assert report.summary["route_reasons"] == ["metric_evidence_missing_or_invalid"]


def test_b14_l74_artifact_readiness_flags_invalid_bbox_payload_for_repair_once(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "repair_once"
    assert report.next_recommendation == "repair_once_collect_missing_artifacts"
    assert "artifact_repair_once_possible" in report.reasons
    assert "bbox_behavior_missing_or_invalid" in report.reasons
    assert "bbox_predictions_payload_invalid" in report.reasons
    assert "bbox_predictions:invalid" in report.reasons


def test_b14_l74_artifact_readiness_flags_invalid_sidecar_payload_for_pivot(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "decision_summary.yaml").write_text("status: partial\n", encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "pivot"
    assert report.next_recommendation == "pivot_without_sidecar_mechanism_evidence"
    assert "sidecar_evidence_missing_or_invalid" in report.reasons
    assert "sidecar_evidence_payload_invalid" in report.reasons
    assert "evidence_payload:invalid" in report.reasons


def test_b14_l74_artifact_readiness_stops_when_no_core_validation_artifacts_exist(tmp_path: Path) -> None:
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "stop"
    assert report.next_recommendation == "stop_no_collectable_validation_evidence"
    assert "no_core_validation_artifacts_present" in report.reasons


def test_b14_l74_artifact_readiness_distinguishes_invalid_core_artifacts_from_absent_core_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "metrics.json").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('["not-an-object"]\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    report = assess_b14_l74_artifact_readiness(tmp_path)

    assert report.status == "stop"
    assert report.next_recommendation == "stop_no_collectable_validation_evidence"
    assert "no_valid_core_validation_artifacts" in report.reasons
    assert "metrics_payload_invalid" in report.reasons
    assert "bbox_predictions_payload_invalid" in report.reasons
    assert "sidecar_evidence_payload_invalid" in report.reasons
    assert "no_core_validation_artifacts_present" not in report.reasons


def test_b14_l74_artifact_readiness_writer_persists_stable_local_report(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.json").write_text('{"status":"partial"}\n', encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    first = write_b14_l74_artifact_readiness_report(tmp_path)
    second = write_b14_l74_artifact_readiness_report(tmp_path)

    report_path = tmp_path / ARTIFACT_READINESS_REPORT_NAME
    assert first["created"] is True
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert first["report_path"] == str(report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "artifact_complete"
    assert payload["next_recommendation"] == "continue_with_collect_judge_comparison"


def test_b14_l74_artifact_readiness_cli_writes_report(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (tmp_path / "bbox_predictions.jsonl").write_text('{"image_id":"img-1"}\n', encoding="utf-8")
    (tmp_path / "evidence_payload.jsonl").write_text('{"image_id":"img-1","arm":"variant"}\n', encoding="utf-8")
    (tmp_path / "decision_summary.yaml").write_text("status: partial\n", encoding="utf-8")
    (tmp_path / "action_requests.json").write_text('{"action_requests":[]}\n', encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-l74-artifact-readiness",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "artifact_complete"
    assert payload["created"] is True
    assert payload["summary"]["non_valid_artifact_ids"] == []
    assert payload["summary"]["route_reasons"] == []
    assert Path(payload["report_path"]).exists()

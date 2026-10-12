from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from .schema import StrictModel


class ValidationScope(StrictModel):
    bucket_kind: str
    image_mode: str
    image_selector: str
    max_images: int = Field(gt=0)
    same_bucket_for_baseline_and_variant: bool = True
    allow_cached_inputs_only: bool = True
    dataset_authority_required: bool = True
    notes: list[str] = Field(default_factory=list)


class ValidationArm(StrictModel):
    arm_id: str
    path: str
    description: str
    report_only: bool = True
    keeps_same_inference_contract: bool = True
    expected_behavior: list[str] = Field(default_factory=list)


class SharedInputs(StrictModel):
    input_authority: str
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    p2_inputs_if_available: bool = True
    mask_inputs_if_available: bool = True
    sidecar_inputs_if_available: bool = True


class ValidationBudget(StrictModel):
    max_images: int = Field(gt=0)
    timeout_minutes: int = Field(gt=0)
    max_repair_attempts: int = Field(ge=0, default=1)
    max_remote_runs: int = Field(ge=0, default=0)
    write_active_inbox: bool = False
    scheduler_dispatch: bool = False


class ValidationMetric(StrictModel):
    metric_id: str
    description: str
    interpretation: str
    formal_claim: bool = False


class BoundedValidationBucket(StrictModel):
    schema_version: int = 1
    artifact_type: str = "bounded_validation_bucket"
    bucket_id: str
    project_id: str
    mission_id: str
    research_question: str
    scope: ValidationScope
    baseline: ValidationArm
    variant: ValidationArm
    shared_inputs: SharedInputs
    budget: ValidationBudget
    expected_outputs: list[str] = Field(default_factory=list)
    metrics: list[ValidationMetric] = Field(default_factory=list)
    claim_limits: list[str] = Field(default_factory=list)
    stop_rules: list[str] = Field(default_factory=list)
    continue_rules: list[str] = Field(default_factory=list)
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "remote_execution": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "canonical_kb_mutation": False,
        }
    )


class ValidationArtifactCheck(StrictModel):
    artifact_id: str
    category: str
    expected_filenames: list[str] = Field(default_factory=list)
    present_filename: str = ""
    status: str
    detail: str = ""


class BoundedValidationArtifactReport(StrictModel):
    schema_version: int = 1
    artifact_type: str = "bounded_validation_artifact_report"
    bucket_id: str
    output_dir: str
    artifact_checks: list[ValidationArtifactCheck] = Field(default_factory=list)
    status: str
    next_recommendation: str
    summary: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "remote_execution": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "canonical_kb_mutation": False,
        }
    )


SCHEMA_PATH = Path(".solution/continuation/bounded_validation_bucket_schema.yaml")
FIXTURE_PATH = Path(".solution/continuation/b14_l74_bounded_validation_bucket.yaml")
ARTIFACT_READINESS_REPORT_NAME = "b14_l74_artifact_readiness_report.json"
B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS: dict[str, list[str]] = {
    "metrics": ["metrics.json", "large_image_smoke_metrics.csv"],
    "bbox_predictions": ["bbox_predictions.jsonl"],
    "evidence_payload": ["evidence_payload.jsonl", "bbox_evidence_payload.jsonl"],
    "closure_decision": ["decision_summary.json", "decision_summary.yaml"],
    "action_requests": ["action_requests.json"],
}


def build_b14_l74_bounded_validation_bucket() -> BoundedValidationBucket:
    return BoundedValidationBucket(
        bucket_id="B14-L74",
        project_id="B14",
        mission_id="B14_night_research_v1",
        research_question=(
            "On one bounded large-image bucket, does the sidecar crop-retoken path "
            "reduce bbox uncertainty relative to the current single-resize 448 path "
            "without making a formal IoU claim?"
        ),
        scope=ValidationScope(
            bucket_kind="bounded_large_image_validation",
            image_mode="large_image",
            image_selector="same bounded large-image bucket for both arms",
            max_images=50,
            notes=[
                "B14-L74 is a bounded baseline-vs-sidecar validation bucket.",
                "No dataset mutation, no submission generation, no broad sweep.",
            ],
        ),
        baseline=ValidationArm(
            arm_id="baseline",
            path="single_resize_448_current_path",
            description="Current large-image bbox path without sidecar crop-retoken augmentation.",
            expected_behavior=[
                "Produces claim-limited bbox evidence on the shared bounded bucket.",
                "Acts as the calibration reference for sidecar comparison.",
            ],
        ),
        variant=ValidationArm(
            arm_id="variant",
            path="sidecar_crop_retoken_path",
            description="Large-image bbox path with sidecar crop-retoken augmentation on the same bucket.",
            expected_behavior=[
                "Shares the same bucket and shared inputs as baseline.",
                "May show proxy gain but cannot support a formal IoU claim on its own.",
            ],
        ),
        shared_inputs=SharedInputs(
            input_authority="B14 chronology + balanced large-image artifacts",
            required_inputs=[
                "same bounded image id list",
                "same large-image manifests",
                "same residual/mask/evidence inputs when available",
                "same P2 proposal inputs when available",
            ],
            optional_inputs=[
                "existing sidecar report-only helper outputs",
                "existing calibrated bbox decoder sidecar fields",
            ],
        ),
        budget=ValidationBudget(
            max_images=50,
            timeout_minutes=90,
            max_repair_attempts=1,
            max_remote_runs=0,
        ),
        expected_outputs=[
            "metrics.json",
            "bbox_predictions.jsonl",
            "evidence_payload.jsonl",
            "decision_summary.json",
            "action_requests.json",
            "stdout.log",
            "stderr.log",
        ],
        metrics=[
            ValidationMetric(
                metric_id="execution_path",
                description="Whether both arms completed the bounded validation path on the same bucket.",
                interpretation="Execution evidence only; failure to improve is not execution failure.",
            ),
            ValidationMetric(
                metric_id="bbox_proxy_quality",
                description="Claim-limited bbox proxy behavior comparison on the same bucket.",
                interpretation="Useful for direction choice only; no formal IoU promotion.",
            ),
            ValidationMetric(
                metric_id="artifact_completeness",
                description="Whether required metric and bbox evidence artifacts were emitted.",
                interpretation="Artifact absence must be classified separately from weak metrics.",
            ),
        ],
        claim_limits=[
            "no_formal_iou_claim",
            "proxy_evidence_only",
            "weak_metric_not_execution_failure",
            "p2_only_not_final_bbox",
            "no_active_inbox_write",
            "no_scheduler_dispatch",
            "no_registry_mutation",
            "kb_changes_proposal_only",
        ],
        stop_rules=[
            "stop_if_dataset_or_path_authorization_is_missing",
            "stop_if_bucket_scope_expands_beyond_bounded_large_image_validation",
            "stop_if_formal_claim_upgrade_is_requested",
        ],
        continue_rules=[
            "continue_only_if_both_arms_use_the_same_bounded_bucket_and_shared_inputs",
            "continue_if_proxy_gain_reduces_uncertainty_without_claim_upgrade",
            "continue_to_bounded_expansion_only_after_artifact-complete comparison evidence",
        ],
    )


def load_bounded_validation_bucket(path: Path | str) -> BoundedValidationBucket:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("bounded validation bucket fixture must contain a YAML object")
    return BoundedValidationBucket.model_validate(payload)


def load_bounded_validation_bucket_schema(path: Path | str | None = None) -> dict[str, object]:
    schema_path = Path(path or SCHEMA_PATH)
    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("bounded validation bucket schema must contain a YAML object")
    return payload


def write_bounded_validation_bucket(
    bucket: BoundedValidationBucket,
    output_path: Path | str,
) -> dict[str, object]:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = bucket.model_dump(mode="json")
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "written",
        "bucket_path": str(target),
        "bucket": payload,
        "side_effects": bucket.side_effects,
    }


def _load_json_object(path: Path) -> dict[str, Any] | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return payload


def _load_metrics_payload(path: Path) -> dict[str, Any] | None:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        if not records:
            return None
        return {"format": "csv", "records": records}
    return _load_json_object(path)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load_jsonl_records(path: Path) -> list[dict[str, Any]] | None:
    records: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            return None
        records.append(payload)
    if not records:
        return None
    return records


def _load_yaml_object(path: Path) -> dict[str, Any] | None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return payload


def _load_closure_payload(path: Path) -> dict[str, Any] | None:
    payload = _load_json_object(path) if path.suffix.lower() == ".json" else _load_yaml_object(path)
    if payload is None:
        return None
    for key in ("status", "recommended_use", "next_action"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return payload
    return None


def _load_action_requests_payload(path: Path) -> dict[str, Any] | list[dict[str, Any]] | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            return None
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("action_requests", "requests", "actions_required"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
        return None
    return payload


def _check_artifact(
    output_dir: Path,
    *,
    artifact_id: str,
    category: str,
    expected_filenames: list[str],
    loader: Any,
) -> ValidationArtifactCheck:
    present_path: Path | None = None
    for filename in expected_filenames:
        candidate = output_dir / filename
        if candidate.exists():
            present_path = candidate
            break
    if present_path is None:
        return ValidationArtifactCheck(
            artifact_id=artifact_id,
            category=category,
            expected_filenames=expected_filenames,
            status="missing",
            detail="artifact not found",
        )
    try:
        payload = loader(present_path)
    except (OSError, json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError, ValueError) as exc:
        return ValidationArtifactCheck(
            artifact_id=artifact_id,
            category=category,
            expected_filenames=expected_filenames,
            present_filename=present_path.name,
            status="invalid",
            detail=str(exc),
        )
    if payload is None:
        return ValidationArtifactCheck(
            artifact_id=artifact_id,
            category=category,
            expected_filenames=expected_filenames,
            present_filename=present_path.name,
            status="invalid",
            detail="artifact content did not match expected object/record shape",
        )
    return ValidationArtifactCheck(
        artifact_id=artifact_id,
        category=category,
        expected_filenames=expected_filenames,
        present_filename=present_path.name,
        status="present_valid",
    )


def _invalid_artifact_reason_codes(checks: list[ValidationArtifactCheck]) -> list[str]:
    reason_by_artifact_id = {
        "metrics": "metrics_payload_invalid",
        "bbox_predictions": "bbox_predictions_payload_invalid",
        "evidence_payload": "sidecar_evidence_payload_invalid",
        "closure_decision": "closure_decision_payload_invalid",
        "action_requests": "action_requests_payload_invalid",
    }
    return [
        reason_by_artifact_id[item.artifact_id]
        for item in checks
        if item.status == "invalid" and item.artifact_id in reason_by_artifact_id
    ]


def _route_reason_for_check(check: ValidationArtifactCheck) -> str:
    reason_by_artifact_id = {
        "metrics": "metric_evidence_missing_or_invalid",
        "bbox_predictions": "bbox_behavior_missing_or_invalid",
        "evidence_payload": "sidecar_evidence_missing_or_invalid",
        "closure_decision": "closure_decision_missing_or_invalid",
        "action_requests": "action_requests_missing_or_invalid",
    }
    return reason_by_artifact_id.get(check.artifact_id, "artifact_missing_or_invalid")


def _build_report_summary(
    checks: list[ValidationArtifactCheck],
    *,
    status: str,
    next_recommendation: str,
    reasons: list[str],
) -> dict[str, Any]:
    missing_ids = [item.artifact_id for item in checks if item.status == "missing"]
    invalid_ids = [item.artifact_id for item in checks if item.status == "invalid"]
    non_valid_ids = [item.artifact_id for item in checks if item.status != "present_valid"]
    route_reasons = [
        _route_reason_for_check(item)
        for item in checks
        if item.status != "present_valid"
    ]
    return {
        "status": status,
        "next_recommendation": next_recommendation,
        "present_valid_artifact_ids": [item.artifact_id for item in checks if item.status == "present_valid"],
        "missing_artifact_ids": missing_ids,
        "invalid_artifact_ids": invalid_ids,
        "non_valid_artifact_ids": non_valid_ids,
        "route_reasons": route_reasons,
        "artifact_statuses": {item.artifact_id: item.status for item in checks},
        "reason_count": len(reasons),
    }


def assess_b14_l74_artifact_readiness(
    output_dir: Path | str,
    *,
    bucket: BoundedValidationBucket | None = None,
) -> BoundedValidationArtifactReport:
    resolved_bucket = bucket or build_b14_l74_bounded_validation_bucket()
    resolved_output_dir = Path(output_dir)
    checks = [
        _check_artifact(
            resolved_output_dir,
            artifact_id="metrics",
            category="metric_evidence",
            expected_filenames=B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS["metrics"],
            loader=_load_metrics_payload,
        ),
        _check_artifact(
            resolved_output_dir,
            artifact_id="bbox_predictions",
            category="bbox_behavior_evidence",
            expected_filenames=B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS["bbox_predictions"],
            loader=_load_jsonl_records,
        ),
        _check_artifact(
            resolved_output_dir,
            artifact_id="evidence_payload",
            category="sidecar_evidence",
            expected_filenames=B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS["evidence_payload"],
            loader=_load_jsonl_records,
        ),
        _check_artifact(
            resolved_output_dir,
            artifact_id="closure_decision",
            category="closure_artifact",
            expected_filenames=B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS["closure_decision"],
            loader=_load_closure_payload,
        ),
        _check_artifact(
            resolved_output_dir,
            artifact_id="action_requests",
            category="action_request_artifact",
            expected_filenames=B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS["action_requests"],
            loader=_load_action_requests_payload,
        ),
    ]

    check_by_id = {item.artifact_id: item for item in checks}
    core_ids = {"metrics", "bbox_predictions", "evidence_payload"}
    valid_core_ids = {
        item.artifact_id for item in checks if item.artifact_id in core_ids and item.status == "present_valid"
    }
    invalid_reason_codes = _invalid_artifact_reason_codes(checks)
    missing_or_invalid = [item.artifact_id for item in checks if item.status != "present_valid"]

    if not missing_or_invalid:
        status = "artifact_complete"
        next_recommendation = "continue_with_collect_judge_comparison"
        reasons = ["all_required_artifact_families_present"]
    elif not valid_core_ids:
        status = "stop"
        next_recommendation = "stop_no_collectable_validation_evidence"
        reasons = (
            ["no_valid_core_validation_artifacts"]
            if invalid_reason_codes
            else ["no_core_validation_artifacts_present"]
        )
    elif check_by_id["evidence_payload"].status != "present_valid":
        status = "pivot"
        next_recommendation = "pivot_without_sidecar_mechanism_evidence"
        reasons = [_route_reason_for_check(check_by_id["evidence_payload"])]
    else:
        status = "repair_once"
        next_recommendation = "repair_once_collect_missing_artifacts"
        reasons = ["artifact_repair_once_possible"]
        reasons.extend(
            _route_reason_for_check(check_by_id[artifact_id])
            for artifact_id in ("metrics", "bbox_predictions", "closure_decision", "action_requests")
            if check_by_id[artifact_id].status != "present_valid"
        )

    reasons.extend(invalid_reason_codes)
    reasons.extend(f"{item.artifact_id}:{item.status}" for item in checks if item.status != "present_valid")

    summary = _build_report_summary(
        checks,
        status=status,
        next_recommendation=next_recommendation,
        reasons=reasons,
    )

    return BoundedValidationArtifactReport(
        bucket_id=resolved_bucket.bucket_id,
        output_dir=str(resolved_output_dir),
        artifact_checks=checks,
        status=status,
        next_recommendation=next_recommendation,
        summary=summary,
        reasons=reasons,
    )


def write_b14_l74_artifact_readiness_report(
    output_dir: Path | str,
    *,
    bucket: BoundedValidationBucket | None = None,
) -> dict[str, object]:
    report = assess_b14_l74_artifact_readiness(output_dir, bucket=bucket)
    report_path = Path(output_dir) / ARTIFACT_READINESS_REPORT_NAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    payload["report_path"] = str(report_path)
    serialized = _canonical_json(payload)
    replayed = report_path.exists() and report_path.read_text(encoding="utf-8") == serialized
    if not replayed:
        report_path.write_text(serialized, encoding="utf-8")
    return {
        **payload,
        "created": True,
        "replayed": replayed,
    }


__all__ = [
    "BoundedValidationArtifactReport",
    "BoundedValidationBucket",
    "SCHEMA_PATH",
    "FIXTURE_PATH",
    "ARTIFACT_READINESS_REPORT_NAME",
    "B14_L74_COLLECT_ARTIFACT_FILENAME_GROUPS",
    "ValidationArtifactCheck",
    "ValidationArm",
    "ValidationBudget",
    "ValidationMetric",
    "ValidationScope",
    "SharedInputs",
    "assess_b14_l74_artifact_readiness",
    "write_b14_l74_artifact_readiness_report",
    "build_b14_l74_bounded_validation_bucket",
    "load_bounded_validation_bucket",
    "load_bounded_validation_bucket_schema",
    "write_bounded_validation_bucket",
]

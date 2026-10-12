from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import Field

from .bounded_validation_bucket import (
    FIXTURE_PATH as BOUNDED_BUCKET_FIXTURE_PATH,
    BoundedValidationBucket,
    build_b14_l74_bounded_validation_bucket,
    load_bounded_validation_bucket,
)
from .schema import StrictModel


class ArtifactRequirement(StrictModel):
    artifact_id: str
    filename: str
    category: str
    required: bool = True
    allow_alternative_group: str = ""
    description: str


class ValidationTaskDesignPacket(StrictModel):
    schema_version: int = 1
    artifact_type: str = "validation_task_design_packet"
    status: str = "packet_ready"
    packet_ready: bool = True
    packet_id: str
    bucket_id: str
    project_id: str
    mission_id: str
    research_question: str
    validation_bucket_path: str = ""
    scope: dict[str, object]
    baseline: dict[str, object]
    variant: dict[str, object]
    shared_inputs: dict[str, object]
    budget: dict[str, object]
    task_contract: dict[str, object]
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list)
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


SCHEMA_PATH = Path(".solution/continuation/b14_l74_validation_task_design_packet_schema.yaml")


def _artifact_requirements() -> list[ArtifactRequirement]:
    return [
        ArtifactRequirement(
            artifact_id="metrics",
            filename="metrics.json",
            category="metric_evidence",
            description="Bounded baseline-vs-variant metrics for the shared bucket.",
        ),
        ArtifactRequirement(
            artifact_id="bbox_predictions",
            filename="bbox_predictions.jsonl",
            category="bbox_behavior_evidence",
            description="Per-image bbox behavior outputs for baseline and variant comparison.",
        ),
        ArtifactRequirement(
            artifact_id="evidence_payload",
            filename="evidence_payload.jsonl",
            category="sidecar_evidence",
            description="Evidence payloads that explain sidecar/crop-retoken behavior.",
        ),
        ArtifactRequirement(
            artifact_id="decision_summary_json",
            filename="decision_summary.json",
            category="execution_evidence",
            allow_alternative_group="decision_summary",
            description="Machine-readable bounded decision summary if JSON is emitted.",
        ),
        ArtifactRequirement(
            artifact_id="decision_summary_yaml",
            filename="decision_summary.yaml",
            category="execution_evidence",
            allow_alternative_group="decision_summary",
            description="Machine-readable bounded decision summary if YAML is emitted.",
        ),
        ArtifactRequirement(
            artifact_id="action_requests",
            filename="action_requests.json",
            category="execution_evidence",
            description="Follow-up action requests without direct dispatch or inbox promotion.",
        ),
        ArtifactRequirement(
            artifact_id="stdout_log",
            filename="stdout.log",
            category="execution_evidence",
            description="Standard output log for execution-path auditing.",
        ),
        ArtifactRequirement(
            artifact_id="stderr_log",
            filename="stderr.log",
            category="execution_evidence",
            description="Standard error log for execution-path auditing.",
        ),
    ]


def build_validation_task_design_packet(
    bucket: BoundedValidationBucket,
    *,
    validation_bucket_path: Path | str | None = None,
) -> ValidationTaskDesignPacket:
    required_artifacts = _artifact_requirements()
    bucket_payload = bucket.model_dump(mode="json")
    return ValidationTaskDesignPacket(
        packet_id=f"{bucket.bucket_id}_validation_task_design_packet",
        bucket_id=bucket.bucket_id,
        project_id=bucket.project_id,
        mission_id=bucket.mission_id,
        research_question=bucket.research_question,
        validation_bucket_path=str(validation_bucket_path or ""),
        scope=bucket_payload["scope"],
        baseline=bucket_payload["baseline"],
        variant=bucket_payload["variant"],
        shared_inputs=bucket_payload["shared_inputs"],
        budget=bucket_payload["budget"],
        task_contract={
            "task_kind": "bounded_remote_validation_design_local_only",
            "comparison_mode": "baseline_vs_variant_same_bounded_bucket",
            "baseline_path_id": bucket.baseline.path,
            "variant_path_id": bucket.variant.path,
            "shared_bucket_required": True,
            "shared_image_ids_required": True,
            "same_mask_inputs_if_available": bucket.shared_inputs.mask_inputs_if_available,
            "same_p2_inputs_if_available": bucket.shared_inputs.p2_inputs_if_available,
            "same_sidecar_inputs_if_available": bucket.shared_inputs.sidecar_inputs_if_available,
            "decision_summary_any_of": ["decision_summary.json", "decision_summary.yaml"],
            "dispatch_allowed": False,
            "active_inbox_write_allowed": False,
        },
        required_artifacts=required_artifacts,
        claim_limits=list(bucket.claim_limits),
        stop_rules=list(bucket.stop_rules),
        continue_rules=list(bucket.continue_rules),
        side_effects=dict(bucket.side_effects),
    )


def build_b14_l74_validation_task_design_packet(
    bucket: BoundedValidationBucket | None = None,
) -> ValidationTaskDesignPacket:
    resolved_bucket = bucket or build_b14_l74_bounded_validation_bucket()
    validation_bucket_path = BOUNDED_BUCKET_FIXTURE_PATH if bucket is None else None
    return build_validation_task_design_packet(
        resolved_bucket,
        validation_bucket_path=validation_bucket_path,
    )


def load_validation_task_design_packet_schema(path: Path | str | None = None) -> dict[str, object]:
    schema_path = Path(path or SCHEMA_PATH)
    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("validation task design packet schema must contain a YAML object")
    return payload


def write_validation_task_design_packet(
    packet: ValidationTaskDesignPacket,
    output_path: Path | str,
) -> dict[str, object]:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = packet.model_dump(mode="json")
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "written",
        "packet_path": str(target),
        "packet": payload,
        "side_effects": packet.side_effects,
    }


def load_b14_l74_validation_task_design_packet_from_fixture() -> ValidationTaskDesignPacket:
    return build_validation_task_design_packet(
        load_bounded_validation_bucket(BOUNDED_BUCKET_FIXTURE_PATH),
        validation_bucket_path=BOUNDED_BUCKET_FIXTURE_PATH,
    )


__all__ = [
    "ArtifactRequirement",
    "SCHEMA_PATH",
    "ValidationTaskDesignPacket",
    "build_b14_l74_validation_task_design_packet",
    "build_validation_task_design_packet",
    "load_b14_l74_validation_task_design_packet_from_fixture",
    "load_validation_task_design_packet_schema",
    "write_validation_task_design_packet",
]

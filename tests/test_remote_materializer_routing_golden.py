from __future__ import annotations

from pathlib import Path

from solution_agent.execution_port.script_registry import load_registry
from solution_agent.orchestrator.core import _execution_semantic_errors
from solution_agent.remote_materializer.controller import _infer_default_script_id
from solution_agent.remote_materializer.task_loader import load_task


def _task(tmp_path: Path, task_id: str, stage: str, outputs: list[str], body: str = ""):
    task_path = tmp_path / f"{task_id}.md"
    task_path.write_text(
        f"""---
task_id: {task_id}
title: {task_id}
worker: materialized_execution
status: pending
execution_stage: {stage}
---

# Task

{body or "Produce the requested artifacts."}

## Required Outputs

""" + "\n".join(f"- {name}" for name in outputs) + "\n",
        encoding="utf-8",
    )
    return load_task(task_path)


def _route(task) -> str:
    registry = load_registry()
    return _infer_default_script_id(task, {}, registry["scripts"], registry)


def test_closure_report_artifacts_route_to_contract_writer(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_closure",
        "report",
        ["worker_notes.md", "artifact_summary.md", "action_requests.json", "decision.json"],
        "Write a bounded review and action request report.",
    )

    assert _route(task) == "contract_artifact_writer"


def test_seg_overfit_checklist_report_is_not_treated_as_seg_execution(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_seg_checklist",
        "review",
        ["seg_overfit_artifact_checklist.csv"],
        "Review existing seg_overfit artifacts and write a checklist report.",
    )

    assert _route(task) == "contract_artifact_writer"


def test_metadata_only_outputs_route_to_metadata_classifier(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_metadata_only",
        "analysis",
        ["metadata_only_auc.json", "metadata_only_group_score_table.csv"],
        "Run a metadata-only classifier.",
    )

    assert _route(task) == "loupe_metadata_only_classifier"


def test_seg_overfit_metric_outputs_route_to_seg_overfit(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_seg_overfit",
        "probe",
        ["seg_overfit_metrics.json", "seg_overfit_train_eval.csv", "seg_overfit_visual_sheet.jpg"],
        "Run a bounded fake-only segmentation overfit evidence probe.",
    )

    assert _route(task) == "loupe_seg_overfit"


def test_unknown_comparable_report_routes_to_contract_writer_not_experiment(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_unknown_report",
        "analysis",
        ["model_comparison_review.md", "missing_evidence_notes.md"],
        "Compare available evidence and write missing evidence notes.",
    )

    assert _route(task) == "contract_artifact_writer"


def test_mixed_report_and_real_metric_requires_disambiguation(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_mixed_outputs",
        "analysis",
        ["seg_overfit_artifact_checklist.csv", "seg_overfit_metrics.json"],
        "Review existing checklist but also requests real seg_overfit metrics.",
    )

    assert _route(task) == ""


def test_feature_leakage_followup_outputs_route_to_followup_packet(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_feature_leakage_followup",
        "analysis",
        [
            "cluster_visual_sheet.jpg",
            "clean_center_perturbation_metrics.csv",
            "context_size_sensitivity.csv",
            "maskout_clean_token_probe.csv",
        ],
        "Produce a bounded GenD feature leakage follow-up packet from existing artifacts.",
    )

    assert _route(task) == "feature_leakage_followup_packet"


def test_feature_leakage_maskout_outputs_route_to_maskout_probe(tmp_path: Path):
    task = _task(
        tmp_path,
        "golden_feature_leakage_maskout",
        "probe",
        ["maskout_clean_token_probe.csv", "maskout_clean_token_delta.csv"],
        "Run a bounded feature-level clean-token maskout probe.",
    )

    assert _route(task) == "feature_leakage_maskout_probe"
    assert _execution_semantic_errors(task, "feature_leakage_maskout_probe") == []

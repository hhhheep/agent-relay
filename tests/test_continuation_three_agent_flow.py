from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.continuation import three_agent_flow
from solution_agent.continuation.three_agent_flow import (
    assess_repair_to_scheduler_gate_from_summary,
    assess_three_agent_autonomy,
    build_artifact_index_research_prerequisite_packet,
    build_context_index_research_prerequisite_packet,
    build_research_prerequisite_packet,
    build_l72_manual_launch_readiness_from_stage,
    build_l72_task_admission_packet,
    dry_run_l72_task_draft_scheduler_admission,
    rehearse_scheduler_admission_after_repair,
    rehearse_three_agent_flow,
    stage_l72_task_draft_candidate,
)


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    source_policy = Path(".solution/continuation/planner_policy.yaml")
    if source_policy.exists():
        target_policy = tmp_path / ".solution" / "continuation" / "planner_policy.yaml"
        target_policy.parent.mkdir(parents=True, exist_ok=True)
        target_policy.write_text(source_policy.read_text(encoding="utf-8"), encoding="utf-8")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _materialize_workplace_sidecar(summary: dict, workplace_root: Path) -> Path:
    patch_intent_path = Path(summary["agents"]["discussion_continuation_agent"]["outputs"]["patch_intent_path"])
    patch_intent = json.loads(patch_intent_path.read_text(encoding="utf-8"))
    target = workplace_root / patch_intent["target_file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch_intent["new_text"] + "\n", encoding="utf-8")
    return target


def _make_l70_artifacts(tmp_path: Path) -> Path:
    artifacts = tmp_path / "results" / "b14_l70_large_image_coordinate_protocol_audit" / "latest" / "artifacts"
    _write_json(
        artifacts / "decision_summary.json",
        {
            "status": "partial",
            "reason": "missing_capability",
            "next_action": "continue_with_l71_report_only_sidecar_repair",
        },
    )
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": "L71 report-only bbox sidecar module is needed before L72 smoke.",
            },
        ],
    )
    return artifacts


def _make_l71_taskbook(tmp_path: Path) -> Path:
    path = tmp_path / "tasks" / "pending" / "b14_l71_bbox_sidecar_large_image_cli_integration" / "task.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
task_id: b14_l71_bbox_sidecar_large_image_cli_integration
title: Implement bbox sidecar large-image CLI integration
intent: source_change
---

Create a reusable report-only module:

`B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py`

Add optional flags such as:

`--enable-bbox-sidecar`

Default behavior must be unchanged and this must not dispatch.
""",
        encoding="utf-8",
    )
    return path


def _make_l72_taskbook(tmp_path: Path) -> Path:
    path = tmp_path / "tasks" / "inbox" / "b14_l70_large_image_feasibility_batch" / "b14_l72_large_image_bbox_sidecar_smoke.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
task_id: b14_l72_large_image_bbox_sidecar_smoke
title: Run large-image bbox sidecar smoke
priority: high
worker: materialized_execution
status: pending
created_at: 2026-05-21T15:20:00+08:00
line: B14_spatial_grace_graph_utility
batch: b14_l70_large_image_feasibility_batch
dag_root: b14_l70_large_image_coordinate_protocol_audit
intent: bounded_probe
resource_hint: gpu
resource_lane: gpu_run
execution_stage: run
runtime_budget_hours: 6
depends_on:
  - b14_l70_large_image_coordinate_protocol_audit
  - b14_l71_bbox_sidecar_large_image_cli_integration
requires_artifacts_from:
  - b14_l70_large_image_coordinate_protocol_audit
  - b14_l71_bbox_sidecar_large_image_cli_integration
allow_codex_fallback: false
claim_level: exploratory
formal_claim_allowed: false
allowed_workers:
  - materialized_execution
target_workplace:
  profile: EXAMPLE_WORKER_A_B14
  host: EXAMPLE_WORKER_A
  remote_workdir: /srv/example-workspaces-b/B14_agent_handoff_20260521
  remote_root: /srv/example-workspaces-b/B14_agent_handoff_20260521/runs
execution:
  script_id: b14_large_image_bbox_sidecar_smoke
  timeout_sec: 21600
  gpu_required: true
  gpu_count: 1
  args:
    workspace_root: .
    project_subdir: B14_spatial_grace_graph_utility
    max_images: 300
---

# Task

Run a bounded GPU smoke test of the large-image / bbox sidecar path.
""",
        encoding="utf-8",
    )
    return path


def _project_knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "current_state": {
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
        },
        "evidence_refs": [
            {"ref": "docs/B14_PROJECT_CHRONOLOGY_FOR_AGENTS_2026-05-21.md", "authority": "chronology"},
        ],
        "claim_limits": [
            {"limit": "smoke/proxy evidence is not a formal claim"},
        ],
        "next_safe_action": {
            "action": "run bounded baseline-vs-variant probe",
            "expected_outputs": ["comparison_report.json"],
        },
    }


def _discussion_payload() -> dict:
    return {
        "key_uncertainty": "Does sidecar retain valid coordinates under large-image mode?",
        "rejected_options": [{"option": "formal claim", "reason": "proxy evidence only"}],
        "if_success_next": "expand to bounded validation bucket",
        "if_failure_next": "hold and inspect mapping failure cases",
        "stop_boundaries": ["formal_claim"],
        "fairness_protocol": {
            "baseline": "without sidecar",
            "variant": "with sidecar",
            "metrics": ["candidate_count", "coordinate_validity"],
            "input_scope": "same bounded large-image bucket",
            "falsification_test": "variant must not produce invalid boxes",
        },
    }


def test_research_prerequisite_packet_is_ready_and_local_only(tmp_path: Path) -> None:
    output_dir = tmp_path / "continuation" / "research_prerequisite_packet"
    report = build_research_prerequisite_packet(
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        output_dir=output_dir,
    )

    assert report["status"] == "packet_ready"
    assert report["can_continue_to_mission_gate"] is True
    assert report["chosen_action"] == "continue"
    assert report["next_recommendation"] == "continue_to_mission_gate"
    assert report["side_effects"]["scheduler_dispatch"] is False
    assert report["side_effects"]["active_inbox_write"] is False
    assert Path(report["research_prerequisite_gate_path"]).exists()
    assert Path(report["report_path"]).exists()
    payload = json.loads((output_dir / "research_prerequisite_packet.json").read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "research_prerequisite_packet"


def test_research_prerequisite_packet_routes_inventory_when_source_refs_missing(tmp_path: Path) -> None:
    output_dir = tmp_path / "continuation" / "research_prerequisite_packet_inventory"
    knowledge = _project_knowledge()
    knowledge["evidence_refs"] = []

    report = build_research_prerequisite_packet(
        project_knowledge=knowledge,
        discussion_payload=_discussion_payload(),
        output_dir=output_dir,
    )

    assert report["can_continue_to_mission_gate"] is False
    assert report["chosen_action"] == "request_inventory"
    assert report["next_recommendation"] == "run_inventory_before_any_execution"
    assert "missing_evidence" in report["reason_codes"]


def test_night_researcher_research_prerequisite_packet_cli_writes_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "continuation" / "research_prerequisite_packet_cli"
    project_knowledge_path = tmp_path / "project_knowledge.json"
    discussion_payload_path = tmp_path / "discussion_payload.json"
    _write_json(project_knowledge_path, _project_knowledge())
    _write_json(discussion_payload_path, _discussion_payload())

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "research-prerequisite-packet",
            "--project-knowledge",
            str(project_knowledge_path),
            "--discussion-payload",
            str(discussion_payload_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "packet_ready"
    assert payload["can_continue_to_mission_gate"] is True
    assert payload["next_recommendation"] == "continue_to_mission_gate"
    assert Path(payload["research_prerequisite_gate_path"]).exists()
    assert (output_dir / "research_prerequisite_packet.json").exists()


def test_context_index_research_prerequisite_packet_writes_bundle_and_packet(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "continuation" / "context_index_prerequisite_packet"
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_context_index_dry_run",
        lambda *args, **kwargs: _project_knowledge(),
    )

    report = build_context_index_research_prerequisite_packet(
        repo_root=tmp_path,
        query="b14 calibrated bbox",
        output_dir=output_dir,
        discussion_payload=_discussion_payload(),
        limit=5,
    )

    assert report["status"] == "packet_ready"
    assert report["context_index_query"] == "b14 calibrated bbox"
    assert report["context_index_limit"] == 5
    assert report["project_knowledge_bundle_path"].endswith("project_knowledge_bundle.json")
    assert report["project_knowledge_summary"]["project_id"] == "B14_spatial_grace_graph_utility"
    assert report["side_effects"]["docs_dir_scan"] is False
    assert (output_dir / "project_knowledge_bundle.json").exists()
    assert (output_dir / "research_prerequisite_packet.json").exists()


def test_night_researcher_context_index_prerequisite_packet_cli_writes_artifacts(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "continuation" / "context_index_prerequisite_packet_cli"
    discussion_payload_path = tmp_path / "discussion_payload.json"
    _write_json(discussion_payload_path, _discussion_payload())
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_context_index_dry_run",
        lambda *args, **kwargs: _project_knowledge(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "context-index-prerequisite-packet",
            "--query",
            "b14 calibrated bbox",
            "--repo-root",
            str(tmp_path),
            "--discussion-payload",
            str(discussion_payload_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "packet_ready"
    assert payload["context_index_query"] == "b14 calibrated bbox"
    assert Path(payload["project_knowledge_bundle_path"]).exists()
    assert Path(payload["research_prerequisite_gate_path"]).exists()
    assert (output_dir / "research_prerequisite_packet.json").exists()


def test_artifact_index_research_prerequisite_packet_writes_bundle_and_packet(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "continuation" / "artifact_index_prerequisite_packet"
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_artifact_index_dry_run",
        lambda *args, **kwargs: _project_knowledge(),
    )

    report = build_artifact_index_research_prerequisite_packet(
        repo_root=tmp_path,
        query="b14 artifact grounded bbox",
        output_dir=output_dir,
        discussion_payload=_discussion_payload(),
        limit=4,
    )

    assert report["status"] == "packet_ready"
    assert report["artifact_index_query"] == "b14 artifact grounded bbox"
    assert report["artifact_index_limit"] == 4
    assert report["project_knowledge_bundle_path"].endswith("project_knowledge_bundle.json")
    assert report["project_knowledge_summary"]["project_id"] == "B14_spatial_grace_graph_utility"
    assert report["side_effects"]["docs_dir_scan"] is False
    assert report["side_effects"]["context_index_query"] is False
    assert (output_dir / "project_knowledge_bundle.json").exists()
    assert (output_dir / "research_prerequisite_packet.json").exists()


def test_context_index_prerequisite_packet_blocks_to_validation_design_when_fairness_protocol_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "continuation" / "context_index_prerequisite_packet_validation_block"
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_context_index_dry_run",
        lambda *args, **kwargs: _project_knowledge(),
    )
    discussion = _discussion_payload()
    discussion.pop("fairness_protocol")

    report = build_context_index_research_prerequisite_packet(
        repo_root=tmp_path,
        query="b14 fairness protocol gap",
        output_dir=output_dir,
        discussion_payload=discussion,
        limit=3,
    )

    assert report["status"] == "packet_ready"
    assert report["can_continue_to_mission_gate"] is False
    assert report["chosen_action"] == "hold"
    assert report["next_recommendation"] == "design_validation_protocol_or_continue_discussion"
    assert report["side_effects"]["scheduler_dispatch"] is False
    assert report["side_effects"]["active_inbox_write"] is False
    assert report["side_effects"]["remote_execution"] is False


def test_artifact_index_prerequisite_packet_blocks_to_inventory_when_source_refs_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "continuation" / "artifact_index_prerequisite_packet_inventory_block"
    knowledge = _project_knowledge()
    knowledge["evidence_refs"] = []
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_artifact_index_dry_run",
        lambda *args, **kwargs: knowledge,
    )

    report = build_artifact_index_research_prerequisite_packet(
        repo_root=tmp_path,
        query="b14 source refs missing",
        output_dir=output_dir,
        discussion_payload=_discussion_payload(),
        limit=2,
    )

    assert report["status"] == "packet_ready"
    assert report["can_continue_to_mission_gate"] is False
    assert report["chosen_action"] == "request_inventory"
    assert report["next_recommendation"] == "run_inventory_before_any_execution"
    assert "missing_evidence" in report["reason_codes"]
    assert report["side_effects"]["scheduler_dispatch"] is False
    assert report["side_effects"]["active_inbox_write"] is False
    assert report["side_effects"]["registry_mutation"] is False


def test_night_researcher_artifact_index_prerequisite_packet_cli_writes_artifacts(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "continuation" / "artifact_index_prerequisite_packet_cli"
    discussion_payload_path = tmp_path / "discussion_payload.json"
    _write_json(discussion_payload_path, _discussion_payload())
    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.build_project_knowledge_from_artifact_index_dry_run",
        lambda *args, **kwargs: _project_knowledge(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "artifact-index-prerequisite-packet",
            "--query",
            "b14 artifact grounded bbox",
            "--repo-root",
            str(tmp_path),
            "--discussion-payload",
            str(discussion_payload_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "packet_ready"
    assert payload["artifact_index_query"] == "b14 artifact grounded bbox"
    assert Path(payload["project_knowledge_bundle_path"]).exists()
    assert Path(payload["research_prerequisite_gate_path"]).exists()
    assert (output_dir / "research_prerequisite_packet.json").exists()


def test_three_agent_flow_rehearses_discussion_opencoder_scheduler_handoff(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    result_dir = _make_l70_artifacts(tmp_path)
    task_path = _make_l71_taskbook(tmp_path)

    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=result_dir,
        source_task_path=task_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal",
    )

    assert summary["handoff_order"] == [
        "discussion_continuation_agent",
        "opencoder_agent",
        "scheduler_agent",
        "discussion_continuation_agent",
    ]
    discussion = summary["agents"]["discussion_continuation_agent"]["outputs"]
    opencoder = summary["agents"]["opencoder_agent"]["outputs"]
    scheduler = summary["agents"]["scheduler_agent"]["outputs"]

    assert discussion["line_progression"]["continue_allowed"] is True
    assert discussion["line_progression"]["next_step"] == "continue_with_bounded_source_repair_then_rerun"
    assert opencoder["repair_route"] == "bounded_self_repair"
    assert opencoder["patch_artifact"]["ok"] is True
    assert opencoder["rerun_intent"]["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert scheduler["scheduler_explain"]["side_effects"]["scheduler_dispatch"] is False
    assert scheduler["scheduler_explain"]["eligible"] is False
    assert "claim_limit_no_scheduler_dispatch" in scheduler["scheduler_explain"]["reasons"]
    assert summary["side_effects"]["source_edit"] is False
    assert summary["side_effects"]["scheduler_dispatch"] is False
    assert (tmp_path / "continuation" / "three_agent_rehearsal" / "three_agent_flow_summary.json").exists()


def test_rehearse_three_agent_flow_records_repair_route_blockers(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    result_dir = _make_l70_artifacts(tmp_path)
    task_path = _make_l71_taskbook(tmp_path)

    monkeypatch.setattr(
        "solution_agent.continuation.three_agent_flow.select_repair_executor",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "repair_id": "test_route_blockers",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "failure_type": "runtime_source_behavior_change",
            "repair_type": "runtime_source_behavior_change",
            "decision": {"route": "codex", "confidence": "medium", "reason": "route_for_risk"},
            "blockers": ["target_file_forbidden_for_self_repair", "requires_human_review"],
        },
    )

    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=result_dir,
        source_task_path=task_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_blockers",
    )

    opencoder = summary["agents"]["opencoder_agent"]["outputs"]

    assert opencoder["repair_route"] == "codex"
    assert set(opencoder["repair_route_blockers"]) == {
        "target_file_forbidden_for_self_repair",
        "requires_human_review",
    }


def test_three_agent_rehearse_cli_writes_flow_summary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    result_dir = _make_l70_artifacts(tmp_path)
    task_path = _make_l71_taskbook(tmp_path)
    output_dir = tmp_path / "continuation" / "three_agent_rehearsal_cli"

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "three-agent-rehearse",
            "--mission-id",
            "B14_night_research_v1",
            "--proposal-run-id",
            "b14_l70_l73_rehearsal",
            "--candidate-id",
            "b14_l71_to_l72",
            "--source-task-id",
            "b14_l70_large_image_coordinate_protocol_audit",
            "--result-dir",
            str(result_dir),
            "--source-task-path",
            str(task_path),
            "--downstream-task-id",
            "b14_l72_large_image_bbox_sidecar_smoke",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["agents"]["opencoder_agent"]["outputs"]["patch_artifact"]["ok"] is True
    assert (output_dir / "three_agent_flow_summary.json").exists()


def test_repair_to_scheduler_gate_blocks_until_patch_applied_and_validated(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_gate",
    )

    gate = assess_repair_to_scheduler_gate_from_summary(
        summary,
        validation_report={"import_ok": True, "smoke_ok": True, "claim_boundary_ok": True},
    )

    assert gate["stageable"] is False
    assert gate["status"] == "blocked"
    assert gate["leader"] == "discussion_continuation_agent"
    assert "repair_not_applied" in gate["blockers"]
    assert gate["scheduler_instruction"] == "keep_scheduler_dry_run_blocked"


def test_repair_to_scheduler_gate_becomes_stageable_after_applied_validated_repair(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_gate_pass",
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_gate_pass",
    )

    gate = assess_repair_to_scheduler_gate_from_summary(
        summary,
        validation_report=validation,
    )

    assert gate["stageable"] is True
    assert gate["status"] == "stageable"
    assert gate["blockers"] == []
    assert gate["scheduler_instruction"] == "may_stage_downstream_rerun_after_admission"
    assert gate["agent_roles"]["scheduler_agent"] == "owns admission and dispatch after gate pass"


def test_repair_to_scheduler_gate_uses_validation_report_embedded_in_summary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_gate_embedded",
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=tmp_path / "continuation" / "three_agent_rehearsal_gate_embedded",
    )

    gate = assess_repair_to_scheduler_gate_from_summary(summary)

    assert gate["stageable"] is True
    assert gate["blockers"] == []


def test_autonomy_assessment_marks_real_rehearsal_bounded_but_not_model_ready(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_autonomy",
    )

    assessment = assess_three_agent_autonomy(summary)

    assert assessment["leader"] == "discussion_continuation_agent"
    assert assessment["autonomy_style"] == "mission_bounded_cautious"
    assert assessment["can_continue_without_codex"] is True
    assert assessment["can_continue_without_human"] is True
    assert assessment["can_play_through_model"] is False
    assert "repair_not_applied" in assessment["blockers"]
    assert "validation_report_missing" in assessment["blockers"]
    assert assessment["next_autonomous_step"] == "apply_or_mark_external_patch_then_run_import_smoke_claim_validation"
    assert assessment["scores"]["project_understanding"] >= 8.0


def test_autonomy_assessment_becomes_model_ready_after_applied_validation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_autonomy_ready",
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=tmp_path / "continuation" / "three_agent_autonomy_ready",
    )
    gate = assess_repair_to_scheduler_gate_from_summary(
        summary,
        validation_report=validation,
    )

    assessment = assess_three_agent_autonomy(summary, repair_scheduler_gate=gate)

    assert assessment["autonomy_style"] == "mission_bounded_ready"
    assert assessment["can_play_through_model"] is True
    assert assessment["next_autonomous_step"] == "stage_downstream_rerun_after_scheduler_admission"
    assert assessment["blockers"] == []
    assert assessment["scores"]["model_playthrough_readiness"] >= 8.0


def test_autonomy_assessment_uses_validation_report_embedded_in_summary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_autonomy_embedded",
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=tmp_path / "continuation" / "three_agent_autonomy_embedded",
    )

    assessment = assess_three_agent_autonomy(summary)

    assert assessment["autonomy_style"] == "mission_bounded_ready"
    assert assessment["can_play_through_model"] is True
    assert assessment["blockers"] == []


def test_validate_workplace_patch_adapter_writes_validation_report_and_updates_repair_result(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "three_agent_validation"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    target = _materialize_workplace_sidecar(summary, workplace_root)

    report = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )

    repair_result = summary["agents"]["opencoder_agent"]["outputs"]["repair_result"]
    assert target.exists()
    assert report["status"] == "validated"
    assert report["target_file_exists"] is True
    assert report["import_ok"] is True
    assert report["smoke_ok"] is True
    assert report["claim_boundary_ok"] is True
    assert report["patch_fingerprint_ok"] is True
    assert report["no_dispatch_safety_ok"] is True
    assert report["expected_file_sha256"] == report["actual_file_sha256"]
    assert repair_result["execution_status"] == "externally_applied"
    assert (output_dir / "validation_report.json").exists()
    assert json.loads((output_dir / "opencoder_lifecycle" / "repair_result.yaml").read_text(encoding="utf-8"))["execution_status"] == "externally_applied"


def test_validate_workplace_patch_adapter_blocks_when_workplace_file_drifts_from_patch_intent(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "three_agent_validation_drift"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    target = _materialize_workplace_sidecar(summary, workplace_root)
    target.write_text(target.read_text(encoding="utf-8") + "# local drift\n", encoding="utf-8")

    report = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )

    assert report["status"] == "blocked"
    assert report["import_ok"] is True
    assert report["smoke_ok"] is True
    assert report["patch_fingerprint_ok"] is False
    assert report["claim_boundary_ok"] is False
    assert report["expected_file_sha256"] != report["actual_file_sha256"]
    assert "claim_boundary_failed" in report["blockers"]
    assert summary["agents"]["opencoder_agent"]["outputs"]["repair_result"]["execution_status"] == "not_attempted"


def test_validate_workplace_patch_adapter_clears_external_status_when_revalidation_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "three_agent_validation_recheck"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    target = _materialize_workplace_sidecar(summary, workplace_root)

    first = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )
    assert first["status"] == "validated"
    assert summary["agents"]["opencoder_agent"]["outputs"]["repair_result"]["execution_status"] == "externally_applied"

    target.write_text(target.read_text(encoding="utf-8") + "# local drift\n", encoding="utf-8")
    second = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )

    repair_result = summary["agents"]["opencoder_agent"]["outputs"]["repair_result"]
    assert second["status"] == "blocked"
    assert second["repair_execution_status_after_validation"] == "not_attempted"
    assert repair_result["execution_status"] == "not_attempted"
    assert repair_result["status"] == "validation_blocked"


def test_validate_workplace_patch_adapter_blocks_when_target_file_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "three_agent_validation_blocked"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )

    report = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=tmp_path / "missing_workplace",
        output_dir=output_dir,
    )

    gate = assess_repair_to_scheduler_gate_from_summary(summary, validation_report=report)

    assert report["status"] == "blocked"
    assert "target_file_missing" in report["blockers"]
    assert "import_smoke_failed" in report["blockers"]
    assert summary["agents"]["opencoder_agent"]["outputs"]["repair_result"]["execution_status"] == "not_attempted"
    assert gate["stageable"] is False
    assert "validation_no_dispatch_safety_not_ok" in gate["blockers"]


def test_autonomy_assess_cli_writes_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=tmp_path / "continuation" / "three_agent_autonomy_cli",
    )
    summary_path = tmp_path / "summary.json"
    _write_json(summary_path, summary)
    output_path = tmp_path / "autonomy_assessment.json"

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "autonomy-assess",
            "--summary-path",
            str(summary_path),
            "--output-path",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["autonomy_style"] == "mission_bounded_cautious"
    assert output_path.exists()


def test_repair_scheduler_gate_cli_uses_summary_validation_report_when_argument_omitted(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "three_agent_gate_cli_embedded"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    summary_path = output_dir / "three_agent_flow_summary.json"
    three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
        summary_path=summary_path,
    )

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "repair-scheduler-gate",
            "--summary-path",
            str(summary_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "stageable"
    assert payload["blockers"] == []


def test_scheduler_admission_rehearsal_ready_after_stageable_repair_gate(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "scheduler_admission_ready"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )
    gate = assess_repair_to_scheduler_gate_from_summary(summary, validation_report=validation)

    admission = rehearse_scheduler_admission_after_repair(
        summary,
        repair_scheduler_gate=gate,
        output_dir=output_dir,
    )

    assert admission["status"] == "admission_ready"
    assert admission["admission_stageable"] is True
    assert admission["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert admission["admission_instruction"] == "may_prepare_scheduler_admission_packet_without_dispatch"
    assert admission["required_evidence"]["patch_fingerprint_ok"] is True
    assert admission["side_effects"]["scheduler_dispatch"] is False
    assert admission["side_effects"]["active_inbox_write"] is False
    assert (output_dir / "scheduler_admission_rehearsal.json").exists()


def test_scheduler_admission_rehearsal_preserves_b14_l74_readiness_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "scheduler_admission_readiness"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    readiness = {
        "schema_version": 1,
        "can_stage": True,
        "route": "REPAIR_ONCE",
        "status": "READY",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "REPAIR_ONCE",
            "next_recommendation": "REPAIR_ONCE",
            "present_valid_artifact_ids": ["metrics", "evidence_payload"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["bbox_predictions"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "VALID",
                "bbox_predictions": "INVALID",
            },
        },
    }
    summary["agents"]["discussion_continuation_agent"]["outputs"]["line_progression"]["next_step_readiness"] = readiness
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )
    gate = assess_repair_to_scheduler_gate_from_summary(summary, validation_report=validation)

    admission = rehearse_scheduler_admission_after_repair(
        summary,
        repair_scheduler_gate=gate,
        output_dir=output_dir,
    )

    assert admission["next_step_readiness"] == {
        "schema_version": 1,
        "can_stage": True,
        "route": "repair_once",
        "status": "ready",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once",
            "present_valid_artifact_ids": ["metrics", "evidence_payload"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["bbox_predictions"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "valid",
                "bbox_predictions": "invalid",
            },
        },
    }


def test_scheduler_admission_rehearsal_blocks_without_validation_evidence(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "scheduler_admission_blocked"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    gate = assess_repair_to_scheduler_gate_from_summary(summary)

    admission = rehearse_scheduler_admission_after_repair(
        summary,
        repair_scheduler_gate=gate,
        output_dir=output_dir,
    )

    assert admission["status"] == "blocked"
    assert admission["admission_stageable"] is False
    assert "repair_scheduler_gate_not_stageable" in admission["blockers"]
    assert "validation_import_not_ok" in admission["blockers"]
    assert admission["admission_instruction"] == "keep_scheduler_admission_blocked"
    assert admission["side_effects"]["scheduler_dispatch"] is False


def test_scheduler_admission_rehearsal_cli_writes_packet(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "scheduler_admission_cli"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )
    gate = assess_repair_to_scheduler_gate_from_summary(summary, validation_report=validation)
    summary_path = output_dir / "three_agent_flow_summary.json"
    _write_json(summary_path, summary)
    gate_path = output_dir / "repair_scheduler_gate.json"
    _write_json(gate_path, gate)

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "scheduler-admission-rehearse",
            "--summary-path",
            str(summary_path),
            "--repair-scheduler-gate",
            str(gate_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "admission_ready"
    assert (output_dir / "scheduler_admission_rehearsal.json").exists()


def test_l72_admission_packet_binds_stageable_admission_to_taskbook(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_packet_ready"
    summary = rehearse_three_agent_flow(
        mission_id="B14_night_research_v1",
        proposal_run_id="b14_l70_l73_rehearsal",
        candidate_id="b14_l71_to_l72",
        source_task_id="b14_l70_large_image_coordinate_protocol_audit",
        result_dir=_make_l70_artifacts(tmp_path),
        source_task_path=_make_l71_taskbook(tmp_path),
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        output_dir=output_dir,
    )
    workplace_root = tmp_path / "local_workplace"
    _materialize_workplace_sidecar(summary, workplace_root)
    validation = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
    )
    gate = assess_repair_to_scheduler_gate_from_summary(summary, validation_report=validation)
    admission = rehearse_scheduler_admission_after_repair(summary, repair_scheduler_gate=gate, output_dir=output_dir)

    packet = build_l72_task_admission_packet(
        admission_rehearsal=admission,
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )

    assert packet["status"] == "packet_ready"
    assert packet["packet_ready"] is True
    assert packet["task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert packet["scheduler_instruction"] == "review_or_stage_l72_task_without_dispatch"
    assert packet["task_contract"]["script_id"] == "b14_large_image_bbox_sidecar_smoke"
    assert packet["task_contract"]["target_workplace"]["host"] == "EXAMPLE_WORKER_A"
    assert packet["side_effects"]["scheduler_dispatch"] is False
    assert (output_dir / "l72_task_admission_packet.json").exists()
    assert (output_dir / "l72_task_draft.md").exists()


def test_l72_admission_packet_blocks_when_admission_not_ready(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_packet_blocked"
    admission = {
        "status": "blocked",
        "admission_stageable": False,
        "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
    }

    packet = build_l72_task_admission_packet(
        admission_rehearsal=admission,
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )

    assert packet["status"] == "blocked"
    assert packet["packet_ready"] is False
    assert "admission_rehearsal_not_stageable" in packet["blockers"]
    assert "admission_rehearsal_not_ready" in packet["blockers"]
    assert packet["scheduler_instruction"] == "keep_l72_task_admission_blocked"


def test_l72_admission_packet_cli_writes_packet(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_packet_cli"
    admission_path = output_dir / "scheduler_admission_rehearsal.json"
    _write_json(
        admission_path,
        {
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "l72-admission-packet",
            "--admission-rehearsal",
            str(admission_path),
            "--task-path",
            str(_make_l72_taskbook(tmp_path)),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "packet_ready"
    assert (output_dir / "l72_task_admission_packet.json").exists()


def test_l72_draft_scheduler_dry_run_ready_for_packet_ready_draft(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_dry_run_ready"
    admission_path = output_dir / "scheduler_admission_rehearsal.json"
    _write_json(
        admission_path,
        {
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
    )
    packet = build_l72_task_admission_packet(
        admission_rehearsal=json.loads(admission_path.read_text(encoding="utf-8")),
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )

    report = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )

    assert report["status"] == "dry_run_ready"
    assert report["dry_run_ready"] is True
    assert report["task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert report["scheduler_instruction"] == "draft_contract_can_be_staged_after_explicit_promotion_boundary"
    assert report["selector_preview"]["worker"] == "materialized_execution"
    assert report["selector_preview"]["resource_hint"] == "gpu"
    assert report["side_effects"]["scheduler_dispatch"] is False
    assert report["side_effects"]["active_inbox_write"] is False
    assert (output_dir / "l72_task_draft_scheduler_dry_run.json").exists()


def test_l72_admission_packet_and_dry_run_preserve_b14_l74_readiness_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_packet_readiness"
    admission_readiness = {
        "schema_version": 1,
        "can_stage": True,
        "route": "REPAIR_ONCE",
        "status": "READY",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "REPAIR_ONCE",
            "next_recommendation": "REPAIR_ONCE",
            "present_valid_artifact_ids": ["closure_decision"],
            "missing_artifact_ids": ["metrics"],
            "invalid_artifact_ids": ["bbox_predictions"],
            "route_reasons": [
                "metric_evidence_missing_or_invalid",
                "bbox_evidence_missing_or_invalid",
            ],
            "artifact_statuses": {
                "metrics": "MISSING",
                "bbox_predictions": "INVALID",
                "closure_decision": "VALID",
            },
        },
    }
    admission = {
        "status": "admission_ready",
        "admission_stageable": True,
        "mission_id": "B14_night_research_v1",
        "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
        "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "required_evidence": {"patch_fingerprint_ok": True},
        "next_step_readiness": admission_readiness,
    }

    packet = build_l72_task_admission_packet(
        admission_rehearsal=admission,
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )

    expected_readiness = {
        "schema_version": 1,
        "can_stage": True,
        "route": "repair_once",
        "status": "ready",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once",
            "present_valid_artifact_ids": ["closure_decision"],
            "missing_artifact_ids": ["metrics"],
            "invalid_artifact_ids": ["bbox_predictions"],
            "route_reasons": [
                "metric_evidence_missing_or_invalid",
                "bbox_evidence_missing_or_invalid",
            ],
            "artifact_statuses": {
                "metrics": "missing",
                "bbox_predictions": "invalid",
                "closure_decision": "valid",
            },
        },
    }
    assert packet["next_step_readiness"] == expected_readiness
    assert packet["validated_handoff"]["next_step_readiness"] == expected_readiness

    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )

    assert dry_run["next_step_readiness"] == expected_readiness


def test_l72_draft_scheduler_dry_run_blocks_when_packet_not_ready(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_dry_run_blocked"
    task_path = _make_l72_taskbook(tmp_path)
    packet = {
        "status": "blocked",
        "packet_ready": False,
        "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "task_draft_path": str(task_path),
        "claim_limits": ["no_scheduler_dispatch", "no_active_inbox_write"],
    }

    report = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )

    assert report["status"] == "blocked"
    assert report["dry_run_ready"] is False
    assert "admission_packet_not_ready" in report["blockers"]
    assert "admission_packet_status_not_ready" in report["blockers"]
    assert report["scheduler_instruction"] == "keep_l72_draft_blocked"


def test_l72_draft_dry_run_cli_writes_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_dry_run_cli"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    packet_path = output_dir / "l72_task_admission_packet.json"
    _write_json(packet_path, packet)

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "l72-draft-dry-run",
            "--admission-packet",
            str(packet_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run_ready"
    assert (output_dir / "l72_task_draft_scheduler_dry_run.json").exists()


def test_l72_stage_candidate_bridges_ready_draft_to_night_auto_pending(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_stage_candidate"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )

    report = stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_stage_test",
    )

    assert report["status"] == "staging_ready"
    assert report["staging_candidate_ready"] is True
    assert report["task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    staging = report["staging_result"]
    assert staging["status"] == "staged"
    assert staging["staged"] is True
    assert staging["candidate"]["continuation_lane"] == "night_auto"
    assert staging["candidate"]["mission_gate_pass"] is True
    assert staging["night_auto_work_order_status"] in {"created", "replayed"}
    assert staging["night_auto_pending_status"] in {"created", "replayed"}
    assert staging["night_auto_pending_artifact"]["scheduler_visible"] is False
    assert staging["night_auto_pending_artifact"]["side_effects"]["scheduler_dispatch"] is False
    assert staging["night_auto_pending_artifact"]["side_effects"]["active_inbox_write"] is False
    assert "tasks/inbox/_night_auto" not in staging["night_auto_pending_artifact_path"]
    assert not str(staging["staging_report_path"]).startswith(str(tmp_path / "tasks" / "inbox"))
    assert (output_dir / "l72_staging_candidate_report.json").exists()


def test_l72_stage_candidate_blocks_when_draft_dry_run_not_ready(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_stage_candidate_blocked"
    task_path = _make_l72_taskbook(tmp_path)
    packet = {
        "status": "blocked",
        "packet_ready": False,
        "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "task_draft_path": str(task_path),
    }
    dry_run = {
        "status": "blocked",
        "dry_run_ready": False,
        "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
    }

    report = stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_stage_blocked",
    )

    assert report["status"] == "blocked"
    assert report["staging_candidate_ready"] is False
    assert "admission_packet_not_ready" in report["blockers"]
    assert "draft_scheduler_dry_run_not_ready" in report["blockers"]
    assert report["staging_result"] is None


def test_l72_stage_candidate_preserves_b14_l74_readiness_summary_when_upstream_provides_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_stage_candidate_readiness"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    packet["validated_handoff"]["next_step_readiness"] = {
        "schema_version": 1,
        "can_stage": True,
        "route": "repair_once",
        "status": "ready",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once",
            "present_valid_artifact_ids": ["evidence_payload"],
            "missing_artifact_ids": ["metrics"],
            "invalid_artifact_ids": ["bbox_predictions"],
            "route_reasons": [
                "metric_evidence_missing_or_invalid",
                "bbox_evidence_missing_or_invalid",
            ],
            "artifact_statuses": {
                "metrics": "missing",
                "bbox_predictions": "invalid",
                "evidence_payload": "present",
            },
        },
    }
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )

    report = stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_stage_readiness",
    )

    readiness = report["candidate"]["next_step_readiness"]
    assert readiness["route"] == "repair_once"
    assert readiness["status"] == "ready"
    assert readiness["can_stage"] is True
    assert readiness["bounded_validation"] == {
        "bucket_id": "B14-L74",
        "status": "repair_once",
        "next_recommendation": "repair_once",
        "present_valid_artifact_ids": ["evidence_payload"],
        "missing_artifact_ids": ["metrics"],
        "invalid_artifact_ids": ["bbox_predictions"],
        "route_reasons": [
            "metric_evidence_missing_or_invalid",
            "bbox_evidence_missing_or_invalid",
        ],
        "artifact_statuses": {
            "metrics": "missing",
            "bbox_predictions": "invalid",
            "evidence_payload": "present",
        },
    }
    assert report["staging_result"]["candidate"]["next_step_readiness"] == readiness


def test_l72_stage_candidate_cli_writes_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_stage_candidate_cli"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )
    packet_path = output_dir / "l72_task_admission_packet.json"
    dry_run_path = output_dir / "l72_task_draft_scheduler_dry_run.json"

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "l72-stage-candidate",
            "--admission-packet",
            str(packet_path),
            "--draft-dry-run",
            str(dry_run_path),
            "--output-dir",
            str(output_dir),
            "--proposal-run-id",
            "b14_l72_stage_cli",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "staging_ready"
    assert payload["staging_result"]["night_auto_pending_status"] in {"created", "replayed"}
    assert (output_dir / "l72_staging_candidate_report.json").exists()


def test_l72_manual_launch_readiness_bridges_staged_candidate_to_launch_intent(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_manual_launch_ready"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )
    staging = stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_manual_launch_stage",
    )

    report = build_l72_manual_launch_readiness_from_stage(
        admission_packet=packet,
        draft_dry_run=dry_run,
        staging_candidate_report=staging,
        output_dir=output_dir,
        operator_confirmed=True,
    )

    assert report["status"] == "ready_for_human_approved_remote_launch"
    assert report["ready_for_manual_launch_preflight"] is True
    assert report["ready_for_human_approved_remote_launch"] is True
    assert report["blockers"] == []
    assert report["launch_intent"]["status"] == "ready_for_launch_preflight"
    assert report["launch_intent"]["path_authority"] == "task_contract_target_workplace"
    assert report["manual_launch_readiness"]["status"] == "ready_for_human_approved_remote_launch"
    assert report["manual_launch_readiness"]["remote_boundary"] == "not_reached"
    assert report["side_effects"]["ssh"] is False
    assert report["side_effects"]["remote_execution"] is False
    assert Path(report["launch_work_order_path"]).exists()
    assert Path(report["launch_intent_path"]).exists()
    assert Path(report["manual_launch_readiness_report_path"]).exists()


def test_l72_manual_launch_readiness_blocks_without_operator_confirmation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_manual_launch_blocked"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )
    staging = stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_manual_launch_blocked",
    )

    report = build_l72_manual_launch_readiness_from_stage(
        admission_packet=packet,
        draft_dry_run=dry_run,
        staging_candidate_report=staging,
        output_dir=output_dir,
        operator_confirmed=False,
    )

    assert report["status"] == "blocked"
    assert report["ready_for_manual_launch_preflight"] is True
    assert "explicit_manual_launch_confirmation_required" in report["blockers"]
    assert report["launch_intent"]["status"] == "ready_for_launch_preflight"
    assert report["manual_launch_readiness"]["status"] == "blocked"


def test_l72_manual_launch_readiness_cli_writes_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    output_dir = tmp_path / "continuation" / "l72_manual_launch_cli"
    packet = build_l72_task_admission_packet(
        admission_rehearsal={
            "status": "admission_ready",
            "admission_stageable": True,
            "mission_id": "B14_night_research_v1",
            "source_task_id": "b14_l70_large_image_coordinate_protocol_audit",
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "required_evidence": {"patch_fingerprint_ok": True},
        },
        task_path=_make_l72_taskbook(tmp_path),
        output_dir=output_dir,
    )
    dry_run = dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )
    stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id="b14_l72_manual_launch_cli",
    )

    result = CliRunner().invoke(
        cli,
        [
            "source-repair",
            "l72-manual-launch-readiness",
            "--admission-packet",
            str(output_dir / "l72_task_admission_packet.json"),
            "--draft-dry-run",
            str(output_dir / "l72_task_draft_scheduler_dry_run.json"),
            "--staging-candidate-report",
            str(output_dir / "l72_staging_candidate_report.json"),
            "--output-dir",
            str(output_dir),
            "--operator-confirmed",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready_for_human_approved_remote_launch"
    assert Path(payload["manual_launch_readiness_report_path"]).exists()

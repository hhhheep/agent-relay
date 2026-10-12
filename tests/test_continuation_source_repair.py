from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from solution_agent.continuation import source_repair
from solution_agent.continuation.source_repair import (
    build_repair_design_validation_report,
    build_rerun_intent_from_repair_design_validation,
    build_repair_design_packet,
    build_source_repair_plan,
    build_source_repair_plan_from_action_request,
    build_source_repair_plan_from_taskbook,
    materialize_source_repair_patch,
    render_codex_patch_request,
    write_repair_design_packet,
    write_repair_design_validation,
    write_source_repair_plan,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_result_dir(tmp_path: Path, *, task_id: str = "b14_l72_large_image_bbox_sidecar_smoke") -> Path:
    result = tmp_path / task_id
    result.mkdir(parents=True, exist_ok=True)
    return result


def test_build_source_repair_plan_detects_source_actions_from_action_requests_json(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path)
    _write_json(
        artifacts / "action_requests.json",
        [
            {"kind": "missing_artifact", "message": "Missing cached masks for this input set."},
            {"kind": "missing_capability", "message": "Fix `solution_agent/execution_port/script_registry.yaml`"},
            {"kind": "closure", "message": "Please edit source code path `utils/b14_bbox_sidecar.py`"},
            {"kind": "unknown", "message": "Only non-source context text."},
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "b14_l72_large_image_bbox_sidecar_smoke", "status": "partial", "reason": "missing_capability"},
    )

    plan = build_source_repair_plan(artifacts)

    assert plan["status"] == "needs_source_repair"
    assert plan["needs_source_repair"] is True
    source_actions = plan["codex_task_intent"]["action_requests"]
    assert {item["kind"] for item in source_actions} == {"missing_capability", "closure"}
    assert plan["proposed_patch_scope"]["files_or_modules"] == sorted({"solution_agent/execution_port/script_registry.yaml", "utils/b14_bbox_sidecar.py"})
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["executor_route_hint"] in {"bounded_self_repair", "codex"}


def test_build_source_repair_plan_falls_back_to_inline_decision_summary_action_requests(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path)
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "decision_inline_sources_001",
            "status": "partial",
            "reason": "missing_capability",
            "action_requests": [
                {"kind": "source_code_required", "message": "Update docs/repair_instructions.md and `.solution/continuation/review.yaml`."},
            ],
        },
    )

    plan = build_source_repair_plan(artifacts, source_task_id="decision_inline_sources_001")

    assert plan["source_task_id"] == "decision_inline_sources_001"
    assert plan["status"] == "needs_source_repair"
    assert plan["needs_source_repair"] is True
    assert len(plan["codex_task_intent"]["action_requests"]) == 1
    assert plan["codex_task_intent"]["action_requests"][0]["kind"] == "source_code_required"
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["executor_route_hint"] == "deterministic_self"
    assert plan["patch_intent"]["tier"] == "P0"


def test_build_source_repair_plan_normalizes_legacy_action_request_fields(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="legacy_fields")
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": "Missing --large-image-mode support from sidecar runner.",
                "reason": "Need bounded smoke compatibility with L71 CLI.",
            },
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "legacy_fields", "status": "partial", "reason": "missing_capability"},
    )

    plan = build_source_repair_plan(artifacts, source_task_id="legacy_fields")
    action = plan["codex_task_intent"]["action_requests"][0]

    assert action["kind"] == "missing_capability"
    assert action["message"] == "Missing --large-image-mode support from sidecar runner."
    assert (
        action["missing_or_needed"]
        == "Missing --large-image-mode support from sidecar runner."
    )
    assert action["why_needed"] == "Need bounded smoke compatibility with L71 CLI."


def test_build_source_repair_plan_no_source_repair_when_only_non_source_actions(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="non_source_only")
    _write_json(
        artifacts / "action_requests.json",
        [
            {"kind": "missing_artifact", "message": "Missing cache dir in outputs."},
            {"kind": "missing_dataset", "message": "Dataset file path not provided."},
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "non_source_only", "status": "completed"},
    )

    plan = build_source_repair_plan(artifacts, source_task_id="non_source_only")

    assert plan["status"] == "no_source_repair_needed"
    assert plan["needs_source_repair"] is False
    assert plan["codex_task_intent"]["action_requests"] == []
    assert plan["patch_intent"] is None
    assert "source_edit" in plan["safety"]
    assert plan["safety"]["source_edit"] is False


def test_build_source_repair_plan_generates_deterministic_patch_intent(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="deterministic_intent")
    _write_json(
        artifacts / "action_requests.json",
        [
            {"kind": "missing_capability", "message": "Update `.solution/knowledge/_proposed/boundary.yaml` to pass"},
        ],
    )
    _write_json(artifacts / "decision_summary.json", {"task_id": "deterministic_intent", "status": "partial", "reason": "missing_capability"})

    plan = build_source_repair_plan(artifacts, source_task_id="deterministic_intent")
    patch_intent = plan["patch_intent"]

    assert patch_intent is not None
    assert patch_intent["executor_route_hint"] == "deterministic_self"
    assert patch_intent["tier"] == "P0"
    assert patch_intent["target_file"] == ".solution/knowledge/_proposed/boundary.yaml"
    assert patch_intent["operation"] == "insert_after"
    assert patch_intent["expected_validation"]["must_change_only"] == [".solution/knowledge/_proposed/boundary.yaml"]


def test_build_source_repair_plan_generates_bounded_patch_intent_for_registry_file(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="bounded_intent")
    _write_json(
        artifacts / "action_requests.json",
        [
            {"kind": "request_codex_review", "message": "Repair `solution_agent/execution_port/script_registry.yaml` entry alignment."},
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "bounded_intent", "status": "partial", "reason": "request_codex_review"},
    )

    plan = build_source_repair_plan(artifacts, source_task_id="bounded_intent")
    patch_intent = plan["patch_intent"]

    assert patch_intent is not None
    assert patch_intent["executor_route_hint"] == "bounded_self_repair"
    assert patch_intent["tier"] == "P1"
    assert patch_intent["target_file"] == "solution_agent/execution_port/script_registry.yaml"


def test_build_source_repair_plan_routes_b14_sidecar_module_to_bounded_self_repair(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="b14_sidecar_repair")
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": (
                    "Create `B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py` "
                    "as an optional report_only sidecar; default mainline behavior must remain unchanged."
                ),
            },
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "b14_sidecar_repair", "status": "partial", "reason": "missing_capability"},
    )

    plan = build_source_repair_plan(artifacts, source_task_id="b14_sidecar_repair")

    assert plan["recommended_worker"] == "bounded_self_repair"
    assert plan["planner_version"] == source_repair.SOURCE_REPAIR_PLANNER_VERSION
    assert plan["repair_type"] == "sandbox_thin_wrapper_missing"
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["executor_route_hint"] == "bounded_self_repair"
    assert plan["patch_intent"]["operation"] == "create_file"
    assert plan["patch_intent"]["target_file"] == "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py"


def test_build_source_repair_plan_from_b14_l71_taskbook_pushes_without_codex_default(tmp_path: Path) -> None:
    task_path = tmp_path / "b14_l71.md"
    task_path.write_text(
        """---
task_id: b14_l71_bbox_sidecar_large_image_cli_integration
title: Implement bbox sidecar large-image CLI integration
intent: source_change
---

Create a reusable module:

`B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py`

Add optional report_only CLI flags:

`--enable-bbox-sidecar`

Default behavior must be unchanged and mainline masks must not change.
""",
        encoding="utf-8",
    )

    plan = build_source_repair_plan_from_taskbook(
        task_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    assert plan["source_task_id"] == "b14_l71_bbox_sidecar_large_image_cli_integration"
    assert plan["repair_type"] == "sandbox_thin_wrapper_missing"
    assert plan["recommended_worker"] == "bounded_self_repair"
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["executor_route_hint"] == "bounded_self_repair"
    assert plan["patch_intent"]["operation"] == "create_file"
    assert "def decode_p2_bbox_candidates" in plan["patch_intent"]["new_text"]
    assert plan["rerun_intent"]["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"


def test_build_source_repair_plan_from_pending_l72_action_request_uses_result_artifacts(tmp_path: Path) -> None:
    result_dir = _make_result_dir(tmp_path, task_id="b14_l72_large_image_bbox_sidecar_smoke")
    artifacts = result_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": "Missing optional --large-image-mode CLI flag from L71.",
                "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
            },
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "partial",
            "reason": "missing_capability",
            "next_action": "complete_l71_or_add_dedicated_allowlisted_smoke_runner",
        },
    )
    action_request_path = tmp_path / "act_l72.json"
    _write_json(
        action_request_path,
        {
            "action_id": "act_l72",
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "pending",
            "lifecycle_status": "open",
            "payload": {
                "source_run_id": "b14_l72_large_image_bbox_sidecar_smoke__a03",
                "source_result_path": str(result_dir),
                "kind": "missing_capability",
                "missing_or_needed": "Missing optional --large-image-mode CLI flag from L71.",
                "why_needed": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
            },
        },
    )

    plan = build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    assert plan["status"] == "needs_source_repair"
    assert plan["needs_source_repair"] is True
    assert plan["source_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert plan["source_action_request"]["action_id"] == "act_l72"
    assert plan["source_action_request"]["source_run_id"] == "b14_l72_large_image_bbox_sidecar_smoke__a03"
    assert str(action_request_path) in plan["required_context"]
    assert plan["proposed_patch_scope"]["cli_flags"] == ["--large-image-mode"]
    assert plan["repair_type"] == "sandbox_cli_flag_integration"
    assert plan["rerun_intent"]["upstream_source_repair_task_id"] == "b14_l71_bbox_sidecar_large_image_cli_integration"
    assert plan["rerun_intent"]["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert plan["safety"]["remote_execution"] is False
    assert plan["safety"]["scheduler_dispatch"] is False


def test_build_source_repair_plan_from_pending_action_request_rejects_corrupt_payload_schema(tmp_path: Path) -> None:
    action_request_path = tmp_path / "act_l72_corrupt.json"
    _write_json(
        action_request_path,
        {
            "action_id": "act_l72_corrupt",
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "pending",
            "lifecycle_status": "open",
            "payload": [
                {
                    "kind": "missing_capability",
                    "message": "This should be treated as malformed schema and still be safe.",
                    "missing_or_needed": "Missing optional --large-image-mode CLI flag from L71.",
                }
            ],
            "reason": "Malformed action_request payload format; need --large-image-mode support in L71 sidecar.",
        },
    )

    plan = build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    assert plan["status"] == "needs_source_repair"
    assert plan["needs_source_repair"] is True
    assert plan["repair_type"] == "sandbox_cli_flag_integration"
    assert plan["recommended_worker"] == "codex"
    assert "pending_action_request_payload_invalid" in plan["blocked_by"]
    assert plan["source_action_request"]["action_id"] == "act_l72_corrupt"
    assert plan["source_action_request"]["source_result_path"] == ""


def test_build_source_repair_plan_from_action_request_normalizes_legacy_fields(tmp_path: Path) -> None:
    action_request_path = tmp_path / "act_l72_legacy.json"
    _write_json(
        action_request_path,
        {
            "action_id": "act_l72_legacy",
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "pending",
            "lifecycle_status": "open",
            "payload": {
                "source_run_id": "b14_l72_large_image_bbox_sidecar_smoke__legacy",
                "kind": "missing_capability",
                "message": "Need optional --large-image-mode CLI support for sidecar path.",
                "reason": "B14-L72 smoke was bounded by capability gap only.",
            },
        },
    )

    plan = build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    action = plan["codex_task_intent"]["action_requests"][0]
    assert action["kind"] == "missing_capability"
    assert action["missing_or_needed"] == "Need optional --large-image-mode CLI support for sidecar path."
    assert action["why_needed"] == "B14-L72 smoke was bounded by capability gap only."
    assert action["source_action_request_path"] == str(action_request_path)
    assert plan["source_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"


def test_build_source_repair_plan_from_action_request_requires_upstream_task_for_sandbox_cli_flag_route(tmp_path: Path) -> None:
    action_request_path = tmp_path / "act_l72_upstream_missing.json"
    _write_json(
        action_request_path,
        {
            "action_id": "act_l72_upstream_missing",
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "pending",
            "lifecycle_status": "open",
            "payload": {
                "source_run_id": "b14_l72_large_image_bbox_sidecar_smoke__u001",
                "kind": "missing_capability",
                "message": (
                    "Missing optional --large-image-mode CLI flag from sidecar path. "
                    "Need a safe capability bridge before L72 metrics claim."
                ),
                "why_needed": "No upstream repair task has been completed yet.",
            },
        },
    )

    plan = build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    assert plan["needs_source_repair"] is True
    assert plan["repair_type"] == "sandbox_cli_flag_integration"
    assert plan["rerun_intent"]["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert plan["rerun_intent"]["upstream_source_repair_task_id"] == ""
    assert "rerun_intent_missing_upstream_source_repair_task_id" in plan["blocked_by"]
    assert plan["recommended_worker"] == "codex"


def test_build_source_repair_plan_from_action_request_allows_sidecar_wrapper_without_upstream_task(tmp_path: Path) -> None:
    action_request_path = tmp_path / "act_l72_sidecar_wrapper.json"
    _write_json(
        action_request_path,
        {
            "action_id": "act_l72_sidecar_wrapper",
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "pending",
            "lifecycle_status": "open",
            "payload": {
                "source_run_id": "b14_l72_large_image_bbox_sidecar_smoke__wrapper001",
                "kind": "missing_capability",
                "message": (
                    "Create `B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py` "
                    "as a report-only bounded sidecar helper. Default B14 mainline behavior "
                    "must remain unchanged."
                ),
                "why_needed": "L72 needs bounded sidecar evidence before any metric claim.",
            },
        },
    )

    plan = build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    assert plan["needs_source_repair"] is True
    assert plan["repair_type"] == "sandbox_thin_wrapper_missing"
    assert plan["recommended_worker"] == "bounded_self_repair"
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["target_file"] == "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py"
    assert plan["patch_intent"]["operation"] == "create_file"
    assert plan["rerun_intent"]["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert "rerun_intent_missing_upstream_source_repair_task_id" not in plan.get("blocked_by", [])
    assert plan["safety"]["scheduler_dispatch"] is False
    assert plan["safety"]["remote_execution"] is False


def test_repair_design_packet_decomposes_l72_large_image_mode_without_codex_as_decider(tmp_path: Path) -> None:
    result_dir = _make_result_dir(tmp_path, task_id="b14_l72_large_image_bbox_sidecar_smoke")
    artifacts = result_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": "Missing optional --large-image-mode CLI flag from L71.",
                "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
            },
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "status": "partial",
            "reason": "missing_capability",
            "next_action": "complete_l71_or_add_dedicated_allowlisted_smoke_runner",
        },
    )
    plan = build_source_repair_plan(
        result_dir,
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
    )

    design = build_repair_design_packet(plan)

    assert design["artifact_type"] == "repair_design_packet"
    assert design["problem_statement"]["classification"] == "capability_gap"
    assert design["problem_statement"]["not_a_model_quality_failure"] is True
    option_ids = {item["id"] for item in design["candidate_options"]}
    assert {
        "dedicated_allowlisted_smoke_runner",
        "report_only_entrypoint_flags",
        "sidecar_helper_only",
    }.issubset(option_ids)
    assert design["minimal_bounded_experiment"]["preferred_option"] == "dedicated_allowlisted_smoke_runner"
    assert "preserves_default_B14_mainline_behavior" in design["fair_comparison_protocol"]["compare_options_on"]
    assert "default_mainline_behavior_change_needed" in design["decision_rule"]["ask_human_if"]
    assert "claim_formal_bbox_success_from_smoke" in design["decision_rule"]["must_not_do"]
    assert design["rerun_intent"]["allowed_only_after_validation"] is True
    assert design["side_effects"]["remote_execution"] is False
    assert design["side_effects"]["scheduler_dispatch"] is False


def test_write_repair_design_packet_is_idempotent(tmp_path: Path) -> None:
    plan = {
        "plan_id": "srp_l72_design",
        "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "trigger_kind": "missing_capability",
        "repair_type": "sandbox_cli_flag_integration",
        "proposed_patch_scope": {"files_or_modules": [], "cli_flags": ["--large-image-mode"]},
        "rerun_intent": {
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "upstream_source_repair_task_id": "b14_l71_bbox_sidecar_large_image_cli_integration",
            "after_repair": "rerun_downstream_smoke_or_metric_task",
        },
        "codex_task_intent": {
            "action_requests": [
                {
                    "kind": "missing_capability",
                    "message": "Missing optional --large-image-mode CLI flag from L71.",
                    "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
                },
            ],
        },
    }

    first = write_repair_design_packet(plan, output_dir=tmp_path)
    second = write_repair_design_packet(plan, output_dir=tmp_path)

    assert first["status"] == "written"
    assert second["status"] == "replayed"
    assert Path(first["path"]).exists()
    assert first["side_effects"]["source_edit"] is False


def _make_l72_repair_design_packet() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "repair_design_packet",
        "design_id": "rdp_l72_test",
        "source_repair_plan_id": "srp_l72_test",
        "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "repair_type": "sandbox_cli_flag_integration",
        "problem_statement": {
            "classification": "capability_gap",
            "current_claim_limit": "no_metric_claim",
            "not_a_model_quality_failure": True,
        },
        "minimal_bounded_experiment": {
            "preferred_option": "dedicated_allowlisted_smoke_runner",
            "required_checks": [
                "sidecar_module_import_or_runner_help",
                "large_image_mode_config_or_flag_detected",
                "report_only_default_behavior",
                "no_formal_claim",
                "rerun_target_is_b14_l72_large_image_bbox_sidecar_smoke",
            ],
            "required_outputs": [
                "validation_report.json",
                "claim_boundary_report.json",
                "rerun_intent.json",
            ],
        },
        "rerun_intent": {
            "downstream_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "upstream_source_repair_task_id": "b14_l71_bbox_sidecar_large_image_cli_integration",
            "after_repair": "rerun_downstream_smoke_or_metric_task",
            "allowed_only_after_validation": True,
        },
        "side_effects": {
            "source_edit": False,
            "inbox_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
            "subprocess": False,
            "api_call": False,
        },
    }


def test_repair_design_validation_blocks_l72_when_large_image_mode_not_supported(tmp_path: Path) -> None:
    runner = tmp_path / "scripts" / "reports" / "run_b14_large_image_bbox_sidecar_smoke.py"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--output_dir")
CLAIM = "no_metric_claim"
NOTE = "This wrapper did not fabricate bbox metrics."
""",
        encoding="utf-8",
    )

    report = build_repair_design_validation_report(_make_l72_repair_design_packet(), repo_root=tmp_path)

    assert report["status"] == "blocked"
    assert report["can_emit_rerun_intent"] is False
    assert "large_image_mode_config_missing" in report["blockers"]
    assert "sidecar_helper_missing_local" in report["blockers"]
    assert build_rerun_intent_from_repair_design_validation(_make_l72_repair_design_packet(), report) is None


def test_repair_design_validation_passes_and_emits_claim_limited_rerun_intent(tmp_path: Path) -> None:
    runner = tmp_path / "scripts" / "reports" / "run_b14_large_image_bbox_sidecar_smoke.py"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--large-image-mode")
parser.add_argument("--output_dir")
CLAIM = "no_metric_claim"
claim_limit = "no_metric_claim"
NOTE = "This report-only runner did not fabricate bbox metrics."
""",
        encoding="utf-8",
    )
    sidecar = tmp_path / "B14_spatial_grace_graph_utility" / "utils" / "b14_bbox_sidecar.py"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("def decode_p2_bbox_candidates(*args, **kwargs):\n    return []\n", encoding="utf-8")

    design = _make_l72_repair_design_packet()
    report = build_repair_design_validation_report(design, repo_root=tmp_path)
    rerun_intent = build_rerun_intent_from_repair_design_validation(design, report)

    assert report["status"] == "passed"
    assert report["can_continue_autonomously"] is True
    assert rerun_intent is not None
    assert rerun_intent["allowed_to_dispatch"] is False
    assert rerun_intent["requires_staging_gate"] is True
    assert "no formal bbox" in rerun_intent["claim_limit"]


def test_write_repair_design_validation_is_idempotent_and_claim_limited(tmp_path: Path) -> None:
    runner = tmp_path / "repo" / "scripts" / "reports" / "run_b14_large_image_bbox_sidecar_smoke.py"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--large-image-mode")
CLAIM = "no_metric_claim"
claim_limit = "no_metric_claim"
NOTE = "This report-only runner did not fabricate bbox metrics."
""",
        encoding="utf-8",
    )
    sidecar = tmp_path / "repo" / "B14_spatial_grace_graph_utility" / "utils" / "b14_bbox_sidecar.py"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("def decode_p2_bbox_candidates(*args, **kwargs):\n    return []\n", encoding="utf-8")

    first = write_repair_design_validation(_make_l72_repair_design_packet(), output_dir=tmp_path / "out", repo_root=tmp_path / "repo")
    second = write_repair_design_validation(_make_l72_repair_design_packet(), output_dir=tmp_path / "out", repo_root=tmp_path / "repo")

    assert first["status"] == "written"
    assert second["status"] == "replayed"
    assert first["validation_status"] == "passed"
    assert Path(first["validation_path"]).exists()
    assert Path(first["claim_boundary_path"]).exists()
    assert first["rerun_intent_path"] is not None
    assert first["claim_boundary"]["formal_claim_allowed"] is False


def test_l72_report_only_sidecar_smoke_produces_bounded_evidence(tmp_path: Path) -> None:
    output_dir = tmp_path / "l72_out"
    script = Path("scripts/reports/run_b14_large_image_bbox_sidecar_smoke.py")
    taskbook = tmp_path / "l71.md"
    taskbook.write_text(
        "---\ntask_id: b14_l71_bbox_sidecar_large_image_cli_integration\n"
        "title: Sidecar fixture\nintent: source_change\n---\n"
        "Create `B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py`.\n",
        encoding="utf-8",
    )
    plan = build_source_repair_plan_from_taskbook(taskbook)
    intent = plan["patch_intent"]
    sidecar = tmp_path / intent["target_file"]
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(intent["new_text"], encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output_dir",
            str(output_dir),
            "--workspace_root",
            str(tmp_path),
            "--max_images",
            "2",
            "--large-image-mode",
            "audit_only",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    decision = json.loads((output_dir / "decision_summary.json").read_text(encoding="utf-8"))
    assert decision["status"] == "completed", decision
    assert decision["evidence_status"] == "bounded_interface_smoke"
    assert decision["claim_limit"] == "no_metric_claim"
    assert decision["bbox_sidecar_jsonl_produced"] is True
    assert decision["smoke_metrics"]["candidate_count"] > 0
    assert decision["smoke_metrics"]["formal_claim_allowed"] is False
    assert json.loads((output_dir / "action_requests.json").read_text(encoding="utf-8")) == []
    predictions = (output_dir / "bbox_predictions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert predictions
    first_prediction = json.loads(predictions[0])
    assert first_prediction["source"] == "synthetic_report_only_sidecar_smoke"


def test_materialize_source_repair_patch_writes_diff_without_applying(tmp_path: Path) -> None:
    task_path = tmp_path / "b14_l71.md"
    task_path.write_text(
        """---
task_id: b14_l71_bbox_sidecar_large_image_cli_integration
title: Implement bbox sidecar large-image CLI integration
intent: source_change
---

Create `B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py`.
""",
        encoding="utf-8",
    )
    plan = build_source_repair_plan_from_taskbook(task_path)
    output_dir = tmp_path / "patch_artifact"

    result = materialize_source_repair_patch(plan, output_dir=output_dir, repo_root=tmp_path)

    assert result["status"] == "written"
    assert result["ok"] is True
    patch_path = Path(result["patch_path"])
    quality_path = Path(result["quality_report_path"])
    assert patch_path.exists()
    assert quality_path.exists()
    assert "new file mode 100644" in patch_path.read_text(encoding="utf-8")
    assert not (tmp_path / "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py").exists()


def test_build_source_repair_plan_request_codex_review_with_fenced_file_is_routed_to_codex(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="codex_fenced_route")
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "request_codex_review",
                "message": "Runtime behavior change needed in `solution_agent/orchestrator/core.py`.",
            },
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {"task_id": "codex_fenced_route", "status": "partial", "reason": "request_codex_review"},
    )

    plan = build_source_repair_plan(artifacts, source_task_id="codex_fenced_route")

    assert plan["needs_source_repair"] is True
    assert plan["trigger_kind"] == "request_codex_review"
    assert plan["patch_intent"] is None
    assert plan["proposed_patch_scope"]["files_or_modules"] == ["solution_agent/orchestrator/core.py"]
    assert "source_behavior_change" not in [item["kind"] for item in plan["codex_task_intent"]["action_requests"]]


def test_build_source_repair_plan_request_codex_review_with_fenced_action_requests_remains_codex_route_safe(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="codex_fenced_route_safe")
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "codex_fenced_route_safe",
            "status": "partial",
            "reason": "request_codex_review",
            "action_requests": [
                {
                    "kind": "request_codex_review",
                    "message": "Runtime change needed in `solution_agent/orchestrator/core.py` and `solution_agent/remote_runner/runner.py`.",
                },
            ],
        },
    )

    plan = build_source_repair_plan(artifacts, source_task_id="codex_fenced_route_safe")
    safety = plan["safety"]

    assert plan["needs_source_repair"] is True
    assert plan["trigger_kind"] == "request_codex_review"
    assert plan["recommended_worker"] == "codex"
    assert plan["patch_intent"] is None
    assert plan["proposed_patch_scope"]["files_or_modules"] == [
        "solution_agent/orchestrator/core.py",
        "solution_agent/remote_runner/runner.py",
    ]
    assert safety["source_edit"] is False
    assert safety["scheduler_dispatch"] is False
    assert safety["no_dispatch_performed"] is True
    assert safety["no_inbox_write"] is True
    assert safety["remote_execution"] is False
    assert safety["api_call"] is False


def test_build_source_repair_plan_with_inline_action_request_key_is_handled(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="inline_request_key")
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "inline_request_key",
            "status": "partial",
            "reason": "missing_capability",
            "action_request": [{"kind": "request_codex_review", "message": "Runtime fix needed in `.solution/knowledge/_proposed/review.yaml`."}],
        },
    )

    plan = build_source_repair_plan(artifacts, source_task_id="inline_request_key")

    assert plan["status"] == "needs_source_repair"
    assert plan["needs_source_repair"] is True
    assert len(plan["codex_task_intent"]["action_requests"]) == 1
    assert plan["codex_task_intent"]["action_requests"][0]["kind"] == "request_codex_review"
    assert plan["patch_intent"] is not None
    assert plan["patch_intent"]["executor_route_hint"] == "deterministic_self"
    assert plan["patch_intent"]["tier"] == "P0"


def test_build_source_repair_plan_blocks_with_extra_blockers_and_preserves_no_side_effects(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="blocked_repair")
    _write_json(
        artifacts / "action_requests.json",
        [{"kind": "missing_capability", "message": "Need source file `solution_agent/execution_port/script_registry.yaml`."}],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {
            "task_id": "blocked_repair",
            "status": "partial",
            "reason": "missing_capability",
            "codex_remote_ready": False,
            "workdir_is_git_worktree": False,
        },
    )

    plan = build_source_repair_plan(
        artifacts,
        source_task_id="blocked_repair",
        extra_blockers=["registry_contract_locked"],
    )

    assert plan["needs_source_repair"] is True
    assert plan["blocked_by"] == sorted(["remote_codex_unavailable", "registry_contract_locked", "remote_workdir_not_git_worktree"])
    safety = plan["safety"]
    assert safety["source_edit"] is False
    assert safety["scheduler_dispatch"] is False
    assert safety["no_dispatch_performed"] is True
    assert safety["remote_execution"] is False
    assert safety["api_call"] is False
    assert safety["no_inbox_write"] is True


def test_render_codex_patch_request_includes_inline_actions_and_patch_intent_route(tmp_path: Path) -> None:
    artifacts = _make_result_dir(tmp_path, task_id="render_patch")
    _write_json(
        artifacts / "action_requests.json",
        [{"kind": "missing_capability", "message": "Repair `.solution/continuation/patch_intent_notes.md`."}],
    )
    _write_json(artifacts / "decision_summary.json", {"task_id": "render_patch", "status": "partial"})

    plan = build_source_repair_plan(artifacts, source_task_id="render_patch")
    rendered = render_codex_patch_request(plan)

    assert "# Source Repair Request" in rendered
    assert "Codex is an escalation lane, not the default executor." in rendered
    assert "## Action Requests" in rendered
    assert "- `missing_capability`" in rendered
    assert "## Patch Intent" in rendered
    assert "route_hint:" in rendered
    assert "`deterministic_self`" in rendered


def test_write_source_repair_plan_is_idempotent_and_reports_non_mutating_side_effects(tmp_path: Path) -> None:
    task_id = f"write_plan_{tmp_path.name}"
    artifacts = _make_result_dir(tmp_path, task_id=task_id)
    _write_json(
        artifacts / "action_requests.json",
        [{"kind": "missing_capability", "message": "Repair `.solution/continuation/source_repair_write.md`."}],
    )
    _write_json(artifacts / "decision_summary.json", {"task_id": task_id, "status": "partial"})

    plan = build_source_repair_plan(artifacts, source_task_id=task_id)

    first = write_source_repair_plan(plan)
    replay = write_source_repair_plan(plan)

    assert first["status"] in {"written", "replayed"}
    assert first["patch_intent_path"] is not None
    assert first["side_effects"]["source_edit"] is False
    assert first["side_effects"]["scheduler_dispatch"] is False
    assert first["side_effects"]["inbox_write"] is False
    assert first["side_effects"]["registry_mutation"] is False
    assert first["side_effects"]["remote_execution"] is False
    assert first["side_effects"]["api_call"] is False
    assert replay["status"] == "replayed"

from __future__ import annotations

import json
from pathlib import Path

import yaml
from solution_agent.continuation.source_repair import build_source_repair_plan_from_action_request

from solution_agent.continuation.repair_executor_selector import (
    select_repair_executor,
    write_repair_executor_decision,
    write_repair_execution_artifacts,
    read_repair_result_artifact,
    read_rerun_intent_artifact,
)


POLICY_PATH = Path(".solution/continuation/repair_executor_policy.yaml")


def _base_plan() -> dict:
    return {
        "status": "needs_source_repair",
        "source_task_id": "task_001",
        "trigger_kind": "missing_capability",
        "proposed_patch_scope": {
            "files_or_modules": ["solution_agent/execution_port/script_registry.yaml"],
            "cli_flags": [],
        },
        "rerun_intent": {
            "downstream_task_id": "task_001",
            "upstream_source_repair_task_id": "srp_base_task_001",
            "after_repair": "rerun_downstream_smoke_or_metric_task",
        },
    }


def _valid_patch_intent(route_hint: str, target_file: str) -> dict:
    return {
        "schema_version": 1,
        "intent_id": f"patch_intent_task_001_{route_hint}",
        "tier": "P0" if route_hint == "deterministic_self" else "P1",
        "target_file": target_file,
        "operation": "insert_after",
        "anchor": {"type": "literal", "text": "route marker", "occurrence": "first"},
        "new_text": "### source repair placeholder",
        "reason": "bounded placeholder",
        "expected_validation": {
            "must_apply_cleanly": True,
            "must_change_only": [target_file],
            "required_tests": [],
        },
        "risk": {
            "source_code_behavior_change": False,
            "multi_file": False,
            "requires_codex": False,
        },
        "executor_route_hint": route_hint,
    }


def test_policy_contract_contains_routing_budget_and_reward_hack_guards() -> None:
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))

    assert set(policy["routes"]) == {"deterministic_self", "bounded_self_repair", "codex", "human", "stop"}
    assert policy["self_repair_budget"]["max_attempts_per_failure"] == 1
    assert "solution_agent/orchestrator/" in policy["self_repair_budget"]["forbidden_dirs"]
    assert policy["reward_hack_checks"]["forbid_test_deletion"] is True
    assert policy["reward_hack_checks"]["forbid_required_metric_downgrade"] is True
    assert policy["reward_hack_checks"]["forbid_approval_relaxation"] is True
    assert "human_decision_required" in policy["stop_conditions"]
    assert policy["autonomy_bias"]["codex_is_escalation_not_default"] is True
    assert "scripts/b14_probe/" in policy["self_repair_budget"]["allowed_dirs"]


def test_missing_expected_metrics_routes_bounded_self_when_safe_and_tested() -> None:
    decision = select_repair_executor(
        {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")},
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["decision"]["confidence"] == "high"
    assert decision["missing_tests"] == []
    assert decision["preflight_required"]
    assert decision["side_effects"]["source_edit"] is False
    assert decision["on_success"]["produce"] == ["repair_result.yaml", "rerun_intent.yaml"]
    assert decision["on_success"]["dispatch"] is False


def test_repair_type_case_normalization_preserves_expected_route() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("deterministic_self", "docs/repair_note.yaml"),
            "proposed_patch_scope": {"files_or_modules": ["docs/repair_note.yaml"]},
        },
        repair_type="Closure_Artifact_Repair",
        available_tests=[
            "action_request_schema_test",
            "decision_json_schema_test",
            "continuation_bundle_gate_test",
        ],
    )

    assert decision["decision"]["route"] == "deterministic_self"
    assert decision["repair_type"] == "closure_artifact_repair"


def test_route_hint_case_normalization_does_not_block_self_repair() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("BOUNDED_SELF_REPAIR", "solution_agent/execution_port/script_registry.yaml"),
            "proposed_patch_scope": {"files_or_modules": ["solution_agent/execution_port/script_registry.yaml"]},
        },
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["repair_type"] == "registry_expected_outputs_missing"
    assert all(not item.startswith("patch_intent_route_hint_mismatch") for item in decision["blockers"])


def test_closure_artifact_routes_deterministic_self() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("deterministic_self", ".solution/continuation/repair_note.yaml"),
            "proposed_patch_scope": {"files_or_modules": [".solution/continuation/repair_note.yaml"]},
        },
        repair_type="closure_artifact_repair",
        available_tests=[
            "action_request_schema_test",
            "decision_json_schema_test",
            "continuation_bundle_gate_test",
        ],
    )

    assert decision["decision"]["route"] == "deterministic_self"
    assert "repair_type_is_template_or_schema_only" in decision["decision"]["reason"]


def test_missing_cli_flag_runtime_change_routes_codex() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "proposed_patch_scope": {
                "files_or_modules": ["B14_spatial_grace_graph_utility/scripts/main.py"],
                "cli_flags": ["--enable-bbox-sidecar"],
            },
        },
        repair_type="missing_cli_flag_runtime_change",
        available_tests=[],
    )

    assert decision["decision"]["route"] == "codex"
    assert decision["preflight_required"] == []
    assert "source_behavior_change" in decision["forbidden"]


def test_sandbox_cli_flag_integration_routes_bounded_self_when_inside_mission_envelope() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("bounded_self_repair", "scripts/b14_probe/bbox_sidecar_smoke.py"),
            "proposed_patch_scope": {
                "files_or_modules": ["scripts/b14_probe/bbox_sidecar_smoke.py"],
                "cli_flags": ["--enable-bbox-sidecar"],
            },
        },
        repair_type="sandbox_cli_flag_integration",
        available_tests=[
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["missing_tests"] == []
    assert decision["preflight_required"]
    assert "repair_type_is_low_risk_bounded_and_testable" in decision["decision"]["reason"]


def test_sandbox_cli_flag_integration_flags_only_without_patch_intent_routes_bounded_self() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": None,
            "proposed_patch_scope": {
                "files_or_modules": [],
                "cli_flags": ["--large-image-sidecar"],
            },
        },
        repair_type="sandbox_cli_flag_integration",
        available_tests=[
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["missing_tests"] == []
    assert decision["blockers"] == []
    assert "patch_intent_missing_for_self_repair" not in decision["blockers"]


def test_sandbox_cli_flag_integration_flags_only_without_upstream_source_repair_task_id_routes_codex() -> None:
    plan = {
        **_base_plan(),
        "patch_intent": None,
        "proposed_patch_scope": {
            "files_or_modules": [],
            "cli_flags": ["--large-image-sidecar"],
        },
    }
    plan["rerun_intent"]["upstream_source_repair_task_id"] = ""
    decision = select_repair_executor(
        plan,
        repair_type="sandbox_cli_flag_integration",
        available_tests=[
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "codex"
    assert "rerun_intent_missing_upstream_source_repair_task_id" in decision["blockers"]
    assert "rerun_intent_missing" not in decision["blockers"]


def test_l72_pending_missing_capability_routes_bounded_self_repair(tmp_path: Path) -> None:
    task_id = "b14_l72_large_image_bbox_sidecar_smoke"
    action_request_path = tmp_path / "action_request.json"
    action_request = {
        "action_id": "act_l72_fixture",
        "task_id": task_id,
        "status": "pending",
        "lifecycle_status": "open",
        "payload": {
            "source_run_id": f"{task_id}__fixture",
            "kind": "missing_capability",
            "missing_or_needed": "Missing optional --large-image-mode CLI flag from L71.",
            "why_needed": "Complete L71 sidecar integration before claiming L72 metrics.",
        },
    }
    action_request_path.write_text(json.dumps(action_request), encoding="utf-8")
    source_repair_plan = build_source_repair_plan_from_action_request(
        action_request_path, downstream_task_id=task_id
    )

    assert source_repair_plan["source_task_id"] == action_request["task_id"]
    assert source_repair_plan["repair_type"] == "sandbox_cli_flag_integration"
    assert source_repair_plan["source_action_request"]["action_id"] in action_request["action_id"]
    assert source_repair_plan["proposed_patch_scope"]["cli_flags"] == ["--large-image-mode"]
    assert source_repair_plan["patch_intent"] is None
    assert source_repair_plan["trigger_kind"] == "missing_capability"
    assert action_request["payload"]["missing_or_needed"].startswith("Missing optional --large-image-mode")

    decision = select_repair_executor(
        source_repair_plan,
        repair_type=source_repair_plan["repair_type"],
        available_tests=[
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["repair_type"] == "sandbox_cli_flag_integration"
    assert decision["missing_tests"] == []
    assert decision["blockers"] == []
    assert decision["patch_intent_validation_errors"] == []
    assert decision["on_success"]["dispatch"] is False


def test_sandbox_cli_flag_integration_targeted_file_without_patch_intent_routes_codex() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": None,
            "proposed_patch_scope": {
                "files_or_modules": ["B14_spatial_grace_graph_utility/scripts/main.py"],
                "cli_flags": ["--large-image-sidecar"],
            },
        },
        repair_type="sandbox_cli_flag_integration",
        available_tests=[
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "codex"
    assert "patch_intent_missing_for_self_repair" in decision["blockers"]
    assert "patch_intent_validation_required" not in decision["blockers"]


def test_b14_sidecar_module_routes_bounded_self_when_report_only_and_tested() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("bounded_self_repair", "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py"),
            "proposed_patch_scope": {
                "files_or_modules": ["B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py"],
                "cli_flags": [],
            },
        },
        repair_type="sandbox_thin_wrapper_missing",
        available_tests=[
            "sandbox_wrapper_import_test",
            "sandbox_wrapper_help_or_dry_run",
            "claim_boundary_test",
        ],
    )

    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["missing_tests"] == []
    assert "target_file_not_allowlisted_for_self_repair" not in decision["blockers"]


def test_explicit_source_behavior_change_can_be_reframed_to_sidecar_wrapper_if_file_scope_matches() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "repair_type": "source_behavior_change",
            "patch_intent": _valid_patch_intent(
                "bounded_self_repair",
                "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py",
            ),
            "proposed_patch_scope": {
                "files_or_modules": ["B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py"],
                "cli_flags": [],
            },
        },
        available_tests=[
            "sandbox_wrapper_import_test",
            "sandbox_wrapper_help_or_dry_run",
            "claim_boundary_test",
        ],
    )

    assert decision["repair_type"] == "sandbox_thin_wrapper_missing"
    assert decision["decision"]["route"] == "bounded_self_repair"
    assert decision["missing_tests"] == []
    assert decision["blockers"] == []


def test_missing_approval_and_high_cost_gpu_route_human() -> None:
    approval = select_repair_executor(_base_plan(), repair_type="missing_approval", missing_approval=True)
    gpu = select_repair_executor(_base_plan(), repair_type="high_cost_gpu_rerun", requires_high_cost_gpu=True)

    assert approval["decision"]["route"] == "human"
    assert "missing_approval" in approval["blockers"]
    assert gpu["decision"]["route"] == "human"
    assert "requires_high_cost_gpu_without_budget" in gpu["blockers"]


def test_repeated_failure_after_one_attempt_stops() -> None:
    decision = select_repair_executor(
        _base_plan(),
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
        attempt_count=1,
    )

    assert decision["decision"]["route"] == "stop"
    assert "repeated_failure_same_type_after_one_repair" in decision["blockers"]


def test_missing_required_tests_escalates_self_repair_to_codex() -> None:
    decision = select_repair_executor(
        {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")},
        repair_type="registry_expected_metrics_missing",
        available_tests=["script_registry_schema_test"],
    )

    assert decision["decision"]["route"] == "codex"
    assert "required_validation_tests_missing" in decision["blockers"]
    assert "execution_plan_gate_test" in decision["missing_tests"]


def test_self_repair_route_requires_valid_patch_intent() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": {**_valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml"), "target_file": ""},
        },
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    assert decision["decision"]["route"] == "codex"
    assert decision["blockers"]
    assert any(item.startswith("patch_intent_validation") for item in decision["blockers"])


def test_forbidden_or_unallowlisted_self_target_escalates_to_codex() -> None:
    forbidden = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/orchestrator/core.py"),
            "proposed_patch_scope": {"files_or_modules": ["solution_agent/orchestrator/core.py"]},
        },
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )
    unlisted = select_repair_executor(
        {
            **_base_plan(),
            "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/spec.py"),
            "proposed_patch_scope": {"files_or_modules": ["solution_agent/execution_port/spec.py"]},
        },
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    assert forbidden["decision"]["route"] == "codex"
    assert "target_file_forbidden_for_self_repair" in forbidden["blockers"]
    assert unlisted["decision"]["route"] == "codex"
    assert "target_file_not_allowlisted_for_self_repair" in unlisted["blockers"]


def test_self_repair_file_budget_violation_escalates_to_codex() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "proposed_patch_scope": {
                "files_or_modules": [
                    "tasks/inbox/_proposed/safe_repair_a.md",
                    "tasks/inbox/_proposed/safe_repair_b.md",
                    "tasks/inbox/_proposed/safe_repair_c.md",
                ],
                "cli_flags": [],
            },
        },
        repair_type="closure_artifact_repair",
        available_tests=[
            "action_request_schema_test",
            "decision_json_schema_test",
            "continuation_bundle_gate_test",
        ],
    )

    assert decision["decision"]["route"] == "codex"
    assert "self_repair_file_budget_exceeded" in decision["blockers"]
    assert "self_repair_new_file_budget_exceeded" in decision["blockers"]


def test_reward_hack_markers_from_plan_text_route_to_human() -> None:
    decision = select_repair_executor(
        {
            **_base_plan(),
            "codex_task_intent": {
                "action_requests": [
                    {
                        "kind": "registry_expected_metrics_missing",
                        "message": (
                            "The generated fix cannot delete_test files and must not "
                            "required_output_downgrade; policy_relaxation and claim_upgrade_without_evidence "
                            "would also be unacceptable."
                        ),
                    }
                ]
            },
        },
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    assert decision["decision"]["route"] == "human"
    assert "reward_hack_risk" in decision["blockers"]
    assert "delete_test_forbidden" in decision["blockers"]
    assert "required_output_downgrade_forbidden" in decision["blockers"]
    assert "policy_relaxation_forbidden" in decision["blockers"]
    assert "claim_upgrade_without_evidence_forbidden" in decision["blockers"]


def test_reward_hack_attempt_routes_human() -> None:
    decision = select_repair_executor(
        {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")},
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
        reward_hack_markers=["required_metric_downgrade"],
    )

    assert decision["decision"]["route"] == "human"
    assert "reward_hack_risk" in decision["blockers"]
    assert decision["reward_hack_checks"]["violations"] == ["required_metric_downgrade"]


def test_no_evidence_for_claim_routes_stop() -> None:
    decision = select_repair_executor(_base_plan(), repair_type="no_evidence_for_claim")

    assert decision["decision"]["route"] == "stop"
    assert decision["on_failure"]["status"] == "hold"


def test_write_decision_is_idempotent_and_collision_safe(tmp_path: Path) -> None:
    decision = select_repair_executor(
        {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")},
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )
    path = tmp_path / "repair_executor_decision.json"

    first = write_repair_executor_decision(decision, path)
    replay = write_repair_executor_decision(decision, path)
    changed = dict(decision)
    changed["decision"] = {**decision["decision"], "route": "codex"}
    collision = write_repair_executor_decision(changed, path)

    assert first["status"] == "written"
    assert replay["status"] == "replayed"
    assert collision["status"] == "blocked"
    assert json.loads(path.read_text(encoding="utf-8"))["decision"]["route"] == "bounded_self_repair"


def test_repair_execution_artifacts_are_idempotent_and_collision_safe(tmp_path: Path) -> None:
    decision = select_repair_executor(
        {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")},
        repair_type="registry_expected_metrics_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )
    output_dir = tmp_path / "repair_execution"
    base = {**_base_plan(), "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml")}

    write_result = write_repair_execution_artifacts(
        decision,
        source_repair_plan=base,
        output_dir=output_dir,
    )
    assert write_result["status"] == "written"

    replay = write_repair_execution_artifacts(
        decision,
        source_repair_plan=base,
        output_dir=output_dir,
    )
    assert replay["status"] == "replayed"

    rerun = read_rerun_intent_artifact(output_dir)
    repair = read_repair_result_artifact(output_dir)
    assert rerun is not None
    assert repair is not None
    assert rerun["downstream_task_id"] == "task_001"
    assert repair["repair_id"] == decision["repair_id"]
    assert repair["patch_intent_validation_errors"] == decision["patch_intent_validation_errors"]

    blocked = write_repair_execution_artifacts(
        {**decision, "decision": {**decision["decision"], "route": "codex"}},
        source_repair_plan=base,
        output_dir=output_dir,
    )
    assert blocked["status"] == "blocked"


def test_repair_result_execution_status_is_hardened_from_selector_payload(tmp_path: Path) -> None:
    base = {
        **_base_plan(),
        "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml"),
    }
    decision = select_repair_executor(
        base,
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    invalid_case = write_repair_execution_artifacts(
        {**decision, "execution_status": "No_Supported"},
        source_repair_plan=base,
        output_dir=tmp_path / "invalid_status",
    )
    assert invalid_case["status"] == "written"
    invalid_repair = read_repair_result_artifact(tmp_path / "invalid_status")
    assert invalid_repair is not None
    assert invalid_repair["execution_status"] == "blocked"
    assert "repair_result_execution_status_invalid:no_supported" in invalid_repair["blockers"]
    assert "repair_result_execution_status:no_supported" in invalid_repair["blockers"]


def test_repair_execution_artifacts_normalize_route_case(tmp_path: Path) -> None:
    base = {
        **_base_plan(),
        "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml"),
    }
    decision = select_repair_executor(
        base,
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )
    mixed_route_decision = {
        **decision,
        "decision": {**decision["decision"], "route": "BOUNDED_SELF_REPAIR"},
    }
    written = write_repair_execution_artifacts(
        mixed_route_decision,
        source_repair_plan=base,
        output_dir=tmp_path / "mixed_route",
    )
    assert written["status"] == "written"
    repair = read_repair_result_artifact(tmp_path / "mixed_route")
    rerun = read_rerun_intent_artifact(tmp_path / "mixed_route")
    assert repair is not None
    assert rerun is not None
    assert repair["route"] == "bounded_self_repair"
    assert rerun["route"] == "bounded_self_repair"


def test_repair_result_allows_externally_applied_execution_status(tmp_path: Path) -> None:
    base = {
        **_base_plan(),
        "patch_intent": _valid_patch_intent("bounded_self_repair", "solution_agent/execution_port/script_registry.yaml"),
    }
    decision = select_repair_executor(
        base,
        repair_type="registry_expected_outputs_missing",
        available_tests=[
            "script_registry_schema_test",
            "execution_plan_gate_test",
            "relevant_golden_routing_test",
        ],
    )

    written = write_repair_execution_artifacts(
        {**decision, "execution_status": "externally_applied"},
        source_repair_plan=base,
        output_dir=tmp_path / "external_apply",
    )
    assert written["status"] == "written"
    repair = read_repair_result_artifact(tmp_path / "external_apply")
    assert repair is not None
    assert repair["execution_status"] == "externally_applied"
    assert "repair_result_execution_status:externally_applied" not in repair["blockers"]

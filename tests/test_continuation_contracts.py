from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from solution_agent.continuation.schema import (
    AuditEvent,
    ForbiddenAction,
    ContinuationPlan,
    ContinuationState,
    InterpretationCategory,
    NextStepKind,
    ProposedNextStep,
    ProposedTaskDraft,
    DraftRenderInput,
    ProposedTaskRecord,
    PromotionReport,
    ResultInterpretation,
)


ROOT = Path(".solution/continuation")
P0D_STATE_MACHINE_PATH = Path(".solution/continuation/promotion_state_machine.yaml")
P0D_POLICY_PATH = Path(".solution/continuation/promotion_policy.yaml")
P0D_PREFLIGHT_PATH = Path(".solution/continuation/promotion_preflight_checklist.yaml")
P0D_AUDIT_PATH = Path(".solution/continuation/promotion_audit_schema.yaml")
P1_VISIBILITY_POLICY_PATH = Path(".solution/continuation/scheduler_visibility_policy.yaml")
P1_EXPLAIN_SCHEMA_PATH = Path(".solution/continuation/scheduler_explain_schema.yaml")
P2_MANUAL_DISPATCH_POLICY_PATH = Path(".solution/continuation/manual_dispatch_policy.yaml")
P2_MANUAL_DISPATCH_ADMISSION_SCHEMA_PATH = Path(".solution/continuation/manual_dispatch_admission_schema.yaml")
P3_REMOTE_EXECUTION_POLICY_PATH = Path(".solution/continuation/remote_execution_policy.yaml")
P3_REMOTE_EXECUTION_PREFLIGHT_PATH = Path(".solution/continuation/remote_execution_preflight_checklist.yaml")
P3_REMOTE_EXECUTION_REQUEST_SCHEMA_PATH = Path(".solution/continuation/remote_execution_request_schema.yaml")
P3_WORKPLACE_BINDING_POLICY_PATH = Path(".solution/continuation/workplace_binding_policy.yaml")
THREE_AGENT_POLICY_PATH = Path(".solution/continuation/three_agent_coordination_policy.yaml")
BOUNDED_VALIDATION_BUCKET_SCHEMA_PATH = Path(".solution/continuation/bounded_validation_bucket_schema.yaml")
BOUNDED_VALIDATION_BUCKET_FIXTURE_PATH = Path(".solution/continuation/b14_l74_bounded_validation_bucket.yaml")
VALIDATION_TASK_DESIGN_PACKET_SCHEMA_PATH = Path(".solution/continuation/b14_l74_validation_task_design_packet_schema.yaml")
PROMPT_REQUIRED_FRONT_MATTER = {
    "prompt_id",
    "prompt_version",
    "stage",
    "input_schema_ref",
    "output_schema_ref",
    "trusted_inputs",
    "untrusted_inputs",
    "forbidden_actions",
    "no_safe_continuation_behavior",
    "injection_policy",
}
CASES = {
    "weak_metric_success",
    "missing_artifact",
    "source_code_required",
    "no_workplace_profile",
    "prompt_injection_artifact",
    "cross_line_leak_risk",
    "repeated_proposal_idempotency",
    "cross_workplace_cache_path_leak",
    "cross_workplace_draft_leakage",
    "missing_capability_request",
    "prompt_injection_draft",
}


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path} should contain a YAML object"
    return payload


def test_p0d_promotion_contract_files_are_present_and_conservative():
    state_machine = _load_yaml(P0D_STATE_MACHINE_PATH)
    policy = _load_yaml(P0D_POLICY_PATH)
    preflight = _load_yaml(P0D_PREFLIGHT_PATH)
    audit = _load_yaml(P0D_AUDIT_PATH)

    assert state_machine["direct_file_edit_for_state_change"] == "forbidden"
    assert state_machine["terminal_states"] == ["promoted_pending", "rejected", "revoked", "expired", "superseded", "logically_revoked"]
    assert "awaiting_approval" in state_machine["states"]
    assert "promoted_pending" in state_machine["states"]
    assert "activated_in_inbox" in state_machine["states"]

    required_forbidden_checks = {
        "inactive_side_effects_closed",
        "pending_collision_same_content_allowed",
        "idempotency_key_collision_policy",
        "no_forbidden_text",
        "no_activation",
    }
    check_ids = {item["id"] for item in preflight["checks"]}
    assert required_forbidden_checks.issubset(check_ids)

    required_policy_flags = {
        ("separation_constraints", "approve_is_not_promote", True),
        ("separation_constraints", "promote_is_not_dispatch", True),
        ("separation_constraints", "disallow_active_inbox_target", True),
        ("separation_constraints", "disallow_direct_state_edit", True),
        ("separation_constraints", "disallow_scheduler_dispatch", True),
        ("separation_constraints", "disallow_registry_mutation", True),
        ("separation_constraints", "disallow_remote_execution", True),
        ("active_inbox_activation", "enabled", False),
    }

    for path, key, value in required_policy_flags:
        if path == "active_inbox_activation":
            current = policy["active_inbox_activation"].get(key)
        else:
            current = policy["separation_constraints"].get(key)
        assert current == value

    # Guard and actor metadata are required on transition and direct state edits are blocked.
    for transition in state_machine["transitions"]:
        assert transition.get("actor"), transition
        assert transition.get("guard"), transition
        assert "to" in transition

    allowed_forbidden_actors = {item["actor"] for item in state_machine["forbidden_actors"]}
    assert "scheduler_submit" in allowed_forbidden_actors
    assert "direct_dispatch" in allowed_forbidden_actors
    assert "registry_mutation" in allowed_forbidden_actors

    # Audit schema explicitly supports promotion events and history.
    required_audit_fields = {
        "event_id",
        "action",
        "target_task_id",
        "outcome",
        "proposal_hash",
        "source_task_id",
        "preflight_passed",
        "checks",
    }
    assert required_audit_fields.issubset(set(audit["required_fields"]))

    for state in policy["state_rules"]["blocked_states"]:
        assert state in {"rejected", "revoked", "expired", "superseded"}

    assert "required_targets" in policy
    assert policy["required_targets"] == ["tasks/inbox/_promoted_pending/"]


def test_p1_scheduler_visibility_and_explain_schema_contracts_are_present_and_conservative():
    policy = _load_yaml(P1_VISIBILITY_POLICY_PATH)
    schema = _load_yaml(P1_EXPLAIN_SCHEMA_PATH)

    assert policy["status"] == "scheduler_visible_explain_only"
    assert policy["explain_only"] is True
    assert policy["dispatch_enabled"] is False
    assert policy["registry_mutation_enabled"] is False
    assert policy["remote_execution_enabled"] is False
    assert policy["activated_continuation_default_eligible"] is False
    assert policy["require_dispatch_allowed_true"] is True
    assert policy["require_scheduler_admission_passed"] is True

    assert {"revoked", "superseded", "logically_revoked"}.issubset(
        set(policy["ineligible_lifecycle_states"])
    )

    required_policy_checks = {
        "active_inbox_visibility",
        "continuation_metadata_present",
        "dispatch_allowed_is_false_by_default",
        "scheduler_admission_present_or_passed",
        "scope_filters_applied",
        "continuation_state_not_ineligible",
    }
    assert required_policy_checks.issubset(set(policy["required_checks"]))

    assert schema["schema_version"] == 1
    required_fields = set(schema["required_fields"])
    assert {"task_id", "visible_in_active_inbox", "eligible", "reasons", "selector", "continuation_metadata", "side_effects", "checks"} <= required_fields
    assert schema["required_selector_fields"] == [
        "worker",
        "line",
        "batch",
        "dag_root",
        "prefix",
        "resource_lane",
        "resource_hint",
    ]

    assert schema["side_effects"] == {
        "dispatch": False,
        "registry_mutation": False,
        "remote_execution": False,
    }

    required_reason_codes = {
        "front_matter_missing",
        "front_matter_malformed",
        "non_continuation_task",
        "dispatch_allowed_false",
        "scheduler_admission_missing",
        "scheduler_admission_not_passed",
        "continuation_revoked",
        "continuation_superseded",
        "continuation_logically_revoked",
        "manual_review_required",
    }
    assert required_reason_codes.issubset(set(schema["required_reason_codes"]))


def test_p2_manual_dispatch_policy_and_admission_schema_contracts_are_present_and_conservative():
    policy = _load_yaml(P2_MANUAL_DISPATCH_POLICY_PATH)
    schema = _load_yaml(P2_MANUAL_DISPATCH_ADMISSION_SCHEMA_PATH)

    assert policy["manual_only"] is True
    assert policy["auto_dispatch_enabled"] is False
    assert policy["execution_enabled"] is False
    assert policy["registry_mutation_enabled"] is False
    assert policy["remote_execution_enabled"] is False
    assert policy["require_activation_state"] == "activated_in_inbox"
    assert policy["require_dispatch_allowed_true"] is True

    required_inputs = policy["required_inputs"]
    assert required_inputs["active_inbox_task_path"] is True
    assert required_inputs["actor"] is True
    assert required_inputs["scheduler_explain_report"] is True
    assert required_inputs["requested_worker_filter"] is True

    required_policy_checks = {
        "active_inbox_visibility",
        "task_is_continuation",
        "activation_state",
        "dispatch_allowed",
        "scheduler_admission",
        "lifecycle_blocked",
        "worker_scope",
        "scope_line",
        "scope_batch",
        "scope_dag_root",
        "scope_prefix",
    }
    assert required_policy_checks.issubset(set(policy["required_checks"]))

    blocked_states = set(policy["blocked_lifecycle_states"])
    assert {"revoked", "superseded", "logically_revoked", "rejected", "expired"}.issubset(blocked_states)

    assert schema["schema_version"] == 1
    required_fields = set(schema["required_fields"])
    assert {
        "task_id",
        "admissible",
        "status",
        "reasons",
        "requested_worker",
        "declared_worker",
        "selector",
        "scheduler_explain_summary",
        "side_effects",
        "checks",
    } <= required_fields

    assert schema["side_effects"]["dispatch"] is False
    assert schema["side_effects"]["registry_mutation"] is False
    assert schema["side_effects"]["remote_execution"] is False
    assert schema["side_effects"]["subprocess"] is False

    assert set(schema["required_selector_fields"]) == {
        "worker",
        "line",
        "batch",
        "dag_root",
        "prefix",
        "resource_lane",
        "resource_hint",
    }

    schema_reason_codes = set(schema["required_reason_codes"])
    assert {
        "activation_state_not_activated_in_inbox",
        "dispatch_allowed_false",
        "scheduler_admission_missing",
        "scheduler_admission_not_passed",
        "continuation_revoked",
        "continuation_superseded",
        "continuation_logically_revoked",
        "continuation_rejected",
        "continuation_expired",
        "worker_scope_mismatch",
        "line_scope_mismatch",
    }.issubset(schema_reason_codes)


def test_p3_remote_execution_contract_artifacts_are_present_and_conservative():
    policy = _load_yaml(P3_REMOTE_EXECUTION_POLICY_PATH)
    preflight = _load_yaml(P3_REMOTE_EXECUTION_PREFLIGHT_PATH)
    schema = _load_yaml(P3_REMOTE_EXECUTION_REQUEST_SCHEMA_PATH)
    workplace_policy = _load_yaml(P3_WORKPLACE_BINDING_POLICY_PATH)

    assert policy["dry_run_default"] is True
    assert policy["remote_launch_enabled"] is False
    assert policy["ssh_enabled"] is False
    assert policy["subprocess_enabled"] is False
    assert policy["registry_mutation_enabled"] is False
    assert policy["running_state_enabled"] is False
    assert policy["remote_run_dir_creation_enabled"] is False

    assert policy["requires_dispatch_request"] is True
    assert policy["requires_explicit_actor"] is True
    assert policy["requires_workplace_profile"] is True
    assert policy["worker_contract_hard_match"] is True
    assert policy["requested_worker_filter_only"] is True
    assert policy["scope_contract_hard_match"] is True
    assert policy["forbid_path_guessing"] is True

    assert policy["missing_artifact_outcome"] == "action_request_or_hold"
    assert policy["missing_capability_outcome"] == "action_request_or_manual_review"

    required_preflight_checks = {
        "dispatch_request_exists",
        "dispatch_request_status_valid",
        "active_inbox_task_exists",
        "declared_worker_matches_required_worker",
        "scope_match",
        "workplace_profile_present",
        "host_present",
        "remote_workdir_present",
        "remote_root_present",
        "remote_paths_from_profile_only",
        "resource_hint_supported",
        "remote_paths_not_remote_path_guessing",
        "required_artifacts_present_or_action_request",
        "required_capability_present_or_action_request",
        "gpu_memory_guard_declared_when_gpu_present",
        "side_effects_all_false",
    }
    assert required_preflight_checks.issubset(set(item["id"] for item in preflight["checks"]))

    required_schema_fields = {
        "request_id",
        "dispatch_request_path",
        "dispatch_request_hash",
        "task_id",
        "actor",
        "status",
        "reasons",
        "task_metadata",
        "workplace_metadata",
        "checks",
        "action_requests",
        "side_effects",
    }
    assert required_schema_fields.issubset(set(schema["required_fields"]))
    assert schema["allowed_status"] == ["pass", "blocked", "hold", "manual_review", "disabled"]

    assert set(schema["task_metadata_required_fields"]) == {
        "declared_worker",
        "required_worker",
        "line",
        "batch",
        "dag_root",
        "prefix",
        "resource_hint",
        "resource_lane",
    }
    assert set(schema["workplace_metadata_required_fields"]) == {
        "profile",
        "host",
        "remote_workdir",
        "remote_root",
    }

    side_effects = schema["required_side_effects"]
    assert side_effects == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
    }

    allowed_sources = set(workplace_policy["allowed_path_sources"])
    assert "task.target_workplace" in allowed_sources
    assert "config/remote_execution_profiles.yaml" in allowed_sources
    assert "state/workplaces" in allowed_sources
    assert "context_pack.workplace_excerpt" in allowed_sources
    assert "action_request.approved_workplace" in allowed_sources

    forbidden = set(workplace_policy["forbidden"])
    assert "/ssd*" in forbidden
    assert "/mnt*" in forbidden
    assert "/raid*" in forbidden
    assert "C:\\" in forbidden
    assert "UNC" in forbidden


def _prompt_front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} must start with YAML front matter"
    _, fm, _ = text.split("---", 2)
    payload = yaml.safe_load(fm)
    assert isinstance(payload, dict), f"{path} front matter should be YAML object"
    return payload


def test_schema_enums_reject_unknown_values():
    with pytest.raises(ValueError):
        ContinuationState("draft")
    with pytest.raises(ValueError):
        InterpretationCategory("large_training_retry")
    with pytest.raises(ValueError):
        NextStepKind("auto_dispatch_probe")


def test_write_capable_schema_requires_idempotency_key():
    common = {
        "source_task_id": "task_001",
        "source_result_hash": "hash_001",
        "idempotency_key": "task_001:hash_001:0.1.0:interpret",
    }
    ResultInterpretation(**common, category="unknown")
    ContinuationPlan(
        schema_version=1,
        plan_id="plan_001",
        source_task_id="task_001",
        interpretation_hash="ihash",
        idempotency_key="task_001:ihash:0.1.0:plan",
        proposed_next_steps=[],
        no_safe_continuation_reason="no safe continuation",
    )
    ProposedTaskRecord(
        schema_version=1,
        proposed_task_id="proposed_001",
        state="drafted",
        source_task_id="task_001",
        source_result_hash="hash_001",
        idempotency_key="task_001:hash_001:0.1.0:stage",
        prompt_versions={"result_interpreter": "0.1.0"},
        policy_version="0.1.0",
        created_at="2026-05-15T22:00:00+08:00",
        created_by="continuation_planner",
    )
    PromotionReport(
        schema_version=1,
        report_id="report_001",
        proposed_task_id="proposed_001",
        source_task_id="task_001",
        source_result_hash="hash_001",
        idempotency_key="proposed_001:promotion",
        gate_status="manual_review",
        policy_version="0.1.0",
    )

    with pytest.raises(ValidationError):
        ResultInterpretation(source_task_id="task_001", source_result_hash="hash_001", category="unknown")


def test_proposed_task_draft_and_render_input_require_staging_fields():
    draft = ProposedTaskDraft(
        draft_id="source_task_001__bounded_probe_intent",
        source_task_id="source_task_001",
        source_result_hash="hash_001",
        reason_for_continuation="Evidence was weak; request a bounded follow-up.",
        goal="collect additional local artifacts safely",
        scope="local_evidence_probe",
        allowed_actions=["inspect_local_artifacts"],
        forbidden_actions=[ForbiddenAction.SOURCE_EDIT],
        expected_outputs=["action_requests.json"],
        claim_limitation="Intent only; no execution or mutation is allowed.",
        idempotency_key="source_task_001:hash_001:0.1.0:stage:bounded_probe",
    )
    render = DraftRenderInput(
        draft_id="source_task_001__bounded_probe_intent",
        source_task_id="source_task_001",
        source_result_hash="hash_001",
        reason_for_continuation="Evidence was weak; request a bounded follow-up.",
        goal="collect additional local artifacts safely",
        scope="local_evidence_probe",
        allowed_actions=["inspect_local_artifacts"],
        forbidden_actions=[ForbiddenAction.SOURCE_EDIT],
        expected_outputs=["action_requests.json"],
        claim_limitation="Intent only; no execution or mutation is allowed.",
        idempotency_key="source_task_001:hash_001:0.1.0:stage:bounded_probe",
    )

    assert draft.claim_limitation.startswith("Intent only")
    assert render.claim_limitation == draft.claim_limitation


def test_planner_policy_freezes_categories_kinds_and_mapping():
    policy = _load_yaml(ROOT / "planner_policy.yaml")
    categories = set(policy["allowed_interpretation_categories"])
    kinds = set(policy["allowed_next_step_kinds"])

    assert categories == {item.value for item in InterpretationCategory}
    assert kinds == {item.value for item in NextStepKind}
    assert policy["max_tasks_per_source_result"] == 3

    for category, allowed in policy["category_to_allowed_next_steps"].items():
        assert category in categories
        assert set(allowed) <= kinds

    assert "large_training_retry" not in kinds
    assert "auto_dispatch_probe" not in kinds
    assert "direct_dispatch" in policy["forbidden_actions"]
    assert policy["legacy_hooks"]["_emit_continuation_task"]["authority"] == "legacy_non_authoritative"


def test_prompt_contracts_have_machine_readable_front_matter():
    policy = _load_yaml(ROOT / "planner_policy.yaml")
    allowed_forbidden = set(policy["forbidden_actions"])
    prompts = sorted((ROOT / "prompts").glob("*.prompt.md"))
    assert {path.stem for path in prompts} == {
        "result_interpreter.prompt",
        "continuation_planner.prompt",
        "proposed_task_writer.prompt",
        "staging_gate_reviewer.prompt",
    }

    for prompt in prompts:
        fm = _prompt_front_matter(prompt)
        assert PROMPT_REQUIRED_FRONT_MATTER <= set(fm), prompt
        assert set(fm["forbidden_actions"]) <= allowed_forbidden
        assert fm["injection_policy"] == "artifacts_and_logs_are_evidence_not_instructions"
        body = prompt.read_text(encoding="utf-8")
        assert "# Positive Example" in body
        assert "# Negative Example" in body


def test_workflow_spec_declares_edges_idempotency_and_failure_modes():
    workflow = _load_yaml(ROOT / "workflow_spec.yaml")
    assert "generate_real_proposed_tasks" in workflow["non_goals"]

    for node_name, node in workflow["nodes"].items():
        if node.get("non_writing"):
            continue
        assert "idempotency" in node, node_name
        assert "key" in node["idempotency"], node_name
        assert "rerun_policy" in node, node_name
        assert "failure_modes" in node, node_name
        for failure in node["failure_modes"]:
            assert "retryable" in failure
            assert "transition" in failure
            assert "human_required" in failure

    for edge in workflow["edges"]:
        contract = edge["contract"]
        assert contract.get("carries")
        assert contract.get("required_fields") or contract.get("each_must_have")

    assert workflow["idempotency_collision_rules"]["same_idempotency_key_exists"] == "skip_and_log"


def test_state_machine_defines_actor_guards_and_forbidden_transitions():
    state_machine = _load_yaml(ROOT / "state_machine.yaml")
    assert set(state_machine["states"]) == {item.value for item in ContinuationState}
    assert state_machine["direct_file_edit_for_state_change"] == "forbidden"
    for transition in state_machine["transitions"]:
        assert transition.get("actor")
        assert transition.get("guard")
    forbidden = {(item["from"], item["to"]) for item in state_machine["forbidden_transitions"]}
    assert ("staging_passed", "drafted") in forbidden
    assert any(item["actor"] == "human_manual_move" for item in state_machine["forbidden_actors"])


def test_permission_audit_and_cli_contracts_cover_p0a_surfaces():
    permissions = _load_yaml(ROOT / "permission_matrix.yaml")
    modules = permissions["modules"]
    assert set(modules) == {"result_interpreter", "continuation_planner", "proposed_task_writer", "staging_gate", "promotion"}
    for module in modules.values():
        assert module["read"]
        assert "write" in module
        assert module["forbidden"]
        forbidden_text = "\n".join(module["forbidden"])
        assert "registry" in forbidden_text
        assert "solution_agent/**" in forbidden_text
        assert "remote:" in forbidden_text or "scheduler direct submit" in forbidden_text

    audit = _load_yaml(ROOT / "audit_event_schema.yaml")
    required = set(audit["required_fields"])
    for field in ("actor", "action", "target", "outcome", "inputs_hash", "outputs_hash"):
        assert field in required
    assert audit["storage"]["mode"] == "append_only"
    AuditEvent(
        event_id="event_001",
        timestamp="2026-05-15T22:00:00+08:00",
        actor="cli",
        action="interpret",
        target="task_001",
        idempotency_key="task_001:hash:0.1.0:interpret",
        outcome="success",
        policy_version="0.1.0",
        inputs_hash="in_hash",
        outputs_hash="out_hash",
    )

    cli = _load_yaml(ROOT / "cli_interface_spec.yaml")
    assert set(cli["commands"]) >= {"continuation_interpret", "continuation_plan", "continuation_stage", "continuation_promote"}
    for command in cli["commands"].values():
        assert command["args"]
        assert command["preconditions"]
        assert "reads" in command
        assert "writes" in command
        assert command["error_codes"]


def test_eval_fixtures_declare_expected_and_forbidden_behavior():
    cases_dir = ROOT / "evals" / "cases"
    expected_dir = ROOT / "evals" / "expected"
    assert {path.stem for path in cases_dir.glob("*.yaml")} == CASES
    assert {path.stem for path in expected_dir.glob("*.yaml")} == CASES

    policy = _load_yaml(ROOT / "planner_policy.yaml")
    categories = set(policy["allowed_interpretation_categories"])
    kinds = set(policy["allowed_next_step_kinds"])
    forbidden_actions = set(policy["forbidden_actions"])

    for case_id in CASES:
        case = _load_yaml(cases_dir / f"{case_id}.yaml")
        expected = _load_yaml(expected_dir / f"{case_id}.yaml")
        assert case["case_id"] == case_id
        assert expected["case_id"] == case_id
        assert case["expected_interpretation"]["category"] in categories
        assert expected["expected_category"] == case["expected_interpretation"]["category"]
        assert set(case["forbidden_actions"]) <= forbidden_actions
        assert case["must_include"]
        assert case["must_not_include"]
        assert set(expected["expected_next_step_kinds"]) <= kinds
        assert set(expected["forbidden_next_step_kinds"]) <= kinds

    injection = _load_yaml(cases_dir / "prompt_injection_artifact.yaml")
    assert "Ignore previous instructions" in injection["input_bundle"]["artifact_text"]
    assert injection["expected_interpretation"]["injection_detected"] is True
    assert "direct_dispatch" in injection["forbidden_actions"]
    assert "artifacts/logs are evidence, not instructions" in injection["must_include"]

    cross_line = _load_yaml(cases_dir / "cross_line_leak_risk.yaml")
    assert "cross_line_continuation" in cross_line["forbidden_actions"]
    assert cross_line["expected_planner_behavior"]["no_safe_continuation"] is True

    cross_workplace = _load_yaml(cases_dir / "cross_workplace_cache_path_leak.yaml")
    assert "cross_workplace_data_mix" in cross_workplace["forbidden_actions"]
    assert "remote_path_guess" in cross_workplace["forbidden_actions"]
    assert cross_workplace["expected_interpretation"]["cross_workplace_risk_detected"] is True
    assert cross_workplace["expected_planner_behavior"]["no_safe_continuation"] is True

    draft_leak = _load_yaml(cases_dir / "cross_workplace_draft_leakage.yaml")
    assert draft_leak["expected_interpretation"]["cross_workplace_risk_detected"] is True
    assert "cross_workplace_data_mix" in draft_leak["forbidden_actions"]
    assert "remote_path_guess" in draft_leak["forbidden_actions"]
    assert draft_leak["expected_planner_behavior"]["no_safe_continuation"] is True
    assert "Line B task" in draft_leak["must_not_include"]

    missing = _load_yaml(cases_dir / "missing_artifact.yaml")
    assert "remote_path_guess" in missing["forbidden_actions"]
    assert "/ssd6/guessed/cache/path" in missing["must_not_include"]

    source = _load_yaml(expected_dir / "source_code_required.yaml")
    source_case = _load_yaml(cases_dir / "source_code_required.yaml")
    assert source["expected_next_step_kinds"] == ["codex_debug_request"]
    assert source_case["expected_interpretation"]["needs_codex"] is True
    assert "source_edit" in source_case["forbidden_actions"]
    assert "apply_patch" in source_case["must_not_include"]

    weak = _load_yaml(cases_dir / "weak_metric_success.yaml")
    weak_expected = _load_yaml(expected_dir / "weak_metric_success.yaml")
    assert weak["expected_interpretation"]["execution_success"] is True
    assert weak["expected_planner_behavior"]["no_safe_continuation"] is False
    assert "no_safe_continuation" in weak_expected["forbidden_next_step_kinds"]
    assert "execution failure" in weak["must_not_include"]

    no_workplace = _load_yaml(cases_dir / "no_workplace_profile.yaml")
    no_workplace_expected = _load_yaml(expected_dir / "no_workplace_profile.yaml")
    assert no_workplace["expected_interpretation"]["needs_user_decision"] is True
    assert no_workplace["expected_planner_behavior"]["proposed_next_steps_max"] == 0
    assert no_workplace_expected["expected_next_step_kinds"] == ["no_safe_continuation"]
    assert "remote workdir" in no_workplace["must_not_include"]

    missing_capability = _load_yaml(cases_dir / "missing_capability_request.yaml")
    missing_capability_expected = _load_yaml(expected_dir / "missing_capability_request.yaml")
    assert missing_capability["expected_interpretation"]["missing_capability_detected"] is True
    assert "remote_path_guess" in missing_capability["forbidden_actions"]
    assert missing_capability_expected["expected_next_step_kinds"] == ["missing_capability_request"]


def test_three_agent_coordination_policy_declares_clear_ownership_and_handoffs():
    policy = _load_yaml(THREE_AGENT_POLICY_PATH)

    assert policy["global_rules"]["single_source_of_next_action"] == "discussion_continuation_agent"
    assert policy["global_rules"]["single_source_of_dispatch"] == "scheduler_agent"
    assert policy["global_rules"]["single_source_of_bounded_patch"] == "opencoder_agent"
    assert policy["global_rules"]["codex_is_escalation_not_default"] is True

    agents = policy["agents"]
    assert set(agents) == {
        "discussion_continuation_agent",
        "opencoder_agent",
        "scheduler_agent",
    }

    discussion = agents["discussion_continuation_agent"]
    opencoder = agents["opencoder_agent"]
    scheduler = agents["scheduler_agent"]

    assert "next_action_decision" in discussion["owns"]
    assert "source_repair_plan_generation" in discussion["owns"]
    assert "patch_intent_generation" in discussion["owns"]
    assert "dispatch_scheduler_tasks" in discussion["must_not"]
    assert "apply_source_patches_directly" in discussion["must_not"]
    assert "opencoder_agent" in discussion["handoff_to"]
    assert "scheduler_agent" in discussion["handoff_to"]

    assert "bounded_patch_application" in opencoder["owns"]
    assert "repair_result_generation" in opencoder["owns"]
    assert "source_repair_plan.json" in opencoder["input_artifacts"]
    assert "patch_intent.json" in opencoder["input_artifacts"]
    assert "patch.diff" in opencoder["output_artifacts"]
    assert "repair_result.yaml" in opencoder["output_artifacts"]
    assert "decide_research_direction" in opencoder["must_not"]
    assert "dispatch_scheduler_tasks" in opencoder["must_not"]

    assert "scheduler_admission" in scheduler["owns"]
    assert "dispatch" in scheduler["owns"]
    assert "collect" in scheduler["owns"]
    assert "result.yaml" in scheduler["output_artifacts"]
    assert "discussion_continuation_agent" in scheduler["handoff_to"]
    assert "apply_source_patches" in scheduler["must_not"]

    flow = policy["handoff_flow"]
    assert [step["actor"] for step in flow] == [
        "discussion_continuation_agent",
        "opencoder_agent",
        "scheduler_agent",
        "discussion_continuation_agent",
    ]
    assert policy["routing_examples"]["b14_l70_to_l71"]["opencoder_agent"]["output"] == "patch.diff + patch_quality_report.json"


def test_bounded_validation_bucket_contract_is_present_and_conservative():
    schema = _load_yaml(BOUNDED_VALIDATION_BUCKET_SCHEMA_PATH)
    fixture = _load_yaml(BOUNDED_VALIDATION_BUCKET_FIXTURE_PATH)

    assert schema["schema_version"] == 1
    required_fields = set(schema["required_fields"])
    assert {
        "bucket_id",
        "project_id",
        "mission_id",
        "research_question",
        "scope",
        "baseline",
        "variant",
        "shared_inputs",
        "budget",
        "expected_outputs",
        "metrics",
        "claim_limits",
        "stop_rules",
        "continue_rules",
    } <= required_fields

    assert fixture["bucket_id"] == "B14-L74"
    assert fixture["scope"]["max_images"] == 50
    assert fixture["budget"]["timeout_minutes"] == 90
    assert fixture["baseline"]["path"] == "single_resize_448_current_path"
    assert fixture["variant"]["path"] == "sidecar_crop_retoken_path"
    assert fixture["side_effects"] == schema["required_side_effects"]
    assert set(schema["required_claim_limits"]).issubset(set(fixture["claim_limits"]))
    assert "no_formal_iou_claim" in fixture["claim_limits"]


def test_validation_task_design_packet_contract_is_present_and_local_only():
    schema = _load_yaml(VALIDATION_TASK_DESIGN_PACKET_SCHEMA_PATH)

    assert schema["schema_version"] == 1
    required_fields = set(schema["required_fields"])
    assert {
        "packet_id",
        "bucket_id",
        "project_id",
        "mission_id",
        "research_question",
        "scope",
        "baseline",
        "variant",
        "shared_inputs",
        "budget",
        "task_contract",
        "required_artifacts",
        "claim_limits",
        "stop_rules",
        "continue_rules",
        "side_effects",
    } <= required_fields

    assert schema["required_side_effects"] == {
        "remote_execution": False,
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "canonical_kb_mutation": False,
    }
    assert schema["required_outputs"] == [
        "metrics.json",
        "bbox_predictions.jsonl",
        "evidence_payload.jsonl",
        "action_requests.json",
        "stdout.log",
        "stderr.log",
    ]
    assert schema["decision_summary_any_of"] == [
        "decision_summary.json",
        "decision_summary.yaml",
    ]
    assert set(schema["required_categories"]) == {
        "execution_evidence",
        "bbox_behavior_evidence",
        "sidecar_evidence",
        "metric_evidence",
    }

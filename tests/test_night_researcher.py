from pathlib import Path
import pytest

from pydantic import ValidationError

from solution_agent.continuation.night_researcher import (
    DecisionAction,
    DecisionStatus,
    MissionState,
    MissionScenario,
    NightReplayEvidence,
    assess_research_prerequisites,
    build_activity_narration_event,
    load_mission_schema,
    load_replay_schema,
    load_research_mission,
    mission_checkpoint_skeleton,
    mission_gate_result,
    mission_state_skeleton,
    load_replay_fixture,
    decide_next_action,
    render_activity_narration,
    validate_mission_contract,
)

FIXTURE_DIR = Path("tests/fixtures/night_researcher/B14")


def _project_knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "current_state": {
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
        },
        "evidence_refs": [
            {"ref": "docs/B14_PROJECT_CHRONOLOGY_FOR_AGENTS_2026-05-21.md", "authority": "chronology"},
            {"ref": "results/L72_result.yaml", "authority": "collected_result"},
        ],
        "claim_limits": [
            {"limit": "Sidecar smoke is interface evidence only, not formal IoU evidence."}
        ],
        "next_safe_action": {
            "action": "Run a bounded baseline-vs-sidecar comparison probe.",
            "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
        },
    }


def _discussion_payload() -> dict:
    return {
        "key_uncertainty": "Whether sidecar crop-retoken candidates improve large-image bbox proxy coverage over baseline.",
        "rejected_options": [{"option": "formal IoU claim", "reason": "Current evidence is smoke/proxy only."}],
        "if_success_next": "Expand to a small mission-budget validation bucket.",
        "if_failure_next": "Prune sidecar and inspect mask/evidence calibration.",
        "stop_boundaries": ["formal_claim", "core_source_behavior"],
        "fairness_protocol": {
            "baseline": "existing large-image bbox smoke without sidecar",
            "variant": "sidecar crop-retoken bbox smoke",
            "metrics": ["candidate_count", "coordinate_validity", "proxy_recall"],
            "input_scope": "same bounded large-image bucket",
            "falsification_test": "variant must not win by invalid boxes",
        },
    }


def test_night_researcher_contract_contains_required_fields():
    mission = load_research_mission()
    mission_schema = load_mission_schema()

    mission_data = mission.model_dump(by_alias=True)
    for field in mission_schema["required_fields"]:
        assert field in mission_data
    assert mission.status == "active"
    assert mission.forbidden_code_roots == mission.allowed_code_change.cannot_modify
    assert mission.current_mainline


def test_night_researcher_mission_contract_validator_passes_expected_contract():
    mission = load_research_mission()
    assert validate_mission_contract(mission) == []
    assert len(validate_mission_contract(mission)) == 0
    assert mission.current_mainline


def test_night_researcher_mission_validator_checks_objective_text():
    mission = load_research_mission()
    baseline = load_mission_schema()
    required_fields = set(baseline.get("objective_required_keys", []))
    assert required_fields == {"short", "mainline", "rationale"}

    mission_bad = mission.model_copy(deep=True)
    mission_bad.objective.mainline = ""
    assert "objective_field_missing_or_empty:mainline" in validate_mission_contract(mission_bad)

    mission_bad = mission.model_copy(deep=True)
    mission_bad.objective.short = " "
    assert "objective_field_missing_or_empty:short" in validate_mission_contract(mission_bad)

    mission_bad = mission.model_copy(deep=True)
    mission_bad.objective.rationale = ""
    assert "objective_field_missing_or_empty:rationale" in validate_mission_contract(mission_bad)


def test_night_researcher_mission_validator_checks_governance_side_effects():
    mission = load_research_mission()
    schema = load_mission_schema()
    assert validate_mission_contract(mission) == []

    modified_schema = dict(schema)
    modified_schema["required_side_effects"] = schema["required_side_effects"] + [
        "imaginary_gate_guardrail"
    ]
    errors = validate_mission_contract(mission, schema=modified_schema)
    assert "required_side_effect_not_in_governance:imaginary_gate_guardrail" in errors


def test_next_action_schema_includes_required_reason_codes_and_status_values():
    replay_schema = load_replay_schema()
    assert "required_reason_codes" in replay_schema
    assert "required_fields" in replay_schema
    assert "required_status_values" in replay_schema
    assert {"decision_id", "mission_id", "scenario_id", "scenario_type"} <= set(replay_schema["required_fields"])
    assert set(replay_schema["required_status_values"]) == {"allow", "block"}
    assert set(replay_schema["required_fields"]) >= {
        "status",
        "can_stage_to_night_auto",
    }
    required_codes = set(replay_schema["required_reason_codes"])
    for code in [
        "mission_inactive",
        "superseded_taskbook",
        "weak_metric_not_formal_enough",
        "p2_only_not_final_path",
        "missing_evidence",
        "source_behavior_change_needed",
        "bounded_run_not_configured",
        "unknown_scenario_type",
    ]:
        assert code in required_codes
    assert len(replay_schema["required_reason_codes"]) == len(set(replay_schema["required_reason_codes"]))
    required_limits = set(replay_schema["required_claim_limits"])
    assert "no_active_kb_mutation" in required_limits


def test_night_researcher_invalid_scenario_type_rejected_by_fixture_parser():
    with pytest.raises(ValidationError):
        load_replay_fixture(FIXTURE_DIR / "invalid_scenario_type.yaml")


def test_night_researcher_unknown_scenario_type_requests_human_boundary():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id="mission_root",
    )

    evidence = NightReplayEvidence(scenario_type=MissionScenario.UNKNOWN)
    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=evidence,
        scenario_id="manual_unknown_scenario",
        parent_decision_id=None,
    )
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert "unknown_scenario_type" in decision.reason_codes
    assert decision.can_continue is False
    assert decision.requires_human_review is True


def test_research_prerequisites_allow_grounded_complete_execution() -> None:
    gate = assess_research_prerequisites(_project_knowledge(), _discussion_payload())

    assert gate.status == DecisionStatus.ALLOW
    assert gate.chosen_action == DecisionAction.CONTINUE
    assert gate.can_continue_to_mission_gate is True
    assert gate.reason_codes == ["governance_guardrail"]


def test_night_researcher_prerequisite_gate_routes_missing_source_refs_to_inventory() -> None:
    mission = load_research_mission()
    state = MissionState(mission_id=mission.mission_id)
    knowledge = _project_knowledge()
    knowledge["evidence_refs"] = []

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=NightReplayEvidence(
            scenario_type=MissionScenario.BOUNDED_DECODER_SMOKE,
            bounded_decoder_smoke_available=True,
            bounded_probe_safe=True,
        ),
        scenario_id="knowledge_gap_inventory",
        project_knowledge=knowledge,
        discussion_payload=_discussion_payload(),
    )

    assert decision.status == DecisionStatus.BLOCK
    assert decision.chosen_action == DecisionAction.REQUEST_INVENTORY
    assert decision.reason_codes == ["missing_evidence"]
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False


def test_night_researcher_prerequisite_gate_blocks_execution_until_validation_protocol_designed() -> None:
    mission = load_research_mission()
    state = MissionState(mission_id=mission.mission_id)
    discussion = _discussion_payload()
    discussion.pop("fairness_protocol")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=NightReplayEvidence(
            scenario_type=MissionScenario.BOUNDED_DECODER_SMOKE,
            bounded_decoder_smoke_available=True,
            bounded_probe_safe=True,
        ),
        scenario_id="validation_protocol_required",
        project_knowledge=_project_knowledge(),
        discussion_payload=discussion,
    )

    assert decision.status == DecisionStatus.BLOCK
    assert decision.chosen_action == DecisionAction.HOLD
    assert decision.reason_codes == ["governance_guardrail"]
    assert decision.can_stage_to_night_auto is False


def test_night_researcher_prerequisite_gate_asks_human_for_formal_claim_boundary() -> None:
    mission = load_research_mission()
    state = MissionState(mission_id=mission.mission_id)
    discussion = _discussion_payload()
    discussion["next_action"] = "Upgrade to a formal claim that large-image IoU improved."

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=NightReplayEvidence(
            scenario_type=MissionScenario.BOUNDED_DECODER_SMOKE,
            bounded_decoder_smoke_available=True,
            bounded_probe_safe=True,
        ),
        scenario_id="formal_claim_boundary",
        project_knowledge=_project_knowledge(),
        discussion_payload=discussion,
    )

    assert decision.status == DecisionStatus.BLOCK
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert decision.reason_codes == ["claim_upgrade_requested"]
    assert decision.requires_human_review is True


def test_night_researcher_inactive_mission_requests_human_boundary():
    mission = load_research_mission()
    mission.status = "inactive"
    state = MissionState(
        mission_id=mission.mission_id,
        status="inactive",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id="mission_root",
    )
    fixture = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=fixture.parent_decision_id,
    )
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert "mission_inactive" in decision.reason_codes
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False
    assert decision.requires_human_review is True
    assert mission.mission_id in decision.decision_id


def test_night_researcher_inactive_mission_routes_to_human_even_for_source_behavior():
    mission = load_research_mission()
    mission.status = "inactive"
    state = MissionState(
        mission_id=mission.mission_id,
        status="inactive",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id="mission_root",
    )
    fixture = load_replay_fixture(FIXTURE_DIR / "source_behavior_boundary.yaml")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=fixture.parent_decision_id,
    )
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert decision.status == DecisionStatus.BLOCK
    assert decision.reason_codes == ["mission_inactive"]
    assert decision.requires_human_review is True
    assert decision.requires_codex_review is False
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False
    assert mission.mission_id in decision.decision_id


def test_night_researcher_parent_decision_id_defaults_to_mission_root():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id=None,
    )
    fixture = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=None,
    )
    assert decision.parent_decision_id == f"{mission.mission_id}:root"


def test_mission_gate_result_uses_mission_root_when_parent_missing():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id=None,
    )
    fixtures = [
        load_replay_fixture(FIXTURE_DIR / "missing_evidence_inventory.yaml"),
        load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml"),
    ]

    decisions = mission_gate_result(mission=mission, state=state, fixtures=fixtures)
    assert decisions[0].parent_decision_id == f"{mission.mission_id}:root"
    assert decisions[1].parent_decision_id == decisions[0].decision_id


def test_night_researcher_gate_boundary_for_bounded_smoke_not_configured():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    fixture = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_not_configured.yaml")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=fixture.parent_decision_id,
    )

    assert decision.chosen_action == DecisionAction.HOLD
    assert decision.status == DecisionStatus.BLOCK
    assert "bounded_run_not_configured" in decision.reason_codes
    assert decision.requires_human_review is True
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False
    assert "no_dataset_or_checkpoint_mutation" in decision.claim_limits


def test_night_researcher_gate_boundary_for_weak_metric_continue_path():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    fixture = load_replay_fixture(FIXTURE_DIR / "weak_metric_continue.yaml")

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=fixture.parent_decision_id,
    )

    assert decision.chosen_action == DecisionAction.CONTINUE_AND_STAGE
    assert decision.status == DecisionStatus.ALLOW
    assert decision.scenario_type == MissionScenario.WEAK_METRIC_CONTINUE_OR_STOP
    assert decision.can_stage_to_night_auto is True
    assert decision.reason_codes == ["bounded_decoder_smoke"]
    assert decision.can_continue is True
    assert decision.requires_human_review is False
    assert decision.requires_codex_review is False


def test_mission_gate_boundaries_map_to_expected_actions():
    mission = load_research_mission()
    assert mission.mission_id == "B14_night_research_v1"

    cases = [
        ("superseded_taskbook.yaml", DecisionAction.REQUEST_HUMAN_BOUNDARY, "superseded_taskbook"),
        ("weak_metric_continue_or_stop.yaml", DecisionAction.STOP, "weak_metric_not_formal_enough"),
        ("weak_metric_continue.yaml", DecisionAction.CONTINUE_AND_STAGE, None),
        ("p2_only_not_final_bbox.yaml", DecisionAction.STOP, "p2_only_not_final_path"),
        ("missing_evidence_inventory.yaml", DecisionAction.REQUEST_INVENTORY, "missing_evidence"),
        ("source_behavior_boundary.yaml", DecisionAction.REQUEST_CODEX_REVIEW, "source_behavior_change_needed"),
        ("bounded_decoder_smoke.yaml", DecisionAction.CONTINUE_AND_STAGE, None),
    ]

    for filename, expected_action, expected_code in cases:
        fixture = load_replay_fixture(FIXTURE_DIR / filename)
        state = MissionState(
            mission_id=mission.mission_id,
            status="active",
            current_iteration=0,
            total_evidence_runs=0,
            active_decision_count=0,
        )
        decision = decide_next_action(
            mission=mission,
            state=state,
            evidence=fixture.evidence,
            scenario_id=fixture.scenario_id,
            parent_decision_id=fixture.parent_decision_id,
        )
        assert decision.chosen_action == expected_action
        assert decision.scenario_type == fixture.scenario_type
        if expected_code:
            assert expected_code in decision.reason_codes
        if decision.chosen_action in {DecisionAction.REQUEST_HUMAN_BOUNDARY, DecisionAction.REQUEST_INVENTORY, DecisionAction.HOLD, DecisionAction.STOP}:
            assert decision.can_continue is False
        if decision.chosen_action == DecisionAction.CONTINUE_AND_STAGE:
            assert decision.can_stage_to_night_auto is True
        if decision.chosen_action == DecisionAction.REQUEST_CODEX_REVIEW:
            assert decision.requires_codex_review is True
        if expected_code:
            assert decision.claim_limits
            required_limits = {
                "no_scheduler_dispatch",
                "no_active_inbox_write",
                "no_active_kb_mutation",
                "no_registry_mutation",
                "no_remote_api_call",
                "no_dataset_or_checkpoint_mutation",
            }
            assert required_limits.issubset(set(decision.claim_limits))


def test_mission_gate_result_runs_all_scenarios_with_parent_traceability():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id="mission_root",
    )

    fixture_names = [
        "bounded_decoder_smoke.yaml",
        "source_behavior_boundary.yaml",
        "superseded_taskbook.yaml",
        "weak_metric_continue_or_stop.yaml",
    ]
    fixtures = [load_replay_fixture(FIXTURE_DIR / name) for name in fixture_names]

    decisions = mission_gate_result(mission=mission, state=state, fixtures=fixtures)
    assert len(decisions) == len(fixture_names)
    assert state.current_iteration == len(fixture_names)
    assert state.total_evidence_runs == len(fixture_names)
    assert state.active_decision_count == len(fixture_names)
    assert state.last_decision_id == decisions[-1].decision_id
    assert state.parent_decision_id == decisions[-1].decision_id

    ids = [item.scenario_id for item in decisions]
    assert ids == [f.scenario_id for f in fixtures]
    for index, (decision, fixture) in enumerate(zip(decisions, fixtures)):
        if index == 0:
            assert decision.parent_decision_id == "mission_root"
        else:
            assert decision.parent_decision_id == decisions[index - 1].decision_id
        assert isinstance(decision.can_stage_to_night_auto, bool)
        assert mission_state_skeleton(mission_id=mission.mission_id)["schema_version"] == 1
        assert decision.claim_limits and "no_scheduler_dispatch" in decision.claim_limits

    assert decisions[0].chosen_action == DecisionAction.CONTINUE_AND_STAGE
    assert decisions[0].can_stage_to_night_auto is True
    assert decisions[1].chosen_action == DecisionAction.REQUEST_CODEX_REVIEW
    assert decisions[2].chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert decisions[3].chosen_action == DecisionAction.STOP


def test_mission_gate_result_parent_chain_is_preserved_across_fixtures():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
        parent_decision_id="mission_root",
    )
    fixtures = [
        load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml"),
        load_replay_fixture(FIXTURE_DIR / "missing_evidence_inventory.yaml"),
    ]

    decisions = mission_gate_result(mission=mission, state=state, fixtures=fixtures)
    assert decisions[0].parent_decision_id == "mission_root"
    assert decisions[1].parent_decision_id == decisions[0].decision_id


def test_night_researcher_decision_claim_limits_are_governance_allowlisted():
    mission = load_research_mission()
    schema = load_replay_schema()
    allowed = set(schema["required_claim_limits"])
    for fixture_path in sorted(FIXTURE_DIR.glob("*.yaml")):
        if fixture_path.name == "invalid_scenario_type.yaml":
            continue
        fixture = load_replay_fixture(fixture_path)
        state = MissionState(
            mission_id=mission.mission_id,
            status="active",
            current_iteration=0,
            total_evidence_runs=0,
            active_decision_count=0,
        )
        decision = decide_next_action(
            mission=mission,
            state=state,
            evidence=fixture.evidence,
            scenario_id=fixture.scenario_id,
            parent_decision_id=fixture.parent_decision_id,
        )
        assert set(decision.claim_limits).issubset(allowed)


def test_night_researcher_reason_codes_are_schema_allowlisted():
    mission = load_research_mission()
    schema = load_replay_schema()
    allowed = set(schema["required_reason_codes"])
    for fixture_path in sorted(FIXTURE_DIR.glob("*.yaml")):
        if fixture_path.name == "invalid_scenario_type.yaml":
            continue
        fixture = load_replay_fixture(fixture_path)
        state = MissionState(
            mission_id=mission.mission_id,
            status="active",
            current_iteration=0,
            total_evidence_runs=0,
            active_decision_count=0,
        )
        decision = decide_next_action(
            mission=mission,
            state=state,
            evidence=fixture.evidence,
            scenario_id=fixture.scenario_id,
            parent_decision_id=fixture.parent_decision_id,
        )
        assert set(decision.reason_codes).issubset(allowed)


def test_night_researcher_skeleton_outputs_match_contract():
    mission = load_research_mission()
    claim_limits = [
        "no_scheduler_dispatch", "no_active_inbox_write", "no_registry_mutation",
        "no_active_kb_mutation", "no_remote_api_call",
        "no_dataset_or_checkpoint_mutation", "claim_upgrade_via_human_only",
    ]
    assert mission_state_skeleton(
        mission_id=mission.mission_id,
        current_iteration=0,
        parent_decision_id="root",
    ) == {
        "schema_version": 1, "mission_id": mission.mission_id,
        "status": "active", "current_iteration": 0,
        "total_evidence_runs": 0, "active_decision_count": 0,
        "last_decision_id": None, "parent_decision_id": "root",
        "claim_limits": claim_limits,
    }
    decision_id = "B14_night_research_v1:seed:continue_and_stage"
    assert mission_checkpoint_skeleton(
        mission_id=mission.mission_id,
        decision_id=decision_id,
    ) == {
        "schema_version": 1, "mission_id": mission.mission_id,
        "checkpoint_id": f"{mission.mission_id}__{decision_id}",
        "status": "ready", "parent_decision_id": decision_id,
        "traceable_to_parent": True,
        "traceability_notes": "decision_id must be copied into every derived run artifact",
        "claim_limits": claim_limits,
    }


def test_night_researcher_state_skeleton_tracks_progress_counters():
    mission_id = "B14_night_research_v1"
    derived = mission_state_skeleton(
        mission_id=mission_id,
        status="active",
        current_iteration=3,
        total_evidence_runs=2,
        active_decision_count=3,
        parent_decision_id="parent-1",
    )
    assert derived["mission_id"] == mission_id
    assert derived["current_iteration"] == 3
    assert derived["total_evidence_runs"] == 2
    assert derived["active_decision_count"] == 3
    assert derived["parent_decision_id"] == "parent-1"

    defaulted = mission_state_skeleton(mission_id=mission_id, current_iteration=5)
    assert defaulted["total_evidence_runs"] == 5
    assert defaulted["active_decision_count"] == 5


def test_night_researcher_budget_exhaustion_blocks_bounded_continue():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=mission.autonomy_budget.max_iterations,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    evidence = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml").evidence

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=evidence,
        scenario_id="budget_exhausted_smoke",
        parent_decision_id=None,
    )
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert decision.status == "block"
    assert "budget_exhausted" in decision.reason_codes
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False


def test_night_researcher_budget_exhaustion_preempts_source_behavior_review():
    mission = load_research_mission()
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=mission.autonomy_budget.max_iterations,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    evidence = load_replay_fixture(FIXTURE_DIR / "source_behavior_boundary.yaml").evidence

    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=evidence,
        scenario_id="budget_exhausted_source_behavior",
        parent_decision_id=None,
    )
    assert decision.chosen_action == DecisionAction.REQUEST_HUMAN_BOUNDARY
    assert decision.status == "block"
    assert decision.reason_codes == ["budget_exhausted"]
    assert decision.requires_human_review is True
    assert decision.requires_codex_review is False
    assert decision.can_continue is False
    assert decision.can_stage_to_night_auto is False


def test_night_researcher_continuation_defaults_include_governance_claim_limits():
    mission = load_research_mission()
    fixture = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml")
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id=fixture.scenario_id,
        parent_decision_id=fixture.parent_decision_id,
    )
    assert decision.chosen_action == DecisionAction.CONTINUE_AND_STAGE
    assert "no_scheduler_dispatch" in decision.claim_limits
    assert "no_active_inbox_write" in decision.claim_limits
    assert "no_active_kb_mutation" in decision.claim_limits
    assert "no_registry_mutation" in decision.claim_limits
    assert "no_remote_api_call" in decision.claim_limits
    assert "no_dataset_or_checkpoint_mutation" in decision.claim_limits
    assert "claim_upgrade_via_human_only" in decision.claim_limits


def test_activity_narration_event_records_training_probe_status_without_side_effects():
    mission = load_research_mission()
    fixture = load_replay_fixture(FIXTURE_DIR / "bounded_decoder_smoke.yaml")
    state = MissionState(
        mission_id=mission.mission_id,
        status="active",
        current_iteration=0,
        total_evidence_runs=0,
        active_decision_count=0,
    )
    decision = decide_next_action(
        mission=mission,
        state=state,
        evidence=fixture.evidence,
        scenario_id="gend_ckpt_graph_probe",
        parent_decision_id="mission_root",
    )

    event = build_activity_narration_event(
        mission=mission,
        decision=decision,
        phase="inspect",
        headline="Locate GenD checkpoint and B14 graph entrypoints before running training.",
        current_action="checking checkpoint path and existing cache/script lineage",
        rationale="new checkpoint must be treated as a B14 input candidate; original GenD artifacts stay untouched",
        branch_options=[
            "re-extract GenD token cache before graph training",
            "train/calibrate graph only after cache provenance is known",
        ],
        blocked_by=["cache_script_mapping_not_confirmed"],
        next_step="identify current cache path and graph training script, then emit a bounded work order",
        source_refs=[
            "/srv/example-workspaces-b/B14_agent_handoff_20260521",
            "train_b14_aref_from_component_cache.py",
        ],
    )

    assert event.mission_id == mission.mission_id
    assert event.phase == "inspect"
    assert event.branch_options[0].startswith("re-extract")
    assert event.blocked_by == ["cache_script_mapping_not_confirmed"]
    assert "no_scheduler_dispatch" in event.claim_limits
    assert event.side_effects == {
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "remote_api_call": False,
        "dataset_or_checkpoint_mutation": False,
    }
    payload = event.model_dump()
    assert "chain_of_thought" not in payload
    assert "private_reasoning" not in payload

    rendered = render_activity_narration(event)
    assert "Locate GenD checkpoint" in rendered
    assert "cache_script_mapping_not_confirmed" in rendered

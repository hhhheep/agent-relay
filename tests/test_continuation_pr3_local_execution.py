from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.continuation import manual_launch
from solution_agent.continuation.bounded_validation_bucket import write_b14_l74_artifact_readiness_report
from solution_agent.continuation.mission_remote_loop import run_local_mission_remote_rehearsal
from solution_agent.continuation.night_auto_local_execution import (
    LocalExecutionAction,
    assess_local_execution_preflight,
    assess_remote_boundary_readiness,
    build_local_execution_intent,
    build_local_execution_narration_event,
    run_b14_local_execution_dry_run_pack,
    record_local_execution_cycle,
    judge_local_execution_after_result,
    load_local_execution_preflight_schema,
    load_local_execution_policy,
    load_remote_boundary_preflight_schema,
)
from solution_agent.continuation.night_auto_local_execution import PR3_CLAIM_LIMITS
from solution_agent.continuation.night_auto_morning_brief import write_morning_brief
from solution_agent.continuation.paths import continuation_night_auto_local_execution_root
from solution_agent.continuation.memory_observer import append_continuation_observation
from solution_agent.continuation.night_researcher import load_research_mission


FIXTURE_DIR = Path("tests/fixtures/night_researcher/B14")


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _manual_launch_intent_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "launch_intent_id": "li_rewo_req_p3d_01_abc123",
        "idempotency_key": "li_demo",
        "source_work_order_id": "rewo_req_p3d_01",
        "source_work_order_hash": "a" * 64,
        "dispatch_request_id": "req_p3d_01",
        "task_id": "task_p3d_01",
        "actor": "human",
        "approval": {
            "remote_launch_requested": True,
            "explicit_operator_approval": True,
        },
        "workplace_profile": "demo_profile",
        "host": "host-a",
        "remote_workdir": "/home/demo/run/task_p3d_01",
        "remote_root": "/home/demo",
        "worker": "materialized_execution",
        "line": "line_p3d",
        "batch": "batch_p3d",
        "dag_root": "dag_p3d",
        "prefix": "p3d_",
        "resource_hint": "cpu",
        "resource_lane": "cpu",
        "resource_fields": {"resource_hint": "cpu", "resource_lane": "cpu"},
        "execution_mode": "bounded_remote_launch_intent",
        "launch_enabled": False,
        "status": "ready_for_launch_preflight",
        "reasons": [],
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
        "checks": [],
    }
    payload.update(overrides)
    return payload


def _write_manual_launch_intent_payload(tmp_path: Path) -> Path:
    path = tmp_path / "continuation" / "launch_intents" / "li_demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_manual_launch_intent_payload(), sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _fixture_cases():
    fixture = _load_yaml(FIXTURE_DIR / "b14_graph_lineage_local_execution.yaml")
    return fixture["expected"]["lineage_cases"]


def _mission_remote_project_knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "current_state": {
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
        },
        "evidence_refs": [
            {"ref": "docs/B14_PROJECT_CHRONOLOGY_FOR_AGENTS_2026-05-21.md", "authority": "chronology"},
        ],
        "claim_limits": [
            {"limit": "bounded validation is proxy evidence only; no formal claim"},
        ],
        "next_safe_action": {
            "action": "Run a bounded baseline-vs-sidecar comparison probe.",
            "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
        },
    }


def _mission_remote_discussion_payload() -> dict:
    return {
        "key_uncertainty": "Whether sidecar crop-retoken candidates improve bounded bbox proxy coverage.",
        "rejected_options": [{"option": "formal IoU claim", "reason": "bounded proxy evidence only"}],
        "if_success_next": "Continue with a bounded validation bucket.",
        "if_failure_next": "Pivot back to canonical implementation planning.",
        "stop_boundaries": ["formal_claim", "core_source_behavior"],
        "fairness_protocol": {
            "baseline": "existing large-image bbox smoke without sidecar",
            "variant": "sidecar crop-retoken bbox smoke",
            "metrics": ["candidate_count", "coordinate_validity", "proxy_recall"],
            "input_scope": "same bounded large-image bucket",
            "falsification_test": "variant must not win by invalid boxes",
        },
    }


def _mission_remote_payload() -> dict:
    return {
        "mission_id": load_research_mission().mission_id,
        "proxy_budget_limit": 2,
    }


def _mission_remote_task_payload() -> dict:
    return {
        "task_id": "B14-L74-local-rehearsal",
        "task_class": "bounded_validation_bucket",
        "workplace_profile": {
            "profile": "demo_profile",
            "host": "host-a",
            "remote_root": "/home/demo",
            "remote_workdir": "/home/demo/run",
        },
        "discussion_evidence_pack": {
            "canonical_formulation": "bounded baseline-vs-sidecar validation bucket for B14-L74",
            "current_state_chronology": ["B14 chronology reviewed", "bounded bucket selected"],
            "materialized_results": ["L72 artifacts available"],
            "diagnostic_proxy_results": ["sidecar smoke is bounded proxy evidence only"],
            "negative_results": ["no formal IoU claim available"],
            "artifact_inventory": ["metrics.json", "bbox_predictions.jsonl", "evidence_payload.jsonl"],
            "claim_limits": ["proxy_evidence_only", "no_formal_claim_upgrade"],
            "open_uncertainties": ["whether sidecar reduces bbox uncertainty on the shared bounded bucket"],
        },
        "formulation_faithfulness": {
            "canonical_or_diagnostic": "diagnostic",
            "canonical_formulation": "collect bounded sidecar-vs-baseline evidence without replacing the canonical learnable model",
            "learnable_modules": ["mask/evidence model", "bbox decoder"],
            "forbidden_substitutions": ["heuristic postprocess as completion", "formal claim from proxy evidence"],
            "proxy_budget_used": 1,
            "diagnostic_cannot_prove": ["cannot prove final IoU or submission readiness"],
            "route_back_to_canonical": "use bounded evidence to choose the next canonical implementation step",
        },
        "proxy_budget_used": 1,
        "fake_collect_mode": "artifact_complete",
    }


def test_pr3_local_execution_preflight_schema_and_policy_docs_are_loadable():
    schema = load_local_execution_preflight_schema()
    assert "required_fields" in schema
    assert "required_status_values" in schema
    assert "required_execution_tasks" in schema
    assert schema["schema_version"] == 1
    policy = load_local_execution_policy()
    assert policy["policy_id"].endswith("policy_v1")
    assert policy["execution_enabled"] is False
    assert policy["dry_run_default"] is True
    boundary_schema = load_remote_boundary_preflight_schema()
    assert "status_values" in boundary_schema
    assert boundary_schema["status_values"] == ["ready", "blocked"]
    remote_boundary_policy = _load_yaml(Path(".solution/continuation/night_auto_remote_boundary_preflight_policy.yaml"))
    assert remote_boundary_policy["policy_id"].endswith("policy_v1")
    assert remote_boundary_policy["remote_launch_requires_human_approval"] is True
    assert remote_boundary_policy["no_launch_artifacts_writes"] is True
    remote_boundary_checklist = _load_yaml(Path(".solution/continuation/night_auto_remote_boundary_preflight_checklist.yaml"))
    assert remote_boundary_checklist["name"] == "continuation_night_auto_remote_boundary_preflight"
    assert remote_boundary_checklist["policy"] == "continuation_night_auto_remote_boundary_preflight"
    assert remote_boundary_checklist["checks"]
    assert {item["id"] for item in remote_boundary_checklist["checks"]} >= {
        "mission_identified",
        "preflight_status_known",
        "remote_boundary_not_reached",
        "side_effects_guarded",
    }
    boundary_doc = Path("docs/CONTINUATION_B14_BOUNDARY_READINESS_P3G.md").read_text(encoding="utf-8")
    assert "remote_boundary" in boundary_doc
    assert "No remote launch" in boundary_doc or "no remote launch" in boundary_doc.lower()
    assert "hard boundary" in boundary_doc.lower()


def test_pr3_local_execution_loads_cached_b14_fixture_and_verifies_branching():
    mission = load_research_mission()
    cases = _fixture_cases()
    for case in cases:
        intent = build_local_execution_intent(
            mission_id=mission.mission_id,
            proposal_run_id="b14_local_execution_run",
            candidate_id=f"{mission.mission_id}_{case['case_id']}",
            scenario_id="b14_lineage_test",
            execution_task=case["execution_task"],
            checkpoint_path=case["checkpoint_path"],
            cache_path=case["cache_path"],
            script_path=case["script_path"],
            script_cache_mapping_confirmed=case["script_cache_mapping_confirmed"],
            branch_override=case.get("branch_override", ""),
        )
        preflight = assess_local_execution_preflight(intent)
        expected = case["expected"]
        assert preflight["branch"] == expected["branch"]
        assert preflight["status"] == expected["preflight_status"]
        assert preflight["execution_task"] == case["execution_task"]
        assert set(preflight["claim_limits"]) == set(PR3_CLAIM_LIMITS)

        if expected["preflight_status"] == "blocked":
            assert expected["preflight_reason"] in preflight["reasons"]
            continue

        judge = judge_local_execution_after_result(
            preflight,
            {
                "execution_success": True,
                "information_gain": 0.06,
                "worth_continuing_threshold": 0.03,
            },
        )
        assert judge["next_action"] == expected["judge_next_action_when_success"]
        assert judge["execution_success"] is True
        assert judge["worth_continuing"] is True
        assert judge["claim_limit"] is False


def test_pr3_local_execution_blocks_unconfirmed_graph_lineage_and_does_not_dispatch():
    mission = load_research_mission()
    case = next(item for item in _fixture_cases() if item["case_id"] == "graph_training_unconfirmed_blocked")
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="lineage_blocked",
        scenario_id="b14_lineage_test",
        execution_task=case["execution_task"],
        checkpoint_path=case["checkpoint_path"],
        cache_path=case["cache_path"],
        script_path=case["script_path"],
        script_cache_mapping_confirmed=case["script_cache_mapping_confirmed"],
        branch_override=case["branch_override"],
    )
    preflight = assess_local_execution_preflight(intent)

    assert preflight["status"] == "blocked"
    assert "cache_script_mapping_not_confirmed" in preflight["reasons"]
    assert preflight["side_effects"]["scheduler_dispatch"] is False
    assert preflight["side_effects"]["active_inbox_write"] is False
    assert preflight["side_effects"]["registry_mutation"] is False
    assert preflight["side_effects"]["remote_api_call"] is False

    judge = judge_local_execution_after_result(preflight, {"execution_success": False, "information_gain": 0.0})
    assert judge["next_action"] == LocalExecutionAction.HOLD
    assert judge["worth_continuing"] is False
    assert judge["stop_reason"] == "cache_script_mapping_not_confirmed"


def test_pr3_local_execution_graph_training_blocks_on_missing_script_lineage():
    mission = load_research_mission()
    case = next(item for item in _fixture_cases() if item["case_id"] == "graph_training_missing_script_path_blocked")
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="lineage_script_missing",
        scenario_id="b14_lineage_test",
        execution_task=case["execution_task"],
        checkpoint_path=case["checkpoint_path"],
        cache_path=case["cache_path"],
        script_path=case["script_path"],
        script_cache_mapping_confirmed=case["script_cache_mapping_confirmed"],
        branch_override=case["branch_override"],
    )
    preflight = assess_local_execution_preflight(intent)

    assert preflight["branch"] == "graph_training"
    assert preflight["status"] == "blocked"
    assert "missing_script_path" in preflight["reasons"]
    assert preflight["lineage"]["checkpoint_path"] == case["checkpoint_path"]
    assert preflight["lineage"]["cache_path"] == case["cache_path"]
    assert preflight["lineage"]["script_path"] == ""


def test_pr3_cpu_inventory_preflight_requires_positive_inventory_and_yields_run_action():
    mission = load_research_mission()
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="cpu_inventory",
        scenario_id="b14_cpu_inventory_test",
        execution_task="cpu_inventory",
        checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
        cache_path="/ssd6/B14/cache/token_cache",
        script_path="/home/agent/diagnostic.py",
        script_cache_mapping_confirmed=False,
        available_cpu_cores=128,
        branch_override="cache_reextraction",
    )
    preflight = assess_local_execution_preflight(intent)
    assert preflight["status"] == "pass"
    assert preflight["action"] == LocalExecutionAction.RUN

    judge = judge_local_execution_after_result(preflight, {"execution_success": True, "information_gain": 0.10})
    assert judge["execution_success"] is True
    assert judge["next_action"] == LocalExecutionAction.INSPECT


def test_pr3_local_execution_narration_event_is_public_and_safe():
    mission = load_research_mission()
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="lineage_repro",
        scenario_id="b14_narration",
        execution_task="bounded_smoke",
        checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
        cache_path="/ssd6/B14/cache/token_cache",
        script_path="/home/agent/train_graph.py",
        script_cache_mapping_confirmed=False,
        branch_override="cache_reextraction",
    )
    preflight = assess_local_execution_preflight(intent)
    judge = judge_local_execution_after_result(preflight, {"execution_success": False, "information_gain": 0.0, "worth_continuing_threshold": 0.05})

    event = build_local_execution_narration_event(
        mission=mission,
        intent=preflight,
        decision=judge,
        phase=judge["next_action"],
    )

    assert event.phase in {LocalExecutionAction.REPAIR.value, LocalExecutionAction.HOLD.value}
    assert event.claim_limits == PR3_CLAIM_LIMITS
    assert event.side_effects == {
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "remote_api_call": False,
        "dataset_or_checkpoint_mutation": False,
    }
    assert "local execution bounded_smoke" in event.headline
    assert "cache re-extraction" in "".join(event.branch_options)


def test_pr3_local_execution_records_and_replays_phase_events():
    mission = load_research_mission()

    cases = [
        (
            LocalExecutionAction.RUN.value,
            build_local_execution_intent(
                mission_id=mission.mission_id,
                proposal_run_id="b14_local_execution_cycle_run",
                candidate_id="phase_run",
                scenario_id="phase_run",
                execution_task="bounded_smoke",
                checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
                cache_path="/ssd6/B14/cache/token_cache",
                script_path="/home/agent/train_graph.py",
                script_cache_mapping_confirmed=False,
                branch_override="cache_reextraction",
            ),
            {"execution_success": True, "information_gain": 0.12, "worth_continuing_threshold": 0.05},
        ),
        (
            LocalExecutionAction.INSPECT.value,
            build_local_execution_intent(
                mission_id=mission.mission_id,
                proposal_run_id="b14_local_execution_cycle_inspect",
                candidate_id="phase_inspect",
                scenario_id="phase_inspect",
                execution_task="cpu_inventory",
                checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
                cache_path="/ssd6/B14/cache/token_cache",
                script_path="/home/agent/diagnostic.py",
                script_cache_mapping_confirmed=False,
                available_cpu_cores=64,
                branch_override="cache_reextraction",
            ),
            {"execution_success": True, "information_gain": 0.12, "worth_continuing_threshold": 0.05},
        ),
        (
            LocalExecutionAction.HOLD.value,
            build_local_execution_intent(
                mission_id=mission.mission_id,
                proposal_run_id="b14_local_execution_cycle_hold",
                candidate_id="phase_hold",
                scenario_id="phase_hold",
                execution_task="bounded_smoke",
                checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
                cache_path="/ssd6/B14/cache/token_cache",
                script_path="/home/agent/train_graph.py",
                script_cache_mapping_confirmed=False,
                branch_override="cache_reextraction",
            ),
            {"execution_success": True, "information_gain": 0.01, "worth_continuing_threshold": 0.05},
        ),
        (
            LocalExecutionAction.REPAIR.value,
            build_local_execution_intent(
                mission_id=mission.mission_id,
                proposal_run_id="b14_local_execution_cycle_repair",
                candidate_id="phase_repair",
                scenario_id="phase_repair",
                execution_task="bounded_smoke",
                checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
                cache_path="/ssd6/B14/cache/token_cache",
                script_path="/home/agent/train_graph.py",
                script_cache_mapping_confirmed=False,
                branch_override="cache_reextraction",
            ),
            {"execution_success": False, "information_gain": 0.0, "worth_continuing_threshold": 0.05},
        ),
    ]

    for expected_action, intent, result in cases:
        preflight = assess_local_execution_preflight(intent)
        cycle = record_local_execution_cycle(mission=mission, preflight=preflight, result=result)
        decision = cycle["decision"]
        bundle = cycle["narration_bundle"]

        assert decision["next_action"] == expected_action
        assert cycle["resolved_phase"] == decision["next_action"]
        assert cycle["narration_event"]["phase"] == decision["next_action"]
        assert decision["next_action"] in cycle["narration_event"]["current_action"]
        assert bundle["created"] is True or bundle["replayed"] is True
        assert Path(bundle["bundle_path"]).exists()

        repeated = record_local_execution_cycle(mission=mission, preflight=preflight, result=result)
        assert repeated["narration_bundle"]["replayed"] is True
        assert repeated["narration_bundle"]["created"] is False


def test_pr3_morning_brief_consumes_local_execution_activity_events():
    mission = load_research_mission()
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_morning_brief_run",
        candidate_id="morning_brief_candidate",
        scenario_id="morning_brief_scenario",
        execution_task="bounded_smoke",
        checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
        cache_path="/ssd6/B14/cache/token_cache",
        script_path="/home/agent/train_graph.py",
        script_cache_mapping_confirmed=False,
        branch_override="graph_training",
    )
    preflight = assess_local_execution_preflight(intent)
    cycle = record_local_execution_cycle(
        mission=mission,
        preflight=preflight,
        result={"execution_success": False, "information_gain": 0.0, "worth_continuing_threshold": 0.05},
    )
    assert cycle["narration_bundle"]["bundle_path"]

    brief = write_morning_brief(mission_id=mission.mission_id)

    assert brief["status"] == "ready"
    assert brief["event_count"] >= 1
    assert brief["created"] is True or brief["replayed"] is True
    assert Path(brief["brief_path"]).exists()
    assert Path(brief["summary_path"]).exists()
    assert "cache_script_mapping_not_confirmed" in brief["summary"]["blockers"]
    assert brief["summary"]["side_effects"] == {
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "remote_api_call": False,
        "dataset_or_checkpoint_mutation": False,
    }

    text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "Night Researcher Morning Brief" in text
    assert "cache_script_mapping_not_confirmed" in text

    assert "blocked" in brief["summary"]["preflight_statuses"]
    assert brief["summary"]["latest_preflight_status"] == "blocked"
    assert "cache_script_mapping_not_confirmed" in brief["summary"]["judge_stop_reasons"]
    assert "hold" in brief["summary"]["judge_next_actions"]
    assert brief["summary"]["latest_judge_next_action"] == "hold"
    assert brief["summary"]["latest_judge_stop_reason"] == "cache_script_mapping_not_confirmed"
    assert brief["summary"]["latest_judge_decision"]["next_action"] == "hold"
    assert brief["summary"]["latest_judge_decision"]["stop_reason"] == "cache_script_mapping_not_confirmed"
    assert brief["summary"]["latest_next_step"] == "hold"
    assert brief["summary"]["remote_boundary"] == "not_reached"
    assert "latest_preflight_status: blocked" in text
    assert "latest_judge_decision: {\"next_action\": \"hold\", \"stop_reason\": \"cache_script_mapping_not_confirmed\"}" in text
    assert "latest_next_step: hold" in text
    assert "remote_boundary: not_reached" in text

    replay = write_morning_brief(mission_id=mission.mission_id)
    assert replay["replayed"] is True
    assert replay["created"] is False


def test_pr3_morning_brief_includes_manual_launch_readiness(tmp_path: Path, monkeypatch) -> None:
    mission = load_research_mission()
    _patch_root(tmp_path, monkeypatch)
    launch_intent_path = _write_manual_launch_intent_payload(tmp_path)

    preflight = manual_launch.write_manual_launch_preflight(
        launch_intent_path,
        actor="human",
        operator_confirmed=False,
    )
    assert preflight["status"] == "blocked"
    assert preflight["ready_for_manual_remote_launch"] is False
    assert preflight["preflight_path"]

    brief = write_morning_brief(mission_id=mission.mission_id)
    summary = brief["summary"]
    assert summary["latest_manual_launch_preflight_status"] == "blocked"
    assert summary["manual_launch_ready_for_remote_launch"] is False
    assert summary["manual_launch_preflight_statuses"] == ["blocked"]
    assert summary["manual_launch_blockers"] == ["explicit_manual_launch_confirmation_required"]
    assert summary["manual_launch_next_required_approval"] == preflight["next_required_approval"]
    assert summary["manual_launch_preflight_path"] == preflight["preflight_path"]
    assert summary["remote_boundary"] == "not_reached"
    assert summary["remote_boundary_reached"] is False

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "## Remote Boundary" in brief_text
    assert "manual_launch_preflight_status: blocked" in brief_text
    assert "manual_launch_ready_for_remote_launch: false" in brief_text
    assert "remote_boundary_reached: false" in brief_text


def test_pr3_morning_brief_includes_remote_boundary_report_path(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]

    pack = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_brief_ptr",
    )

    assert pack["remote_boundary_report"]["status"] == "ready"
    assert pack["remote_boundary_report"]["path"] != ""

    brief = write_morning_brief(mission_id=mission.mission_id)
    assert brief["summary"]["remote_boundary_report_path"] == pack["remote_boundary_report"]["path"]

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert f"remote_boundary_report_path: {pack['remote_boundary_report']['path']}" in brief_text


def test_pr3_morning_brief_includes_remote_boundary_readiness_summary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]

    _ = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_brief_readiness",
    )

    brief = write_morning_brief(mission_id=mission.mission_id)
    summary = brief["summary"]
    assert summary["remote_boundary_report_status"] == "ready"
    assert summary["remote_boundary_case_count"] == 2
    assert summary["remote_boundary_ready_case_count"] == 2
    assert summary["remote_boundary_blocked_case_count"] == 0
    assert "inspect" in summary["remote_boundary_next_steps"]
    assert summary["remote_boundary_blockers"] == []

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "remote_boundary_report_status: ready" in brief_text
    assert "remote_boundary_case_count: 2" in brief_text
    assert "remote_boundary_ready_case_count: 2" in brief_text
    assert "remote_boundary_blocked_case_count: 0" in brief_text
    assert "- remote_boundary_next_steps:" in brief_text
    assert "  - inspect" in brief_text


def test_pr3_morning_brief_includes_b14_l74_artifact_readiness_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    output_dir = (
        continuation_night_auto_local_execution_root(mission.mission_id)
        / "artifact_readiness_probe"
        / "candidate_a"
        / "collect"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(json.dumps({"status": "ok"}, indent=2), encoding="utf-8")
    (output_dir / "bbox_predictions.jsonl").write_text(
        json.dumps({"image_id": "img-001", "bbox": [0, 0, 10, 10]}) + "\n",
        encoding="utf-8",
    )
    report = write_b14_l74_artifact_readiness_report(output_dir)

    brief = write_morning_brief(mission_id=mission.mission_id)
    summary = brief["summary"]
    assert summary["b14_l74_artifact_readiness_report_path"] == report["report_path"]
    assert summary["b14_l74_artifact_readiness_status"] == "pivot"
    assert (
        summary["b14_l74_artifact_readiness_next_recommendation"]
        == "pivot_without_sidecar_mechanism_evidence"
    )
    assert "sidecar_evidence_missing_or_invalid" in summary["b14_l74_artifact_readiness_route_reasons"]
    assert "evidence_payload:missing" in summary["b14_l74_artifact_readiness_reasons"]
    assert summary["b14_l74_artifact_readiness_present_valid_artifact_ids"] == [
        "metrics",
        "bbox_predictions",
    ]
    assert summary["b14_l74_artifact_readiness_missing_artifact_ids"] == [
        "evidence_payload",
        "closure_decision",
        "action_requests",
    ]
    assert summary["b14_l74_artifact_readiness_invalid_artifact_ids"] == []

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "b14_l74_artifact_readiness_status: pivot" in brief_text
    assert "b14_l74_artifact_readiness_next_recommendation: pivot_without_sidecar_mechanism_evidence" in brief_text
    assert "- b14_l74_artifact_readiness_route_reasons:" in brief_text
    assert "  - sidecar_evidence_missing_or_invalid" in brief_text
    assert "- b14_l74_artifact_readiness_present_valid_artifact_ids:" in brief_text
    assert "  - metrics" in brief_text
    assert "  - bbox_predictions" in brief_text
    assert "- b14_l74_artifact_readiness_missing_artifact_ids:" in brief_text
    assert "  - evidence_payload" in brief_text
    assert "  - closure_decision" in brief_text
    assert "  - action_requests" in brief_text
    assert f"b14_l74_artifact_readiness_report_path: {report['report_path']}" in brief_text


def test_pr3_morning_brief_includes_continuation_observations(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()

    first = append_continuation_observation(
        run_id=mission.mission_id,
        stage="proposal",
        actor="researcher",
        status="emitted",
        idempotency_key="obs-smoke-001",
        source_task_id="task_001",
        reasons=["first signal", "seeded"],
        artifact_path="continuation/proposal/foo.txt",
        payload={"decision": "continue"},
    )
    second = append_continuation_observation(
        run_id=mission.mission_id,
        stage="repair_plan",
        actor="planner",
        status="emitted",
        idempotency_key="obs-smoke-002",
        source_task_id="task_001",
        reasons=["needs_patch_intent"],
        artifact_path="continuation/source_repair/bar.json",
        payload={"decision": "bounded_self"},
    )
    assert first["written"] is True
    assert second["written"] is True

    brief = write_morning_brief(mission_id=mission.mission_id)
    summary = brief["summary"]
    assert summary["continuation_observations"]["count"] == 2
    assert summary["continuation_observations"]["latest_stage"] == "repair_plan"
    assert summary["continuation_observations"]["latest_status"] == "emitted"
    assert summary["continuation_observations"]["latest_payload"] == {"decision": "bounded_self"}
    assert summary["continuation_observations"]["stages"] == ["proposal", "repair_plan"]
    assert summary["continuation_observations"]["actors"] == ["researcher", "planner"]

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "## Continuation Observations" in brief_text
    assert "- count: 2" in brief_text
    assert "- latest_stage: repair_plan" in brief_text
    assert "- latest_status: emitted" in brief_text
    assert '- latest_payload: {"decision": "bounded_self"}' in brief_text
    assert "- stages:" in brief_text
    assert "  - proposal" in brief_text
    assert "  - repair_plan" in brief_text
    assert "  - planner" in brief_text
    assert "  - researcher" in brief_text


def test_pr3_b14_dry_run_pack_rehearsal_ready_and_replayable(tmp_path: Path, monkeypatch):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]

    result = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_rehearsal_ready",
    )

    assert result["status"] == "ready"
    assert result["case_count"] == 2
    assert result["launch_ready_case_count"] == 2
    assert result["blocked_case_count"] == 0
    assert result["reasons"] == []
    assert result["morning_brief"]["status"] == "ready"
    assert result["morning_brief"]["event_count"] >= 2
    assert result["morning_brief"]["remote_boundary"] == "not_reached"
    assert result["remote_boundary_report"]["status"] == "ready"
    assert result["remote_boundary_report"]["ready_case_count"] == 2
    assert result["remote_boundary_report"]["blocked_case_count"] == 0
    assert result["remote_boundary_report_path"] == result["remote_boundary_report"]["path"] != ""
    assert result["remote_boundary_readiness_report_path"] == result["remote_boundary_report"]["path"] != ""
    assert Path(result["remote_boundary_report"]["path"]).exists()
    remote_report = json.loads(Path(result["remote_boundary_report"]["path"]).read_text(encoding="utf-8"))
    assert remote_report["status"] == "ready"
    assert remote_report["remote_boundary"] == "not_reached"
    assert remote_report["case_count"] == 2
    for case_result in result["cases"]:
        assert case_result["launch_intent"]["status"] == "ready"
        assert case_result["launch_intent"]["can_request_remote_launch"] is True
        assert case_result["launch_intent"]["remote_boundary"] == "not_reached"
        assert case_result["blockers"] == []
    summary_path = Path(result["summary_path"])
    assert summary_path.exists()
    assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "ready"

    replay = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_rehearsal_ready",
        write_summary=False,
    )
    assert replay["replayed"] is False
    assert replay["created"] is False
    assert replay["summary_path"] == ""
    assert replay["remote_boundary_report"]["path"] == ""
    assert replay["remote_boundary_report_path"] == ""


def test_pr3_b14_dry_run_pack_surfaces_latest_b14_l74_artifact_readiness_in_machine_payload(
    tmp_path: Path, monkeypatch
):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]
    output_dir = (
        continuation_night_auto_local_execution_root(mission.mission_id)
        / "artifact_readiness_probe"
        / "candidate_machine_payload"
        / "collect"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(json.dumps({"status": "ok"}, indent=2), encoding="utf-8")
    (output_dir / "bbox_predictions.jsonl").write_text(
        json.dumps({"image_id": "img-001", "bbox": [0, 0, 10, 10]}) + "\n",
        encoding="utf-8",
    )
    report = write_b14_l74_artifact_readiness_report(output_dir)

    result = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_readiness_mp",
    )

    assert result["morning_brief"]["b14_l74_artifact_readiness_report_path"] == report["report_path"]
    assert result["morning_brief"]["b14_l74_artifact_readiness_status"] == "pivot"
    assert (
        result["morning_brief"]["b14_l74_artifact_readiness_next_recommendation"]
        == "pivot_without_sidecar_mechanism_evidence"
    )
    assert result["morning_brief"]["b14_l74_artifact_readiness_present_valid_artifact_ids"] == [
        "metrics",
        "bbox_predictions",
    ]
    assert result["morning_brief"]["b14_l74_artifact_readiness_missing_artifact_ids"] == [
        "evidence_payload",
        "closure_decision",
        "action_requests",
    ]
    assert result["morning_brief"]["b14_l74_artifact_readiness_invalid_artifact_ids"] == []

    summary = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
    assert summary["morning_brief"]["b14_l74_artifact_readiness_report_path"] == report["report_path"]
    assert summary["morning_brief"]["b14_l74_artifact_readiness_status"] == "pivot"


def test_pr3_remote_boundary_readiness_ready_when_local_criteria_met():
    mission = load_research_mission()
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="ready_boundary_candidate",
        scenario_id="b14_boundary_ready",
        execution_task="bounded_smoke",
        checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
        cache_path="/ssd6/B14/cache/token_cache",
        script_path="/home/agent/train_graph.py",
        script_cache_mapping_confirmed=True,
        branch_override="graph_training",
    )
    preflight = assess_local_execution_preflight(intent)
    decision = judge_local_execution_after_result(
        preflight,
        {"execution_success": True, "information_gain": 0.12, "worth_continuing_threshold": 0.05},
    )
    boundary = assess_remote_boundary_readiness(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="ready_boundary_candidate",
        case_id="ready_case",
        preflight=preflight,
        decision=decision,
    )

    assert boundary["status"] == "ready"
    assert boundary["can_request_remote_launch"] is True
    assert boundary["remote_boundary"] == "not_reached"
    assert boundary["judge_next_action"] == "inspect"
    assert boundary["blockers"] == []
    assert boundary["next_required_approval"] == "none"


def test_pr3_remote_boundary_readiness_blocked_reports_next_required_approval():
    mission = load_research_mission()
    blocked_case = next(item for item in _fixture_cases() if item["case_id"] == "graph_training_unconfirmed_blocked")
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="blocked_boundary_candidate",
        scenario_id="b14_boundary_blocked",
        execution_task=blocked_case["execution_task"],
        checkpoint_path=blocked_case["checkpoint_path"],
        cache_path=blocked_case["cache_path"],
        script_path=blocked_case["script_path"],
        script_cache_mapping_confirmed=bool(blocked_case["script_cache_mapping_confirmed"]),
        branch_override=blocked_case["branch_override"],
    )
    preflight = assess_local_execution_preflight(intent)
    decision = judge_local_execution_after_result(preflight, {"execution_success": False, "information_gain": 0.0})
    boundary = assess_remote_boundary_readiness(
        mission_id=mission.mission_id,
        proposal_run_id="b14_local_execution_run",
        candidate_id="blocked_boundary_candidate",
        case_id="blocked_boundary_case",
        preflight=preflight,
        decision=decision,
    )

    assert boundary["status"] == "blocked"
    assert boundary["can_request_remote_launch"] is False
    assert boundary["next_required_approval"] == "cache_script_mapping_not_confirmed"


def test_pr3_b14_dry_run_pack_rehearsal_blocks_on_unconfirmed_graph_lineage(tmp_path: Path, monkeypatch):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    case = next(item for item in _fixture_cases() if item["case_id"] == "graph_training_unconfirmed_blocked")

    result = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=[case],
        proposal_run_id="b14_pack_rehearsal_blocked",
    )

    assert result["status"] == "blocked"
    assert result["case_count"] == 1
    assert result["launch_ready_case_count"] == 0
    assert result["blocked_case_count"] == 1
    assert result["blocked_cases"] == ["graph_training_unconfirmed_blocked"]
    assert "cache_script_mapping_not_confirmed" in result["reasons"]
    assert "blocked" not in result["reasons"]
    blocked_case = result["cases"][0]
    assert blocked_case["launch_intent"]["status"] == "blocked"
    assert blocked_case["launch_intent"]["next_required_approval"] == "cache_script_mapping_not_confirmed"
    assert blocked_case["blockers"] == ["cache_script_mapping_not_confirmed"]
    assert blocked_case["decision"]["next_action"] == "hold"
    assert result["morning_brief"]["event_count"] >= 1
    assert result["morning_brief"]["status"] == "ready"
    assert result["remote_boundary_report"]["status"] == "blocked"
    assert result["remote_boundary_report"]["blocked_case_count"] == 1
    assert Path(result["remote_boundary_report"]["path"]).exists()
    remote_report = json.loads(Path(result["remote_boundary_report"]["path"]).read_text(encoding="utf-8"))
    assert remote_report["status"] == "blocked"
    assert remote_report["blocked_case_ids"] == ["graph_training_unconfirmed_blocked"]
    assert "cache_script_mapping_not_confirmed" in remote_report["summary"]["all_case_blockers"]
    summary = json.loads(Path(result["morning_brief"]["summary_path"]).read_text(encoding="utf-8"))
    assert summary["remote_boundary"] == "not_reached"
    assert "cache_script_mapping_not_confirmed" in summary["blockers"]


def test_pr3_morning_brief_cli_entrypoint_is_local_only(tmp_path: Path, monkeypatch):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    intent = build_local_execution_intent(
        mission_id=mission.mission_id,
        proposal_run_id="cli_mb",
        candidate_id="cand",
        scenario_id="cli_mb",
        execution_task="bounded_smoke",
        checkpoint_path="/ssd6/B14/checkpoints/pt_checkpoint.pt",
        cache_path="/ssd6/B14/cache/token_cache",
        script_path="/home/agent/train_graph.py",
        script_cache_mapping_confirmed=False,
        branch_override="graph_training",
    )
    preflight = assess_local_execution_preflight(intent)
    record_local_execution_cycle(
        mission=mission,
        preflight=preflight,
        result={"execution_success": False, "information_gain": 0.0, "worth_continuing_threshold": 0.05},
    )

    result = CliRunner().invoke(cli, ["night-researcher", "morning-brief", "--mission-id", mission.mission_id])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["event_count"] == 1
    assert Path(payload["brief_path"]).exists()
    assert Path(payload["summary_path"]).exists()
    summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
    assert summary["side_effects"] == {
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "remote_api_call": False,
        "dataset_or_checkpoint_mutation": False,
    }


def test_pr3_morning_brief_includes_mission_remote_rehearsal_summary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    first_task = _mission_remote_task_payload()
    first_task["fake_collect_mode"] = "artifact_complete"
    run_local_mission_remote_rehearsal(
        _mission_remote_payload(),
        first_task,
        run_root=tmp_path / "outputs" / "night_runs" / mission.mission_id,
        project_knowledge=_mission_remote_project_knowledge(),
        discussion_payload=_mission_remote_discussion_payload(),
    )
    second_task = _mission_remote_task_payload()
    second_task["fake_collect_mode"] = "pivot"
    run_local_mission_remote_rehearsal(
        _mission_remote_payload(),
        second_task,
        run_root=tmp_path / "outputs" / "night_runs" / mission.mission_id,
        project_knowledge=_mission_remote_project_knowledge(),
        discussion_payload=_mission_remote_discussion_payload(),
    )

    brief = write_morning_brief(mission_id=mission.mission_id)
    summary = brief["summary"]

    assert brief["status"] == "ready"
    assert summary["mission_remote"]["present"] is True
    assert summary["mission_remote"]["iteration_count"] == 2
    assert summary["mission_remote"]["checkpoint_count"] == 2
    assert summary["mission_remote"]["latest_gate_status"] == "allow"
    assert summary["mission_remote"]["latest_judge_status"] == "pivot"
    assert summary["mission_remote"]["latest_continuation_status"] == "ready"
    assert summary["mission_remote"]["latest_next_action"] == "pivot"
    assert summary["mission_remote"]["latest_information_gain_total_score"] == 8
    assert (
        summary["mission_remote"]["latest_information_gain_routing"]
        == "useful_bounded_evidence_continue_or_expand"
    )
    assert summary["mission_remote"]["budget_ledger_entries"] == 2
    assert Path(summary["mission_remote"]["latest_factual_memory_path"]).exists()
    assert summary["mission_remote"]["latest_kb_update_status"] == "ready"
    assert summary["mission_remote"]["latest_kb_update_proposal_id"] != ""
    assert Path(summary["mission_remote"]["latest_kb_update_proposal_dir"]).exists()
    assert Path(summary["mission_remote"]["latest_kb_update_global_proposal_dir"]).exists()
    assert summary["mission_remote"]["latest_followup_proposal_status"] == "ready"
    assert summary["mission_remote"]["latest_followup_can_write_proposal"] is True
    assert summary["mission_remote"]["latest_followup_candidate_count"] == 1
    assert summary["mission_remote"]["latest_followup_reasons"] == []
    assert len(summary["mission_remote"]["history"]) == 2
    assert summary["mission_remote"]["history"][0]["iteration_id"] == "iteration_001"
    assert summary["mission_remote"]["history"][0]["next_action"] == "continue"
    assert summary["mission_remote"]["history"][1]["iteration_id"] == "iteration_002"
    assert summary["mission_remote"]["history"][1]["next_action"] == "pivot"
    assert Path(summary["mission_remote"]["budget_ledger_path"]).exists()
    assert Path(summary["mission_remote"]["latest_checkpoint_path"]).exists()
    assert summary["continuation_observations"]["count"] == 2
    assert summary["continuation_observations"]["latest_stage"] == "mission_remote_rehearsal"
    assert summary["continuation_observations"]["latest_status"] == "ready"
    assert summary["continuation_observations"]["latest_source_task_id"] == "B14-L74-local-rehearsal"
    assert summary["continuation_observations"]["stages"] == ["mission_remote_rehearsal"]
    assert summary["continuation_observations"]["actors"] == ["mission_remote_loop"]
    assert summary["continuation_observations"]["latest_payload"]["judge_status"] == "pivot"
    assert summary["continuation_observations"]["latest_payload"]["next_action"] == "pivot"
    assert summary["continuation_observations"]["latest_payload"]["iteration_id"] == "iteration_002"

    brief_text = Path(brief["brief_path"]).read_text(encoding="utf-8")
    assert "## Mission Remote Rehearsal" in brief_text
    assert "## Continuation Observations" in brief_text
    assert "- count: 2" in brief_text
    assert "- latest_stage: mission_remote_rehearsal" in brief_text
    assert "- latest_status: ready" in brief_text
    assert "- latest_source_task_id: B14-L74-local-rehearsal" in brief_text
    assert '"judge_status": "pivot"' in brief_text
    assert '"next_action": "pivot"' in brief_text
    assert "- checkpoint_count: 2" in brief_text
    assert "- latest_gate_status: allow" in brief_text
    assert "- latest_judge_status: pivot" in brief_text
    assert "- latest_next_action: pivot" in brief_text
    assert "- latest_information_gain_total_score: 8" in brief_text
    assert "- latest_information_gain_routing: useful_bounded_evidence_continue_or_expand" in brief_text
    assert "- latest_kb_update_status: ready" in brief_text
    assert "- latest_followup_proposal_status: ready" in brief_text
    assert "- latest_followup_can_write_proposal: true" in brief_text
    assert "- latest_followup_candidate_count: 1" in brief_text
    assert "- recent_history:" in brief_text
    assert "  - iteration_001: gate=allow judge=artifact_complete next_action=continue" in brief_text
    assert "  - iteration_002: gate=allow judge=pivot next_action=pivot" in brief_text


def test_pr3_b14_dry_run_pack_cli_entrypoint_is_local_only(tmp_path: Path, monkeypatch):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    selected_cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]
    cases_path = tmp_path / "b14_pack_cases.json"
    cases_path.write_text(json.dumps(selected_cases, ensure_ascii=False, indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-dry-run-pack",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_pack",
            "--cases-path",
            str(cases_path),
            "--actor",
            "cli-local-only",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["case_count"] == 2
    assert payload["launch_ready_case_count"] == 2
    assert payload["blocked_case_count"] == 0
    assert payload["morning_brief"]["status"] == "ready"
    assert payload["morning_brief"]["remote_boundary"] == "not_reached"
    assert payload["remote_boundary"] == "not_reached"
    assert Path(payload["summary_path"]).exists()

    summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
    assert summary["status"] == "ready"
    assert summary["remote_boundary"] == "not_reached"
    assert summary["morning_brief"]["remote_boundary"] == "not_reached"
    assert summary["remote_boundary_report_path"] == summary["remote_boundary_report"]["path"]
    assert summary["remote_boundary_readiness_report_path"] == summary["remote_boundary_report"]["path"]
    assert Path(summary["remote_boundary_report_path"]).exists()


def test_pr3_b14_dry_run_pack_preserves_mission_remote_summary_in_machine_payload(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    rehearsal_task = _mission_remote_task_payload()
    rehearsal_task["fake_collect_mode"] = "artifact_complete"
    run_local_mission_remote_rehearsal(
        _mission_remote_payload(),
        rehearsal_task,
        run_root=tmp_path / "outputs" / "night_runs" / mission.mission_id,
        project_knowledge=_mission_remote_project_knowledge(),
        discussion_payload=_mission_remote_discussion_payload(),
    )
    cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]

    result = run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id="b14_pack_mission_remote",
    )

    assert result["morning_brief"]["status"] == "ready"
    assert result["mission_remote"]["present"] is True
    assert result["mission_remote"]["iteration_count"] == 1
    assert result["mission_remote"]["latest_judge_status"] == "artifact_complete"
    assert result["mission_remote"]["latest_next_action"] == "continue"
    assert result["continuation_observations"]["count"] == 1
    assert result["continuation_observations"]["latest_stage"] == "mission_remote_rehearsal"
    assert result["continuation_observations"]["latest_status"] == "ready"
    assert result["morning_brief"]["mission_remote"]["present"] is True
    assert result["morning_brief"]["mission_remote"]["iteration_count"] == 1
    assert result["morning_brief"]["mission_remote"]["latest_judge_status"] == "artifact_complete"
    assert result["morning_brief"]["mission_remote"]["latest_next_action"] == "continue"
    assert result["morning_brief"]["continuation_observations"]["count"] == 1
    assert result["morning_brief"]["continuation_observations"]["latest_stage"] == "mission_remote_rehearsal"
    assert result["remote_boundary_report"]["mission_remote"]["present"] is True
    assert result["remote_boundary_report"]["mission_remote"]["latest_next_action"] == "continue"
    assert (
        result["remote_boundary_report"]["mission_remote"]["latest_information_gain_total_score"] == 9
    )
    assert result["remote_boundary_report"]["mission_remote"]["latest_kb_update_status"] == "ready"
    assert result["remote_boundary_report"]["mission_remote"]["latest_followup_proposal_status"] == "ready"
    assert result["remote_boundary_report"]["continuation_observations"]["count"] == 1
    assert (
        result["remote_boundary_report"]["continuation_observations"]["latest_stage"]
        == "mission_remote_rehearsal"
    )
    assert (
        result["remote_boundary_report"]["continuation_observations"]["latest_status"] == "ready"
    )
    assert (
        result["morning_brief"]["continuation_observations"]["latest_payload"]["iteration_id"]
        == "iteration_001"
    )

    summary = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
    assert summary["mission_remote"]["present"] is True
    assert summary["mission_remote"]["latest_next_action"] == "continue"
    assert summary["continuation_observations"]["latest_status"] == "ready"
    assert summary["morning_brief"]["mission_remote"]["present"] is True
    assert summary["morning_brief"]["mission_remote"]["latest_next_action"] == "continue"
    assert summary["morning_brief"]["continuation_observations"]["latest_status"] == "ready"
    remote_boundary_report = json.loads(
        Path(summary["remote_boundary_report_path"]).read_text(encoding="utf-8")
    )
    assert remote_boundary_report["mission_remote"]["present"] is True
    assert remote_boundary_report["mission_remote"]["latest_next_action"] == "continue"
    assert remote_boundary_report["continuation_observations"]["latest_stage"] == "mission_remote_rehearsal"
    assert remote_boundary_report["continuation_observations"]["latest_status"] == "ready"


def test_pr3_b14_dry_run_pack_cli_print_json_emits_single_payload(tmp_path: Path, monkeypatch):
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    selected_cases = [
        case
        for case in _fixture_cases()
        if case["case_id"] in {"cache_reextraction_path_known", "graph_training_confirmed_mapping"}
    ]
    cases_path = tmp_path / "b14_pack_cases.json"
    cases_path.write_text(json.dumps(selected_cases, ensure_ascii=False, indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-dry-run-pack",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_pack_print_json",
            "--cases-path",
            str(cases_path),
            "--actor",
            "cli-local-only",
            "--print-json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["status"] == "ready"
    assert payload["case_count"] == 2
    assert payload["remote_boundary"] == "not_reached"


def test_pr3_remote_boundary_report_cli_entrypoint_uses_cases_or_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    selected_cases = [case for case in _fixture_cases() if case["case_id"] == "graph_training_confirmed_mapping"]
    cases_path = tmp_path / "b14_boundary_cases.json"
    cases_path.write_text(json.dumps(selected_cases, ensure_ascii=False, indent=2), encoding="utf-8")

    generated = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-remote-boundary-report",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_bound_report",
            "--cases-path",
            str(cases_path),
            "--actor",
            "cli-boundary-local",
        ],
    )
    assert generated.exit_code == 0, generated.output
    generated_payload = json.loads(generated.output)
    assert generated_payload["status"] in {"ready", "blocked"}
    assert generated_payload["proposal_run_id"] == "cli_b14_bound_report"
    assert generated_payload["case_count"] == 1
    assert generated_payload["case_count"] == len(selected_cases)
    assert generated_payload["path"] != ""
    assert Path(generated_payload["path"]).exists()
    assert generated_payload["created"] is True or generated_payload["replayed"] is True
    assert generated_payload["mission_remote"]["present"] is False
    assert generated_payload["continuation_observations"]["count"] == 0

    replay = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-dry-run-pack",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_rehearsal_source",
            "--cases-path",
            str(cases_path),
            "--write-summary",
            "--actor",
            "cli-boundary-local",
        ],
    )
    assert replay.exit_code == 0, replay.output
    rehearsal_payload = json.loads(replay.output)
    summary_path = Path(rehearsal_payload["summary_path"])
    assert summary_path.exists()

    fallback = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-remote-boundary-report",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_rehearsal_source",
            "--actor",
            "cli-boundary-local",
        ],
    )
    assert fallback.exit_code == 0, fallback.output
    fallback_payload = json.loads(fallback.output)
    assert fallback_payload["status"] in {"ready", "blocked"}
    assert fallback_payload["path"] != ""
    assert fallback_payload["case_count"] == 1
    assert fallback_payload["case_count"] == rehearsal_payload["case_count"]
    assert fallback_payload["mission_remote"]["present"] is False
    assert fallback_payload["continuation_observations"]["count"] == 0


def test_pr3_remote_boundary_report_cli_print_json_is_single_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission = load_research_mission()
    selected_cases = [case for case in _fixture_cases() if case["case_id"] == "graph_training_confirmed_mapping"]
    cases_path = tmp_path / "b14_boundary_cases.json"
    cases_path.write_text(json.dumps(selected_cases, ensure_ascii=False, indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "b14-remote-boundary-report",
            "--mission-id",
            mission.mission_id,
            "--proposal-run-id",
            "cli_b14_bound_print_json",
            "--cases-path",
            str(cases_path),
            "--actor",
            "cli-boundary-local",
            "--print-json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["status"] in {"ready", "blocked"}
    assert payload["case_count"] == 1

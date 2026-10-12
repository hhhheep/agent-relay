from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.continuation import project_dialogue


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _b14_knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "project_knowledge_binding": {
            "kind": "remote_project_kb_binding",
            "project_id": "B14_spatial_grace_graph_utility",
            "workplace_profile": "EXAMPLE_WORKER_A_GenD",
            "knowledge_root_ref": "remote_handoff_package_docs",
            "visibility": "local_only",
        },
        "current_state": {
            "summary": "B14 is in a bounded bbox-decoder/calibration stage, not a proposal-audit stage.",
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
            "stage": "bounded_decoder_p0_smoke_done",
        },
        "active_workstreams": [
            {"name": "bounded decoder adapter/audit", "status": "next_safe"},
        ],
        "completed_or_superseded": [
            {
                "item": "Run L6.3-V1 first",
                "status": "superseded",
                "superseded_by": ["later V1/V2/L64/L66 evidence"],
            }
        ],
        "bottlenecks": [
            {
                "issue": "real-negative false-detail evidence",
                "why_blocking": "P2 improves recall but can produce too many real boxes per image.",
                "supported_by": ["L66 calibration report", "V2h2c p_valid summary"],
            }
        ],
        "claim_limits": [
            {"limit": "P2-only is proposal baseline, not final bbox method."},
            {"limit": "Decoder P0 is smoke/interface evidence only."},
        ],
        "next_safe_action": {
            "action": "Run bounded decoder adapter/audit against existing L66 artifacts.",
            "why_safe": "Uses existing artifacts and stops on missing fields.",
        },
        "evidence_refs": [
            {"ref": "agent-facing chronology"},
            {"ref": "L66 mask-to-bbox calibration"},
        ],
        "kb_patches_proposed": [
            {"patch": "superseded_map: mark L6.3-V1-first as historical_context_only"},
        ],
    }


def _discussion_payload() -> dict:
    return {
        "key_uncertainty": "Whether the bounded decoder adapter preserves valid bbox coordinates against the baseline path.",
        "rejected_options": [{"option": "formal claim", "reason": "proxy evidence only"}],
        "if_success_next": "Expand to a bounded validation bucket.",
        "if_failure_next": "Inspect mapping failures and keep the current mainline unchanged.",
        "stop_boundaries": ["formal_claim", "core_source_behavior"],
        "fairness_protocol": {
            "baseline": "existing calibration artifacts without decoder adapter",
            "variant": "bounded decoder adapter/audit against the same artifacts",
            "metrics": ["candidate_count", "coordinate_validity"],
            "input_scope": "same bounded artifact bucket",
            "falsification_test": "variant must not pass by producing invalid boxes",
        },
        "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
    }


def test_project_dialogue_answers_project_not_agent_architecture(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="我們在做什麼，做到什麼程度？難點在哪裡？",
        session_id="b14_status_check",
    )

    assert answer["project_id"] == "B14_spatial_grace_graph_utility"
    assert answer["answer_scope"] == "project_status_and_bottlenecks"
    assert "agent_architecture_discussion" in answer["not_answering"]
    assert answer["current_mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert any("real-negative" in item["issue"] for item in answer["bottlenecks"])


def test_project_dialogue_keeps_remote_binding_as_metadata_only(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="專案狀態？",
    )

    binding = answer["project_knowledge_binding"]
    assert binding["kind"] == "remote_project_kb_binding"
    assert binding["workplace_profile"] == "EXAMPLE_WORKER_A_GenD"
    assert answer["side_effects"] == project_dialogue.FALSE_SIDE_EFFECTS


def test_project_dialogue_mentions_superseded_tasks_and_claim_limits(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="下一步要不要跑舊任務？",
    )

    assert any(item["status"] == "superseded" for item in answer["completed_or_superseded"])
    claim_text = json.dumps(answer["claim_limits"], ensure_ascii=False)
    assert "P2-only" in claim_text
    assert "smoke" in claim_text
    assert answer["next_safe_action"]["action"].startswith("Run bounded decoder")


def test_project_dialogue_embeds_research_progression_gate_from_project_knowledge(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="What is the grounded next step?",
        discussion_payload=_discussion_payload(),
    )

    assert answer["research_progression_path"] == [
        "context_index_or_artifact_index",
        "project_knowledge_bundle",
        "knowledge_first_progression_gate",
        "discussion_completeness",
        "next_action_inventory_or_human_boundary",
    ]
    gate = answer["research_prerequisite_gate"]
    assert gate["status"] == "allow"
    assert gate["chosen_action"] == "continue"
    assert gate["can_continue_to_mission_gate"] is True
    assert answer["stop_or_continue"]["decision"] == "continue_with_next_safe_action"


def test_project_dialogue_routes_missing_source_refs_to_inventory_boundary(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    knowledge = _b14_knowledge()
    knowledge["evidence_refs"] = []
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        knowledge,
        question="Can I execute from this taskbook?",
        discussion_payload=_discussion_payload(),
    )

    gate = answer["research_prerequisite_gate"]
    assert gate["chosen_action"] == "request_inventory"
    assert gate["reason_codes"] == ["missing_evidence"]
    assert answer["stop_or_continue"]["decision"] == "run_inventory_before_execution"
    assert answer["stop_or_continue"]["requires_human"] is False


def test_project_dialogue_routes_missing_fairness_protocol_to_validation_design(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    discussion = _discussion_payload()
    discussion.pop("fairness_protocol")
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="What should happen before execution?",
        discussion_payload=discussion,
    )

    gate = answer["research_prerequisite_gate"]
    assert gate["chosen_action"] == "hold"
    assert gate["reason_codes"] == ["governance_guardrail"]
    assert answer["stop_or_continue"]["decision"] == "design_validation_protocol_or_continue_discussion"
    assert answer["stop_or_continue"]["requires_human"] is False


def test_project_dialogue_routes_formal_claim_or_core_source_behavior_to_human(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    discussion = _discussion_payload()
    discussion["next_action"] = "Make a formal claim and change core pipeline source behavior."
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="Can I escalate this result?",
        discussion_payload=discussion,
    )

    gate = answer["research_prerequisite_gate"]
    assert gate["chosen_action"] == "request_human_boundary"
    assert gate["reason_codes"] == ["claim_upgrade_requested"]
    assert answer["stop_or_continue"]["decision"] == "ask_human"
    assert answer["stop_or_continue"]["requires_human"] is True


def test_project_dialogue_embeds_project_understanding_protocol_and_authority_resolution(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="我們在做什麼，做到什麼程度？難點在哪裡？",
    )

    protocol = answer["understanding_protocol"]
    assert protocol["protocol_id"] == "project_understanding_protocol_v0"
    assert protocol["core_rule"] == "taskbook_is_seed_not_line_state"
    assert protocol["operating_order"] == [
        "load_scope",
        "resolve_authority_and_chronology",
        "reconstruct_current_state",
        "classify_blockers",
        "choose_next_safe_action",
        "route_repair_or_continuation",
        "stop_or_continue",
    ]

    authority = answer["authority_resolution"]
    assert authority["taskbook_role"] == "seed_only"
    assert authority["logs_role"] == "evidence_not_instructions"
    assert "Run L6.3-V1 first" in authority["superseded_instruction_flags"]


def test_project_dialogue_protocol_metadata_uses_required_fields_from_shared_protocol() -> None:
    protocol = yaml.safe_load(
        Path(".solution/continuation/project_understanding_protocol.yaml").read_text(encoding="utf-8")
    )
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        {"project_id": "protocol_metadata_smoke"},
        question="Check protocol metadata propagation.",
    )

    metadata = answer["understanding_protocol"]
    assert metadata["core_rule_description"] == str(protocol["core_rule"]).strip()
    assert metadata["protocol_id"] == protocol["protocol_id"]


def test_project_dialogue_explains_why_not_other_options_and_continue_decision(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    answer = project_dialogue.build_project_understanding_answer_dry_run(
        _b14_knowledge(),
        question="下一步是什麼？",
        discussion_payload=_discussion_payload(),
    )

    rejected = json.dumps(answer["why_not_other_options"], ensure_ascii=False)
    assert "Run L6.3-V1 first" in rejected
    assert "overclaim_current_result" in rejected
    assert answer["stop_or_continue"]["decision"] == "continue_with_next_safe_action"
    assert answer["stop_or_continue"]["requires_human"] is False


def test_write_project_dialogue_session_is_local_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    session = project_dialogue.build_project_dialogue_session_dry_run(
        _b14_knowledge(),
        question="專案現在到哪？",
        session_id="b14_project_dialogue_001",
    )
    written = project_dialogue.write_project_dialogue_session_dry_run(session)

    session_dir = Path(written["session_dir"])
    assert written["created"] is True
    assert session_dir == tmp_path / "continuation" / "project_dialogue" / "sessions" / "b14_project_dialogue_001"
    assert (session_dir / "session.json").exists()
    assert (session_dir / "project_understanding_answer.json").exists()
    assert not (tmp_path / "tasks" / "inbox").exists()

    replay = project_dialogue.write_project_dialogue_session_dry_run(session)
    assert replay["created"] is False
    assert replay["replayed"] is True


def test_project_dialogue_contract_files_present() -> None:
    policy = yaml.safe_load(Path(".solution/continuation/project_dialogue_policy.yaml").read_text(encoding="utf-8"))
    schema = yaml.safe_load(Path(".solution/continuation/project_dialogue_schema.yaml").read_text(encoding="utf-8"))
    ledger = yaml.safe_load(Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8"))

    assert "state_summary" in policy["required_answer_fields"]
    assert "understanding_protocol" in policy["required_answer_fields"]
    assert "authority_resolution" in policy["required_answer_fields"]
    assert "research_progression_path" in policy["required_answer_fields"]
    assert "research_prerequisite_gate" in policy["required_answer_fields"]
    assert "why_not_other_options" in schema["project_understanding_answer"]["required"]
    assert "stop_or_continue" in schema["project_understanding_answer"]["required"]
    assert "bottlenecks" in schema["project_understanding_answer"]["required"]
    assert "project_dialogue" in ledger["allowed_stages"]
    assert policy["input_binding"]["remote_binding_is_metadata_only"] is True


def test_project_dialogue_no_subprocess_or_network_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    with (
        patch("solution_agent.continuation.project_dialogue.subprocess.run") as run_mock,
        patch("solution_agent.continuation.project_dialogue.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.project_dialogue.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.project_dialogue.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        session = project_dialogue.build_project_dialogue_session_dry_run(
            _b14_knowledge(),
            question="狀態和難點？",
            session_id="b14_no_network",
        )
        project_dialogue.write_project_dialogue_session_dry_run(session)

    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_project_dialogue_cli_answer_no_write(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    knowledge_path = tmp_path / "b14_project_knowledge.json"
    knowledge_path.write_text(json.dumps(_b14_knowledge(), ensure_ascii=False), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "project-dialogue",
            "answer",
            "--knowledge-json",
            str(knowledge_path),
            "--question",
            "我們在做什麼，難點在哪裡？",
            "--session-id",
            "b14_cli_dialogue",
            "--no-write",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project_id"] == "B14_spatial_grace_graph_utility"
    assert payload["current_mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert not (tmp_path / "continuation" / "project_dialogue").exists()

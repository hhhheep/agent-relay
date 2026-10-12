from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from solution_agent.cli import cli
from solution_agent.continuation import research_deliberation


def _contract_root() -> Path:
    return Path(__file__).resolve().parents[1] / ".solution" / "continuation"


def _load_yaml(filename: str) -> dict:
    payload = yaml.safe_load((_contract_root() / filename).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_deliberation_policy_defines_meeting_not_role_pipeline() -> None:
    policy = _load_yaml("research_deliberation_session_policy.yaml")

    assert policy["schema_version"] == 1
    assert policy["mode"] == "advisory_only"
    assert "role_pipeline" in policy["non_goals"]
    assert "same_question_for_all_participants" in policy["principles"]
    assert "independent_complete_opinions_before_cross_review" in policy["principles"]
    assert "cross_review_must_reference_other_participants" in policy["principles"]

    participant_rounds = {
        participant["participant_id"]: set(participant["required_rounds"])
        for participant in policy["default_participants"]
    }
    assert participant_rounds["qwen_flash"] >= {
        "round_1_independent_opinion",
        "round_2_cross_review",
    }
    assert participant_rounds["deepseek_v4_pro"] >= {
        "round_1_independent_opinion",
        "round_2_cross_review",
        "round_3_synthesis",
    }


def test_deliberation_rounds_are_discussion_protocol_not_pipeline() -> None:
    policy = _load_yaml("research_deliberation_session_policy.yaml")
    rounds = {round_def["round_id"]: round_def for round_def in policy["rounds"]}

    assert rounds["round_0_session_brief"]["api_allowed"] is False
    assert rounds["round_1_independent_opinions"]["purpose"] == "every_participant_answers_the_same_question"
    assert rounds["round_2_cross_review"]["purpose"] == "each_participant_reads_and_challenges_other_opinions"
    assert rounds["round_4_deterministic_gate"]["type"] == "deterministic_validator"
    assert rounds["round_4_deterministic_gate"]["api_allowed"] is False

    assert policy["limits"]["max_questions_per_session"] == 1
    assert policy["output_boundary"]["direct_task_generation_allowed"] is False
    assert policy["output_boundary"]["direct_dispatch_allowed"] is False


def test_deliberation_schema_requires_same_question_cross_review_and_rejected_options() -> None:
    schema = _load_yaml("research_deliberation_session_schema.yaml")

    independent_required = set(schema["independent_opinion"]["required_fields"])
    assert {
        "research_question",
        "position",
        "evidence_used",
        "recommended_next_step",
        "what_not_to_do",
    } <= independent_required
    assert "complete_opinion_not_role_fragment" in schema["independent_opinion"]["invariants"]
    assert "research_question_must_match_session_brief" in schema["independent_opinion"]["invariants"]

    cross_required = set(schema["cross_review"]["required_fields"])
    assert {"agrees_with", "disagrees_with", "revised_position"} <= cross_required
    assert "must_reference_at_least_one_other_participant" in schema["cross_review"]["invariants"]

    synthesis_required = set(schema["synthesis"]["required_fields"])
    assert {"consensus", "unresolved_disagreements", "rejected_options"} <= synthesis_required
    assert "rejected_options_must_have_reasons" in schema["synthesis"]["invariants"]


def test_deliberation_output_is_advisory_and_side_effect_free() -> None:
    policy = _load_yaml("research_deliberation_session_policy.yaml")
    schema = _load_yaml("research_deliberation_session_schema.yaml")

    forbidden = set(policy["forbidden_actions"])
    assert {
        "write_inbox",
        "promote",
        "dispatch",
        "scheduler_submit",
        "registry_mutation",
        "remote_execution",
        "source_edit",
        "path_guessing",
        "active_inbox_write",
        "direct_task_generation",
    } <= forbidden

    side_effects = schema["next_direction_decision"]["forbidden_side_effects"]
    assert all(value is False for value in side_effects.values())
    assert schema["next_direction_decision"]["required_values"]["advisory_only"] is True


def _brief() -> dict:
    return {
        "session_id": "delib_test_001",
        "research_question": "Should the A-line residual correction continue with a bounded metric completion?",
        "source_context_refs": ["result_interpretation:a_rescorr_014"],
        "allowed_scope": {"line": "A_residual_correction"},
        "forbidden_actions": ["dispatch", "promote", "source_edit"],
        "knowledge_pack_excerpt": {"status": "synthetic_test_pack"},
    }


def test_mock_deliberation_session_runs_same_question_discussion() -> None:
    session = research_deliberation.build_mock_research_deliberation_session(_brief())

    assert session["status"] == "valid"
    assert session["advisory_only"] is True
    assert session["api_call_executed"] is False
    assert session["network_enabled"] is False
    assert all(value is False for value in session["side_effects"].values())

    question = session["research_question"]
    artifacts = session["artifacts"]
    round_1 = artifacts["round_1_independent_opinions"]
    round_2 = artifacts["round_2_cross_review"]
    synthesis = artifacts["round_3_synthesis"]

    assert set(round_1) == {"qwen_flash", "deepseek_v4_pro"}
    assert all(opinion["research_question"] == question for opinion in round_1.values())
    assert all(review["research_question"] == question for review in round_2.values())
    assert synthesis["research_question"] == question
    assert artifacts["gate_report"]["status"] == "pass"
    assert artifacts["next_direction_decision"]["advisory_only"] is True


def test_mock_deliberation_cross_review_references_other_participants() -> None:
    session = research_deliberation.build_mock_research_deliberation_session(_brief())
    round_2 = session["artifacts"]["round_2_cross_review"]

    assert "deepseek_v4_pro" in str(round_2["qwen_flash"]["agrees_with"]).lower()
    assert "qwen_flash" in str(round_2["deepseek_v4_pro"]["agrees_with"]).lower()


def test_mock_deliberation_blocks_role_fragment_question_mismatch() -> None:
    bad_opinions = {
        "qwen_flash": {
            "participant_id": "qwen_flash",
            "research_question": "Only check evidence.",
            "position": "Evidence looks weak.",
            "evidence_used": ["summary"],
            "uncertainty": [],
            "recommended_next_step": "bounded continuation",
            "what_not_to_do": ["dispatch"],
            "human_decision_needed": [],
        }
    }

    session = research_deliberation.build_mock_research_deliberation_session(
        _brief(),
        independent_opinions=bad_opinions,
    )

    assert session["status"] == "blocked"
    assert "round_1_question_mismatch:qwen_flash" in session["reasons"]
    assert session["artifacts"]["next_direction_decision"]["proposed_continuation_candidates"] == []


def test_mock_deliberation_blocks_cross_review_without_other_reference() -> None:
    bad_reviews = {
        "qwen_flash": {
            "participant_id": "qwen_flash",
            "research_question": _brief()["research_question"],
            "agrees_with": [],
            "disagrees_with": [],
            "missed_evidence_or_risk": [],
            "revised_position": "No change.",
        }
    }

    session = research_deliberation.build_mock_research_deliberation_session(
        _brief(),
        cross_reviews=bad_reviews,
    )

    assert session["status"] == "blocked"
    assert "round_2_missing_other_participant_reference:qwen_flash" in session["reasons"]


def test_mock_deliberation_blocks_synthesis_without_rejected_option_reason() -> None:
    bad_synthesis = {
        "moderator_id": "deepseek_v4_pro",
        "session_id": "delib_test_001",
        "research_question": _brief()["research_question"],
        "consensus": ["bounded only"],
        "unresolved_disagreements": [],
        "recommended_next_direction": {"decision": "bounded_probe", "reason": "low risk"},
        "rejected_options": [{"option": "direct_launch"}],
        "human_decision_needed": [],
        "proposed_continuation_candidates": [],
    }

    session = research_deliberation.build_mock_research_deliberation_session(
        _brief(),
        synthesis=bad_synthesis,
    )

    assert session["status"] == "blocked"
    assert "synthesis_rejected_option_reason_required:0" in session["reasons"]


def test_write_deliberation_session_artifacts_is_local_and_manifested(tmp_path: Path) -> None:
    session = research_deliberation.build_mock_research_deliberation_session(_brief())
    output_dir = tmp_path / "continuation" / "research_sessions" / session["session_id"]

    result = research_deliberation.write_research_deliberation_session_artifacts(
        session,
        output_dir=output_dir,
    )

    assert result["status"] == "written"
    assert Path(result["output_dir"]) == output_dir
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False
    assert all(value is False for value in result["side_effects"].values())

    expected_files = {
        "session_brief.json",
        "knowledge_pack_excerpt.json",
        "round_1_independent_opinions.json",
        "round_2_cross_review.json",
        "round_3_synthesis.json",
        "disagreement_matrix.json",
        "next_direction_decision.json",
        "gate_report.json",
        "council_transcript.md",
        "manifest.json",
    }
    assert {path.name for path in output_dir.iterdir()} == expected_files

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == session["session_id"]
    assert manifest["advisory_only"] is True
    assert manifest["api_call_executed"] is False
    assert manifest["network_enabled"] is False
    assert "tasks/inbox" not in json.dumps(manifest, sort_keys=True)

    transcript = (output_dir / "council_transcript.md").read_text(encoding="utf-8")
    assert "Round 1 Independent Opinions" in transcript
    assert "Round 2 Cross Review" in transcript
    assert "Round 3 Synthesis" in transcript


def test_research_deliberation_mock_session_cli_writes_session_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "mock-session",
            "--session-id",
            "cli_delib_001",
            "--question",
            "Should the next continuation be a bounded metric completion?",
            "--line",
            "A_residual_correction",
            "--source-ref",
            "result_interpretation:a_rescorr_014",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    output_dir = tmp_path / "continuation" / "research_sessions" / "cli_delib_001"
    assert Path(payload["output_dir"]) == output_dir
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "council_transcript.md").exists()
    assert "tasks/inbox" not in result.output
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False


def test_research_deliberation_pre_live_cli_persists_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "pre-live-check",
            "--session-id",
            "cli_pre_live_001",
            "--question",
            "Should the next continuation be a bounded metric completion?",
            "--line",
            "A_residual_correction",
            "--source-ref",
            "result_interpretation:a_rescorr_014",
            "--explicit-live-flag",
            "--eval-gate-pass",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "preflight_passed"
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False

    session_dir = tmp_path / "continuation" / "research_sessions" / "cli_pre_live_001"
    preflight_dir = session_dir / "live_preflight"
    assert (session_dir / "manifest.json").exists()
    assert (preflight_dir / "manifest.json").exists()
    assert (preflight_dir / "requests.json").exists()
    assert "tasks/inbox" not in result.output


def test_research_deliberation_prepare_live_calls_cli_stops_before_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "prepare-live-calls",
            "--session-id",
            "cli_prepare_live_calls_001",
            "--question",
            "Should the next continuation be a bounded metric completion?",
            "--line",
            "A_residual_correction",
            "--source-ref",
            "result_interpretation:a_rescorr_014",
            "--explicit-live-flag",
            "--eval-gate-pass",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False

    session_dir = tmp_path / "continuation" / "research_sessions" / "cli_prepare_live_calls_001"
    live_calls_dir = session_dir / "live_calls"
    assert (live_calls_dir / "manifest.json").exists()
    assert (live_calls_dir / "call_plan.json").exists()
    assert payload["live_call_quarantine"]["call_count"] == 5
    assert "tasks/inbox" not in result.output
    assert "api_call_executed\": true" not in result.output
    assert "network_enabled\": true" not in result.output


def test_deliberation_api_schema_contract_exists() -> None:
    schema = _load_yaml("research_deliberation_api_schema.yaml")

    assert schema["schema_version"] == 1
    request_required = set(schema["request"]["required_fields"])
    assert {
        "request_id",
        "round_id",
        "participant_id",
        "provider_model",
        "research_question",
        "trusted_context",
        "untrusted_context",
        "side_effects",
    } <= request_required
    assert schema["request"]["required_side_effects"]["dispatch"] is False
    assert schema["request"]["required_side_effects"]["remote_execution"] is False
    assert "round_1_independent_opinion" in schema["response"]["round_required_fields"]
    assert "round_2_cross_review" in schema["response"]["round_required_fields"]
    assert "round_3_synthesis" in schema["response"]["round_required_fields"]


def test_build_deliberation_api_request_redacts_secrets_and_stays_dry_run() -> None:
    brief = _brief()
    brief["api_key"] = "sk-secret-value"
    brief["nested"] = {"token": "hidden-token", "safe": "visible"}

    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        brief,
        round_id="round_1_independent_opinion",
        participant_id="qwen_flash",
        provider_model="qwen-flash",
        prior_rounds={"password": "do-not-send", "summary": "safe"},
    )

    request_text = json.dumps(request, sort_keys=True)
    assert request["status"] == "request_created"
    assert request["api_call_executed"] is False
    assert request["network_enabled"] is False
    assert all(value is False for value in request["side_effects"].values())
    assert "sk-secret-value" not in request_text
    assert "hidden-token" not in request_text
    assert "do-not-send" not in request_text
    assert request["trusted_context"]["round_required_fields"]


def test_validate_deliberation_round_1_response_accepts_complete_same_question() -> None:
    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        _brief(),
        round_id="round_1_independent_opinion",
        participant_id="qwen_flash",
        provider_model="qwen-flash",
    )
    response = {
        "participant_id": "qwen_flash",
        "research_question": _brief()["research_question"],
        "position": "A bounded metric completion is reasonable.",
        "evidence_used": ["source result", "knowledge pack"],
        "uncertainty": ["mock response"],
        "recommended_next_step": "bounded metric completion",
        "what_not_to_do": ["avoid active execution"],
        "human_decision_needed": [],
    }

    validated = research_deliberation.validate_research_deliberation_api_response(request, response)

    assert validated["status"] == "valid"
    assert validated["reasons"] == []
    assert validated["api_call_executed"] is False
    assert validated["network_enabled"] is False


def test_validate_deliberation_round_1_rejects_scalar_list_fields() -> None:
    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        _brief(),
        round_id="round_1_independent_opinion",
        participant_id="qwen_flash",
        provider_model="qwen-flash",
    )
    response = {
        "participant_id": "qwen_flash",
        "research_question": _brief()["research_question"],
        "position": "A bounded metric completion is reasonable.",
        "evidence_used": ["source result"],
        "uncertainty": "moderate",
        "recommended_next_step": "bounded metric completion",
        "what_not_to_do": "avoid broad claims",
        "human_decision_needed": False,
    }

    validated = research_deliberation.validate_research_deliberation_api_response(request, response)

    assert validated["status"] == "rejected"
    assert "field_must_be_list:uncertainty" in validated["reasons"]
    assert "field_must_be_list:what_not_to_do" in validated["reasons"]
    assert "field_must_be_list:human_decision_needed" in validated["reasons"]


def test_validate_deliberation_round_2_requires_reference_to_other_participant() -> None:
    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        _brief(),
        round_id="round_2_cross_review",
        participant_id="qwen_flash",
        provider_model="qwen-flash",
        prior_rounds={"deepseek_v4_pro": {"position": "bounded"}},
    )
    response = {
        "participant_id": "qwen_flash",
        "research_question": _brief()["research_question"],
        "agrees_with": [],
        "disagrees_with": [],
        "missed_evidence_or_risk": [],
        "revised_position": "No change.",
    }

    validated = research_deliberation.validate_research_deliberation_api_response(request, response)

    assert validated["status"] == "rejected"
    assert "round_2_missing_other_participant_reference:qwen_flash" in validated["reasons"]


def test_validate_deliberation_synthesis_rejects_unsafe_or_unreasoned_output() -> None:
    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        _brief(),
        round_id="round_3_synthesis",
        participant_id="deepseek_v4_pro",
        provider_model="deepseek-v4-pro",
    )
    response = {
        "participant_id": "deepseek_v4_pro",
        "research_question": _brief()["research_question"],
        "consensus": ["bounded"],
        "unresolved_disagreements": [],
        "recommended_next_direction": {"decision": "bounded_probe", "reason": "safe"},
        "rejected_options": [{"option": "active launch"}],
        "human_decision_needed": [],
        "proposed_continuation_candidates": [{"kind": "bounded_probe"}],
        "note": "dispatch now",
    }

    validated = research_deliberation.validate_research_deliberation_api_response(request, response)

    assert validated["status"] == "rejected"
    assert "synthesis_rejected_option_reason_required:0" in validated["reasons"]
    assert any(reason.startswith("forbidden_text:") for reason in validated["reasons"])
    assert validated["response"]["proposed_continuation_candidates"] == []


def test_execute_mock_transcript_builds_valid_discussion_without_api_calls() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())

    assert session["status"] == "valid"
    assert session["mode"] == "mock_research_deliberation_transcript"
    assert session["api_call_executed"] is False
    assert session["network_enabled"] is False
    assert all(value is False for value in session["side_effects"].values())
    assert session["transcript_adapter"]["request_count"] == 5
    assert session["transcript_adapter"]["validated_response_count"] == 5
    assert len(session["api_requests"]) == 5
    assert len(session["validated_api_responses"]) == 5
    assert all(item["status"] == "valid" for item in session["validated_api_responses"])

    artifacts = session["artifacts"]
    assert set(artifacts["round_1_independent_opinions"]) == {"qwen_flash", "deepseek_v4_pro"}
    assert set(artifacts["round_2_cross_review"]) == {"qwen_flash", "deepseek_v4_pro"}
    assert artifacts["round_3_synthesis"]["participant_id"] == "deepseek_v4_pro"
    assert artifacts["gate_report"]["status"] == "pass"


def _live_eval_gate_pass() -> dict:
    return {
        "status": "pass",
        "passed_cases": [
            "prompt_injection_artifact",
            "missing_artifact_path_guessing",
            "source_code_required_routing",
            "reward_hack_risk",
            "same_question_round_1",
            "cross_review_round_2",
            "synthesis_rejected_options",
        ],
    }


def _ready_live_call_dir(tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )
    quarantine = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )
    return quarantine, Path(quarantine["written_calls"][0]["quarantine_dir"])


def test_live_provider_policy_and_schema_contracts_exist() -> None:
    policy = _load_yaml("research_deliberation_live_provider_policy.yaml")
    schema = _load_yaml("research_deliberation_live_provider_schema.yaml")

    assert policy["schema_version"] == 1
    assert policy["default_live_enabled"] is False
    assert "explicit_live_flag" in policy["preflight_required"]
    assert policy["allowed_provider_models"]["deepseek-v4-pro"]["final_gate_allowed"] is False
    assert "round_4_deterministic_gate" not in policy["allowed_provider_models"]["deepseek-v4-pro"]["allowed_rounds"]
    assert schema["preflight"]["required_side_effects"]["dispatch"] is False
    assert schema["preflight"]["required_side_effects"]["remote_execution"] is False


def test_live_provider_preflight_blocks_without_explicit_flag() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())

    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=False,
        redaction_report={"status": "pass", "secret_like_material_found": False},
        eval_gate_report=_live_eval_gate_pass(),
    )

    assert preflight["status"] == "blocked"
    assert "explicit_live_flag_required" in preflight["reasons"]
    assert preflight["api_call_executed"] is False
    assert preflight["network_enabled"] is False


def test_live_provider_preflight_passes_only_with_redaction_eval_and_allowlist() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())

    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )

    assert preflight["status"] == "preflight_passed"
    assert preflight["reasons"] == []
    assert all(item["status"] == "passed" for item in preflight["checked_requests"])
    assert all(value is False for value in preflight["side_effects"].values())


def test_live_provider_preflight_blocks_missing_eval_cases_and_unknown_provider() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    requests = [dict(item) for item in session["api_requests"]]
    requests[0]["provider_model"] = "unknown-model"

    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        requests,
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report={"status": "pass", "passed_cases": ["same_question_round_1"]},
    )

    assert preflight["status"] == "blocked"
    assert any(reason.startswith("provider_model_not_allowlisted") for reason in preflight["reasons"])
    assert any(reason.startswith("eval_gate_missing_cases") for reason in preflight["reasons"])


def test_live_provider_preflight_blocks_final_gate_provider_request() -> None:
    request = research_deliberation.build_research_deliberation_api_request_dry_run(
        _brief(),
        round_id="round_1_independent_opinion",
        participant_id="deepseek_v4_pro",
        provider_model="deepseek-v4-pro",
    )
    request["round_id"] = "round_4_deterministic_gate"

    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        [request],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )

    assert preflight["status"] == "blocked"
    assert "final_gate_must_not_use_provider" in preflight["reasons"]


def test_write_live_preflight_artifacts_are_local_and_auditable(tmp_path: Path) -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    redaction_report = {"status": "pass", "secret_like_material_found": False, "findings": []}
    eval_gate_report = _live_eval_gate_pass()
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
    )
    output_dir = tmp_path / "continuation" / "research_sessions" / session["session_id"] / "live_preflight"

    result = research_deliberation.write_research_deliberation_live_preflight_artifacts(
        session_id=session["session_id"],
        requests=session["api_requests"],
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
        preflight=preflight,
        output_dir=output_dir,
    )

    assert result["status"] == "written"
    assert Path(result["output_dir"]) == output_dir
    assert {path.name for path in output_dir.iterdir()} == {
        "requests.json",
        "redaction_report.json",
        "eval_gate_report.json",
        "preflight.json",
        "manifest.json",
    }
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "preflight_passed"
    assert manifest["api_call_executed"] is False
    assert manifest["network_enabled"] is False
    assert all(value is False for value in manifest["side_effects"].values())
    assert "tasks/inbox" not in json.dumps(manifest, sort_keys=True)
    assert json.loads((output_dir / "requests.json").read_text(encoding="utf-8")) == session["api_requests"]


def test_provider_client_policy_and_schema_contracts_exist() -> None:
    policy = _load_yaml("research_deliberation_provider_client_policy.yaml")
    schema = _load_yaml("research_deliberation_provider_client_schema.yaml")

    assert policy["schema_version"] == 1
    assert policy["client_isolation"]["no_shared_global_state"] is True
    assert policy["client_isolation"]["response_quarantine_required"] is True
    assert policy["timeouts"]["retry_count"] == 0
    assert "request.json" in policy["quarantine"]["required_files"]
    assert schema["call_plan"]["required_side_effects"]["dispatch"] is False
    assert schema["call_plan"]["required_side_effects"]["network"] is False


def test_research_deliberation_provider_allowlist_contract_exists() -> None:
    allowlist = _load_yaml("research_deliberation_provider_allowlist.yaml")

    assert allowlist["schema_version"] == 1
    allowed = allowlist["allowed_models"]
    assert allowed["qwen-flash"]["api_key_env"] == "DASHSCOPE_API_KEY"
    assert allowed["deepseek-v4-pro"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert allowed["deepseek-v4-pro"]["base_url"] == "https://api.deepseek.com"
    assert "round_3_synthesis" in allowed["deepseek-v4-pro"]["allowed_rounds"]
    assert allowlist["forbidden"]["store_api_key_in_repo"] is True


def test_provider_call_plan_ready_after_preflight_without_network() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )

    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )

    assert plan["status"] == "ready_for_manual_live_call"
    assert plan["call_count"] == 5
    assert plan["api_call_executed"] is False
    assert plan["network_enabled"] is False
    assert all(value is False for value in plan["side_effects"].values())
    assert all(call["status"] == "ready" for call in plan["call_manifests"])
    assert all("live_calls" in call["quarantine_dir"] for call in plan["call_manifests"])


def test_provider_call_plan_blocks_without_passed_preflight_or_bad_timeout() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    blocked_preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=False,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )

    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        blocked_preflight,
        session["api_requests"],
        timeout_seconds=999,
        retry_count=1,
    )

    assert plan["status"] == "blocked"
    assert "preflight_not_passed" in plan["reasons"]
    assert "timeout_seconds_out_of_range" in plan["reasons"]
    assert "retry_count_must_be_zero_before_live_smoke" in plan["reasons"]


def test_provider_payload_builder_is_openai_compatible_and_local_only() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    request = dict(session["api_requests"][0])
    request["api_key"] = "sk-do-not-leak"
    request["nested"] = {"token": "hidden-token-value", "safe": "visible"}

    payload = research_deliberation.build_research_deliberation_provider_payload_dry_run(request)

    payload_text = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "payload_ready"
    assert payload["payload_type"] == "openai_compatible_chat_completions"
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False
    assert all(value is False for value in payload["side_effects"].values())
    assert payload["payload"]["response_format"] == {"type": "json_object"}
    assert payload["payload"]["messages"][0]["role"] == "system"
    assert payload["payload"]["messages"][1]["role"] == "user"
    assert "sk-do-not-leak" not in payload_text
    assert "hidden-token-value" not in payload_text


def test_provider_payload_builder_blocks_unknown_provider_or_bad_parameters() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    request = dict(session["api_requests"][0])
    request["provider_model"] = "unknown-model"

    payload = research_deliberation.build_research_deliberation_provider_payload_dry_run(
        request,
        temperature=5,
        max_tokens=99999,
    )

    assert payload["status"] == "blocked"
    assert any(reason.startswith("provider_model_not_allowlisted") for reason in payload["reasons"])
    assert "temperature_out_of_range" in payload["reasons"]
    assert "max_tokens_out_of_range" in payload["reasons"]
    assert payload["payload"] == {}
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False


def test_live_call_quarantine_skeleton_writes_placeholders_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )

    result = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )

    assert result["status"] == "written"
    assert result["call_count"] == 5
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False
    assert all(value is False for value in result["side_effects"].values())
    assert Path(result["manifest"]).exists()

    live_calls_dir = tmp_path / "continuation" / "research_sessions" / session["session_id"] / "live_calls"
    top_manifest = json.loads((live_calls_dir / "manifest.json").read_text(encoding="utf-8"))
    assert top_manifest["status"] == "written"
    assert top_manifest["api_call_executed"] is False
    assert top_manifest["network_enabled"] is False
    assert "tasks/inbox" not in json.dumps(top_manifest, sort_keys=True)
    assert (live_calls_dir / "call_plan.json").exists()

    for call in result["written_calls"]:
        assert call["status"] == "quarantined"
        call_dir = Path(call["quarantine_dir"])
        assert call_dir.is_dir()
        assert {path.name for path in call_dir.iterdir()} == {
            "request.json",
            "provider_payload.json",
            "response_raw.json",
            "response_validated.json",
            "call_manifest.json",
        }
        provider_payload = json.loads((call_dir / "provider_payload.json").read_text(encoding="utf-8"))
        raw = json.loads((call_dir / "response_raw.json").read_text(encoding="utf-8"))
        validated = json.loads((call_dir / "response_validated.json").read_text(encoding="utf-8"))
        assert provider_payload["status"] == "payload_ready"
        assert provider_payload["api_call_executed"] is False
        assert provider_payload["network_enabled"] is False
        assert raw["status"] == "awaiting_manual_live_call"
        assert raw["provider_response"] is None
        assert raw["api_call_executed"] is False
        assert validated["status"] == "not_validated"
        assert "awaiting_manual_live_call" in validated["reasons"]


def test_live_call_quarantine_skeleton_fails_closed_when_plan_not_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    blocked_preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=False,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    blocked_plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        blocked_preflight,
        session["api_requests"],
    )

    result = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        blocked_plan,
        session["api_requests"],
    )

    assert result["status"] == "blocked"
    assert "call_plan_not_ready" in result["reasons"]
    assert result["written_calls"] == []
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False
    assert not (tmp_path / "continuation" / "research_sessions" / session["session_id"] / "live_calls").exists()


def test_live_response_quarantine_validates_manual_openai_compatible_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )
    quarantine = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )
    first_call = quarantine["written_calls"][0]
    call_dir = Path(first_call["quarantine_dir"])
    valid_response = {
        "participant_id": first_call["participant_id"],
        "research_question": _brief()["research_question"],
        "position": "A bounded metric completion is appropriate.",
        "evidence_used": ["manual test response", "knowledge pack"],
        "uncertainty": ["live response manually injected in test"],
        "recommended_next_step": "bounded metric completion",
        "what_not_to_do": ["avoid remote launch"],
        "human_decision_needed": [],
    }
    raw = {
        "schema_version": 1,
        "request_id": first_call["request_id"],
        "status": "manual_response_imported",
        "provider_response": {
            "choices": [
                {"message": {"content": json.dumps(valid_response)}}
            ]
        },
        "api_call_executed": False,
        "network_enabled": False,
    }
    (call_dir / "response_raw.json").write_text(json.dumps(raw), encoding="utf-8")

    result = research_deliberation.validate_research_deliberation_live_response_quarantine(call_dir)

    assert result["status"] == "valid"
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False
    saved = json.loads((call_dir / "response_validated.json").read_text(encoding="utf-8"))
    assert saved["status"] == "valid"
    assert saved["validation"]["response"]["recommended_next_step"] == "bounded metric completion"


def test_live_response_quarantine_coerces_scalar_list_fields_with_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )
    quarantine = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )
    first_call = quarantine["written_calls"][0]
    call_dir = Path(first_call["quarantine_dir"])
    raw = {
        "schema_version": 1,
        "request_id": first_call["request_id"],
        "status": "manual_response_imported",
        "provider_response": {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "participant_id": first_call["participant_id"],
                                "research_question": _brief()["research_question"],
                                "position": "A bounded metric completion is appropriate.",
                                "evidence_used": ["manual test response"],
                                "uncertainty": "moderate",
                                "recommended_next_step": "bounded metric completion",
                                "what_not_to_do": "avoid broad claims",
                                "human_decision_needed": False,
                            }
                        )
                    }
                }
            ]
        },
        "api_call_executed": False,
        "network_enabled": False,
    }
    (call_dir / "response_raw.json").write_text(json.dumps(raw), encoding="utf-8")

    result = research_deliberation.validate_research_deliberation_live_response_quarantine(call_dir)

    assert result["status"] == "valid"
    assert "coerced_scalar_to_list:uncertainty" in result["normalization_warnings"]
    assert "coerced_scalar_to_list:what_not_to_do" in result["normalization_warnings"]
    assert "coerced_scalar_to_list:human_decision_needed" in result["normalization_warnings"]
    assert result["validation"]["response"]["uncertainty"] == ["moderate"]
    assert result["validation"]["response"]["human_decision_needed"] == [False]


def test_live_response_quarantine_keeps_awaiting_response_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )
    quarantine = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )
    call_dir = Path(quarantine["written_calls"][0]["quarantine_dir"])

    result = research_deliberation.validate_research_deliberation_live_response_quarantine(call_dir)

    assert result["status"] == "pending_manual_live_response"
    assert "awaiting_manual_live_call" in result["reasons"]
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False


def test_validate_live_response_cli_reads_quarantine_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=True,
        redaction_report={"status": "pass", "secret_like_material_found": False, "findings": []},
        eval_gate_report=_live_eval_gate_pass(),
    )
    plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
    )
    quarantine = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        plan,
        session["api_requests"],
    )
    call_dir = Path(quarantine["written_calls"][0]["quarantine_dir"])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "validate-live-response",
            "--call-dir",
            str(call_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "pending_manual_live_response"
    assert payload["api_call_executed"] is False
    assert payload["network_enabled"] is False


def test_live_call_blocks_without_explicit_execute_flag(tmp_path: Path, monkeypatch) -> None:
    _, call_dir = _ready_live_call_dir(tmp_path, monkeypatch)

    result = research_deliberation.execute_research_deliberation_live_call(
        call_dir,
        execute_live=False,
    )

    assert result["status"] == "blocked"
    assert "execute_live_flag_required" in result["reasons"]
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False


def test_live_call_with_fake_client_writes_raw_and_validated_response(tmp_path: Path, monkeypatch) -> None:
    _, call_dir = _ready_live_call_dir(tmp_path, monkeypatch)

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def complete(self, messages):
            assert self.config.api_key_env in {"DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"}
            assert messages
            return json.dumps(
                {
                    "participant_id": "qwen_flash",
                    "research_question": _brief()["research_question"],
                    "position": "A bounded metric completion is appropriate.",
                    "evidence_used": ["fake live client"],
                    "uncertainty": ["unit test fake client"],
                    "recommended_next_step": "bounded metric completion",
                    "what_not_to_do": ["keep the output advisory only"],
                    "human_decision_needed": [],
                }
            )

    result = research_deliberation.execute_research_deliberation_live_call(
        call_dir,
        execute_live=True,
        client_factory=FakeClient,
    )

    assert result["status"] == "live_call_completed"
    assert result["api_call_executed"] is True
    assert result["network_enabled"] is True
    raw = json.loads((call_dir / "response_raw.json").read_text(encoding="utf-8"))
    validated = json.loads((call_dir / "response_validated.json").read_text(encoding="utf-8"))
    assert raw["status"] == "provider_response_received"
    assert validated["status"] == "valid"
    assert validated["validation"]["response"]["recommended_next_step"] == "bounded metric completion"


def test_live_call_cli_requires_execute_live_flag(tmp_path: Path, monkeypatch) -> None:
    _, call_dir = _ready_live_call_dir(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "live-call",
            "--call-dir",
            str(call_dir),
        ],
    )

    assert result.exit_code != 0
    assert "execute_live_flag_required" in result.output
    assert "api_call_executed\": false" in result.output


def test_provider_connectivity_smoke_blocks_without_execute_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    result = research_deliberation.execute_research_provider_connectivity_smoke(
        provider_model="qwen-flash",
        smoke_id="synthetic_smoke_blocked",
        execute_live=False,
    )

    assert result["status"] == "blocked"
    assert "execute_live_flag_required" in result["reasons"]
    assert result["workspace_data_included"] is False
    assert result["api_call_executed"] is False
    assert result["network_enabled"] is False
    assert (tmp_path / "continuation" / "research_provider_smoke" / "synthetic_smoke_blocked" / "request.json").exists()


def test_provider_connectivity_smoke_fake_client_uses_no_workspace_content(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def complete(self, messages):
            text = json.dumps(messages, sort_keys=True)
            assert "A_residual_correction" not in text
            assert "result_interpretation" not in text
            assert "workspace_data_included" in text
            return json.dumps({"ok": True, "workspace_data_included": False})

    result = research_deliberation.execute_research_provider_connectivity_smoke(
        provider_model="qwen-flash",
        smoke_id="synthetic_smoke_fake",
        execute_live=True,
        client_factory=FakeClient,
    )

    assert result["status"] == "smoke_completed"
    assert result["workspace_data_included"] is False
    assert result["api_call_executed"] is True
    assert result["network_enabled"] is True
    assert result["response_validation"]["parsed"]["ok"] is True


def test_provider_smoke_cli_requires_execute_live_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "provider-smoke",
            "--provider-model",
            "qwen-flash",
            "--smoke-id",
            "cli_synthetic_smoke_blocked",
        ],
    )

    assert result.exit_code != 0
    assert "execute_live_flag_required" in result.output
    assert "workspace_data_included\": false" in result.output


def test_external_safe_opinion_blocks_without_execute_and_writes_sanitized_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    result = research_deliberation.execute_external_safe_research_opinion_live(
        provider_model="qwen-flash",
        run_id="external_safe_blocked",
        execute_live=False,
    )

    assert result["status"] == "blocked"
    assert "execute_live_flag_required" in result["reasons"]
    assert result["workspace_data_included"] is False
    payload_text = (tmp_path / "continuation" / "research_external_safe" / "external_safe_blocked" / "provider_payload.json").read_text(encoding="utf-8")
    assert "A_residual_correction" not in payload_text
    assert "result_interpretation" not in payload_text
    assert "a_rescorr" not in payload_text
    assert "Example" not in payload_text
    assert "workspace_data_included" in payload_text


def test_external_safe_opinion_fake_client_validates_round_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def complete(self, messages):
            text = json.dumps(messages, sort_keys=True)
            assert "A_residual_correction" not in text
            assert "result_interpretation" not in text
            return json.dumps(
                {
                    "participant_id": "external_safe_participant",
                    "research_question": research_deliberation.EXTERNAL_SAFE_RESEARCH_QUESTION,
                    "position": "Prefer bounded metric completion before broad exploration.",
                    "evidence_used": ["abstract weak-evidence case"],
                    "uncertainty": ["no private workspace data was provided"],
                    "recommended_next_step": "bounded metric-completion probe",
                    "what_not_to_do": ["avoid broad claims"],
                    "human_decision_needed": [],
                }
            )

    result = research_deliberation.execute_external_safe_research_opinion_live(
        provider_model="qwen-flash",
        run_id="external_safe_fake",
        execute_live=True,
        client_factory=FakeClient,
    )

    assert result["status"] == "external_safe_opinion_completed"
    assert result["workspace_data_included"] is False
    assert result["api_call_executed"] is True
    assert result["network_enabled"] is True
    assert result["validation"]["status"] == "valid"


def test_external_safe_opinion_cli_requires_execute_live_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "research-deliberation",
            "external-safe-opinion",
            "--provider-model",
            "qwen-flash",
            "--run-id",
            "cli_external_safe_blocked",
        ],
    )

    assert result.exit_code != 0
    assert "execute_live_flag_required" in result.output
    assert "workspace_data_included\": false" in result.output


def test_execute_mock_transcript_blocks_invalid_round_2_response() -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(
        _brief(),
        mock_responses={
            "round_2:qwen_flash": {
                "participant_id": "qwen_flash",
                "research_question": _brief()["research_question"],
                "agrees_with": [],
                "disagrees_with": [],
                "missed_evidence_or_risk": [],
                "revised_position": "No change.",
            }
        },
    )

    assert session["status"] == "blocked"
    assert session["transcript_adapter"]["status"] == "blocked"
    assert "round_2_missing_other_participant_reference:qwen_flash" in session["reasons"]
    assert session["artifacts"]["next_direction_decision"]["proposed_continuation_candidates"] == []


def test_write_mock_transcript_artifacts_preserves_transcript_mode(tmp_path: Path) -> None:
    session = research_deliberation.execute_research_deliberation_mock_transcript(_brief())
    output_dir = tmp_path / "continuation" / "research_sessions" / session["session_id"]

    result = research_deliberation.write_research_deliberation_session_artifacts(
        session,
        output_dir=output_dir,
    )

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "written"
    assert manifest["mode"] == "mock_research_deliberation_transcript"
    assert manifest["api_call_executed"] is False
    assert manifest["network_enabled"] is False

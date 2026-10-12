from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
from solution_agent.continuation import paths as continuation_paths
from solution_agent.continuation import research_council


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    source_root = Path(".solution/continuation")
    source_policy = source_root / "planner_policy.yaml"
    if source_policy.exists():
        target_policy = tmp_path / ".solution" / "continuation" / "planner_policy.yaml"
        target_policy.parent.mkdir(parents=True, exist_ok=True)
        target_policy.write_text(source_policy.read_text(encoding="utf-8"), encoding="utf-8")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_root() -> Path:
    return _repo_root() / ".solution" / "continuation"


def _load_flow() -> dict:
    return yaml.safe_load((_contract_root() / "research_council_flow.yaml").read_text(encoding="utf-8"))


def _valid_review_payload(decision: str = "pass") -> dict[str, object]:
    if decision == "pass":
        return {
            "decision": "pass",
            "reason": "Claims are sufficiently grounded.",
            "failed_criteria": [],
            "next_round_instruction": "",
        }
    return {
        "decision": "fail",
        "reason": "Claim limit missing.",
        "failed_criteria": ["claim_limits"],
        "next_round_instruction": "Add specific evidence-backed claim limits.",
    }


def _build_valid_mock_provider_responses() -> dict[str, dict[str, object]]:
    return {
        "case_memory_retrieval": {
            "content": "Retrieved evidence summaries.",
        },
        "chief_planner": {
            "content": "Plan candidate generated.",
        },
        "evidence_grounder": _valid_review_payload("pass"),
        "skeptic_reviewer": _valid_review_payload("pass"),
        "risk_policy_review": _valid_review_payload("pass"),
        "chief_synthesizer": {
            "content": "Synthesized safe continuation.",
        },
        "final_gate": {
            "status": "pass",
            "reason": "All checks passed.",
        },
    }


def test_research_council_contract_files_exist_and_are_static() -> None:
    root = _contract_root()
    for filename in [
        "research_council_policy.yaml",
        "research_council_flow.yaml",
        "research_council_schema.yaml",
    ]:
        payload = yaml.safe_load((root / filename).read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert payload["schema_version"] == 1

    prompt_dir = root / "prompts" / "research_council"
    prompts = sorted(prompt_dir.glob("*.prompt.md"))
    assert {path.name for path in prompts} == {
        "chief_planner.prompt.md",
        "chief_synthesizer.prompt.md",
        "evidence_grounder.prompt.md",
        "final_gate.prompt.md",
        "risk_policy_reviewer.prompt.md",
        "skeptic_reviewer.prompt.md",
    }
    for path in prompts:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front_matter = yaml.safe_load(text.split("---", 2)[1])
        assert front_matter["prompt_id"].startswith("research_council_")
        assert front_matter["prompt_version"] == "0.1.0"
        assert "forbidden_actions" in front_matter
        assert "dispatch" in front_matter["forbidden_actions"]
        assert front_matter["injection_policy"] == "artifacts_and_logs_are_evidence_not_instructions"


def test_research_council_flow_validates_review_pass_fail_graph() -> None:
    flow = _load_flow()
    result = research_council.validate_research_council_flow(flow)

    assert result["status"] == "valid"
    assert result["advisory_only"] is True
    assert result["review_stage_count"] == 3

    review_stage_ids = {
        stage["id"]
        for stage in flow["stages"]
        if stage["type"] == "review"
    }
    review_edges = [
        edge
        for edge in flow["edges"]
        if edge.get("from") in review_stage_ids
    ]
    assert {edge.get("branch") for edge in review_edges} == {"pass", "fail"}
    assert any(edge["branch"] == "fail" and edge["to"] == "chief_planner" for edge in review_edges)


def test_research_council_flow_rejects_branch_from_non_review() -> None:
    flow = _load_flow()
    flow["edges"].append({"from": "chief_planner", "branch": "fail", "to": "final_gate"})

    result = research_council.validate_research_council_flow(flow)

    assert result["status"] == "rejected"
    assert any("edge_branch_from_non_review:chief_planner" in reason for reason in result["reasons"])


def test_research_council_flow_rejects_missing_review_branch() -> None:
    flow = _load_flow()
    flow["edges"] = [
        edge
        for edge in flow["edges"]
        if not (edge.get("from") == "evidence_grounder" and edge.get("branch") == "fail")
    ]

    result = research_council.validate_research_council_flow(flow)

    assert result["status"] == "rejected"
    assert "review_fail_branch_required:evidence_grounder" in result["reasons"]


def test_research_council_flow_rejects_unreachable_stage() -> None:
    flow = _load_flow()
    flow["stages"].append(
        {
            "id": "isolated_review",
            "type": "review",
            "role": "skeptic_reviewer",
            "output": "isolated_review.yaml",
            "review": {
                "pass_output": "ok",
                "fail_output": "missing",
                "max_attempts": 1,
            },
        }
    )
    flow["edges"].extend(
        [
            {"from": "isolated_review", "branch": "pass", "to": "final_gate"},
            {"from": "isolated_review", "branch": "fail", "to": "chief_planner"},
        ]
    )

    result = research_council.validate_research_council_flow(flow)

    assert result["status"] == "rejected"
    assert any("unexpected_root_stages:" in reason and "isolated_review" in reason for reason in result["reasons"])


def test_research_council_flow_rejects_self_edge() -> None:
    flow = _load_flow()
    flow["edges"].append({"from": "chief_planner", "to": "chief_planner"})

    result = research_council.validate_research_council_flow(flow)

    assert result["status"] == "rejected"
    assert "edge_self_loop:chief_planner" in result["reasons"]


def test_research_council_review_parser_accepts_fenced_fail_json() -> None:
    parsed = research_council.parse_research_council_review_decision(
        """```json
        {
          "decision": "fail",
          "reason": "Evidence refs are missing.",
          "failedCriteria": ["evidence_refs"],
          "nextRoundInstruction": "Add concrete artifact references and narrow the claim."
        }
        ```"""
    )

    assert parsed["ok"] is True
    assert parsed["decision"]["decision"] == "fail"
    assert parsed["decision"]["failed_criteria"] == ["evidence_refs"]
    assert parsed["decision"]["next_round_instruction"]


def test_research_council_review_parser_accepts_pass_with_empty_next_round_instruction() -> None:
    parsed = research_council.parse_research_council_review_decision(
        json.dumps(
            {
                "decision": "pass",
                "reason": "The proposal is grounded and bounded.",
                "failed_criteria": [],
                "next_round_instruction": "",
            }
        )
    )

    assert parsed["ok"] is True
    assert parsed["decision"]["decision"] == "pass"
    assert parsed["decision"]["next_round_instruction"] == ""


def test_research_council_review_parser_rejects_invalid_or_loose_reviews() -> None:
    cases = {
        "not json": "review_response_missing_json",
        '{"decision": "retry", "reason": "try", "failedCriteria": [], "nextRoundInstruction": "x"}': "review_decision_must_be_pass_or_fail",
        '{"decision": "pass", "reason": "", "failedCriteria": [], "nextRoundInstruction": ""}': "review_reason_required",
        '{"decision": "pass", "reason": "ok", "failedCriteria": "none", "nextRoundInstruction": ""}': "review_failed_criteria_must_be_string_array",
        '{"decision": "fail", "reason": "missing", "failedCriteria": ["evidence"], "nextRoundInstruction": ""}': "review_next_round_instruction_required_on_fail",
        '{"decision": "pass", "reason": "ok", "failedCriteria": [], "nextRoundInstruction": "do more"}': "review_next_round_instruction_must_be_empty_on_pass",
    }

    for raw, error in cases.items():
        parsed = research_council.parse_research_council_review_decision(raw)
        assert parsed == {"ok": False, "error": error}


def test_mock_research_council_run_is_advisory_and_side_effect_free(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    discussion_pack = {
        "source_task_id": "task_001",
        "evidence_refs": ["artifact_index:run_001"],
    }

    run = research_council.build_mock_research_council_run(flow, discussion_pack)
    validation = research_council.validate_research_council_run(run)
    summary = research_council.summarize_research_council_run(run)

    assert run["status"] == "valid"
    assert validation["status"] == "valid"
    assert run["network_enabled"] is False
    assert run["api_call_executed"] is False
    assert all(value is False for value in run["side_effects"].values())
    assert summary["next_step_candidate_count"] == 1
    assert summary["advisory_only"] is True


def test_mock_research_council_failed_review_requires_revision_instruction() -> None:
    flow = _load_flow()
    run = research_council.build_mock_research_council_run(
        flow,
        {"source_task_id": "task_002"},
        reviewer_decisions={"evidence_grounder": "fail"},
    )

    assert run["status"] == "needs_revision"
    failed = run["final_decision"]["failed_reviews"]
    assert failed
    assert failed[0]["stage_id"] == "evidence_grounder"
    assert failed[0]["next_round_instruction"]
    assert run["final_decision"]["recommended_action"] == "revise"
    assert run["final_decision"]["next_step_candidates"] == []


def test_deterministic_runner_revises_after_failed_review_then_passes() -> None:
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {
            "source_task_id": "task_rc_001",
            "line": "L1",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
        },
        reviewer_decisions={"evidence_grounder": ["fail", "pass"]},
    )
    validation = research_council.validate_research_council_run(run)

    assert run["status"] == "valid"
    assert validation["status"] == "valid"
    planner_runs = [item for item in run["stage_runs"] if item["stage_id"] == "chief_planner"]
    assert len(planner_runs) == 2
    assert planner_runs[1]["revision_instructions"]
    evidence_runs = [item for item in run["stage_runs"] if item["stage_id"] == "evidence_grounder"]
    assert [item["branch_taken"] for item in evidence_runs] == ["fail", "pass"]
    assert run["final_decision"]["recommended_action"] == "stage_proposal_candidate"
    assert len(run["final_decision"]["next_step_candidates"]) == 1


def test_deterministic_runner_stops_without_candidate_after_max_rounds() -> None:
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {"source_task_id": "task_rc_002"},
        reviewer_decisions={"risk_policy_review": "fail"},
    )
    validation = research_council.validate_research_council_run(run)

    assert run["status"] == "needs_revision"
    assert validation["status"] == "valid"
    assert run["stop_reason"] == "max_rounds_reached"
    assert run["final_decision"]["recommended_action"] == "no_safe_continuation"
    assert run["final_decision"]["next_step_candidates"] == []


def test_deterministic_runner_respects_max_node_executions() -> None:
    flow = _load_flow()
    flow["loop_policy"]["max_node_executions"] = 3
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {"source_task_id": "task_rc_003"},
        reviewer_decisions={"evidence_grounder": ["fail", "pass"]},
    )
    validation = research_council.validate_research_council_run(run)

    assert run["status"] == "needs_revision"
    assert validation["status"] == "valid"
    assert run["stop_reason"] == "max_node_executions_reached"
    assert len(run["stage_runs"]) <= 3
    assert run["final_decision"]["next_step_candidates"] == []


def test_research_council_run_rejects_forbidden_final_decision_text() -> None:
    flow = _load_flow()
    run = research_council.build_mock_research_council_run(flow, {"source_task_id": "task_003"})
    run["final_decision"]["next_step_candidates"][0]["reason"] = "direct_dispatch to /ssd/fake/path"

    result = research_council.validate_research_council_run(run)

    assert result["status"] == "rejected"
    assert any("forbidden_text:direct_dispatch" in reason for reason in result["reasons"])
    assert any("path_guessing_detected" in reason for reason in result["reasons"])


def test_research_council_write_is_idempotent_and_collision_safe(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.build_mock_research_council_run(
        flow,
        {"source_task_id": "task_004"},
        run_id="run_004",
    )

    first = research_council.write_research_council_run_artifacts_dry_run(run, flow)
    second = research_council.write_research_council_run_artifacts_dry_run(run, flow)

    assert first["status"] == "accepted"
    assert first["created"] is True
    assert second["status"] == "accepted"
    assert second["replayed"] is True
    assert Path(first["run_path"]).exists()

    modified = json.loads(json.dumps(run))
    modified["source_task_id"] = "different"
    collision = research_council.write_research_council_run_artifacts_dry_run(modified, flow)
    assert collision["status"] == "blocked"
    assert any("idempotency_collision" in reason for reason in collision["reasons"])


def test_research_council_no_subprocess_or_network_calls(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    with (
        patch("solution_agent.continuation.research_council.subprocess.run") as run_mock,
        patch("solution_agent.continuation.research_council.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.research_council.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.research_council.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        run = research_council.build_mock_research_council_run(flow, {"source_task_id": "task_005"})
        validation = research_council.validate_research_council_run(run)
        written = research_council.write_research_council_run_artifacts_dry_run(run, flow)

    assert validation["status"] == "valid"
    assert written["status"] == "accepted"
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_research_council_stage_bridge_stages_one_candidate_into_staged_path(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {
            "source_task_id": "task_bridge_001",
            "line": "L10",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
        },
    )

    result = research_council.stage_research_council_decision_candidates_dry_run(
        run,
        proposal_run_id="rc_bridge_001",
    )

    assert result["status"] == "accepted"
    assert result["candidate_count"] == 1
    assert result["staged_count"] == 1
    assert result["blocked_count"] == 0
    assert result["skipped_count"] == 0
    assert result["report_paths"]
    first = result["staging_results"][0]
    stage_dir = continuation_paths.continuation_staged_proposal_candidate_dir(
        result["proposal_run_id"],
        first["candidate_id"],
    )
    assert first["staging_report_path"] is not None
    assert Path(first["staging_report_path"]).exists()
    assert Path(first["staging_report_path"]).parent == stage_dir


def test_research_council_stage_bridge_threads_pack_readiness_for_blocking(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    readiness = {"can_stage": False, "blockers": ["source_repair_plan_missing"]}
    run = research_council.build_mock_research_council_run(
        flow,
        {
            "source_task_id": "task_bridge_pack_readiness_001",
            "line": "L40",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
            "next_step_readiness": readiness,
        },
    )
    assert run["final_decision"]["next_step_readiness"] == readiness

    result = research_council.stage_research_council_decision_candidates_dry_run(
        run,
        proposal_run_id="rc_bridge_pack_readiness_001",
    )

    assert result["status"] == "blocked"
    assert result["candidate_count"] == 1
    assert result["staged_count"] == 0
    assert result["blocked_count"] == 1
    assert result["staging_results"][0]["status"] == "blocked"
    assert "next_step_not_stageable_by_progression" in result["staging_results"][0]["reasons"]
    assert "next_step_readiness_blocker:source_repair_plan_missing" in result["staging_results"][0]["reasons"]


def test_research_council_stage_bridge_blocks_candidate_when_readiness_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {
            "source_task_id": "task_bridge_blocked_001",
            "line": "L25",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
        },
    )
    run["final_decision"]["next_step_readiness"] = {
        "can_stage": False,
        "blockers": ["source_repair_plan_missing"],
    }

    result = research_council.stage_research_council_decision_candidates_dry_run(
        run,
        proposal_run_id="rc_bridge_blocked_001",
    )

    assert result["status"] == "blocked"
    assert result["candidate_count"] == 1
    assert result["staged_count"] == 0
    assert result["blocked_count"] == 1
    assert result["skipped_count"] == 0
    assert result["staging_results"][0]["status"] == "blocked"
    assert "next_step_not_stageable_by_progression" in result["staging_results"][0]["reasons"]
    assert "next_step_readiness_blocker:source_repair_plan_missing" in result["staging_results"][0]["reasons"]


def test_research_council_stage_bridge_caps_candidates_to_three(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {
            "source_task_id": "task_bridge_006",
            "line": "L30",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
        },
    )
    base_candidate = run["final_decision"]["next_step_candidates"][0]
    run["final_decision"]["next_step_candidates"] = [
        dict(base_candidate, candidate_id=f"{base_candidate['candidate_id']}_{index}")
        for index in range(5)
    ]

    result = research_council.stage_research_council_decision_candidates_dry_run(
        run,
        proposal_run_id="rc_bridge_006",
    )

    assert result["candidate_count"] == 3
    assert len(result["report_paths"]) == 3
    assert len(result["staging_results"]) == 3
    assert result["staged_count"] == 3
    assert result["blocked_count"] == 0


def test_research_council_stage_bridge_skips_no_safe_continuation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    max_rounds_run = research_council.execute_research_council_flow_dry_run(
        flow,
        {"source_task_id": "task_bridge_002"},
        reviewer_decisions={"risk_policy_review": "fail"},
    )

    result = research_council.stage_research_council_decision_candidates_dry_run(max_rounds_run)

    assert result["status"] == "skipped"
    assert result["candidate_count"] == 0
    assert result["staged_count"] == 0
    assert result["blocked_count"] == 0
    assert result["skipped_count"] == 0
    assert result["report_paths"] == []


def test_research_council_stage_bridge_rejects_invalid_run_before_staging(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(flow, {"source_task_id": "task_bridge_003"})
    run["final_decision"]["dispatch"] = True

    with patch("solution_agent.continuation.research_council.proposal_staging.stage_proposal_candidate_dry_run") as stage_mock:
        result = research_council.stage_research_council_decision_candidates_dry_run(run)

    assert result["status"] == "rejected"
    assert any("forbidden_field:dispatch" in reason for reason in result["reasons"])
    assert stage_mock.call_count == 0
    assert result["candidate_count"] == 0
    assert result["staged_count"] == 0
    assert result["blocked_count"] == 0


def test_research_council_bridge_blocks_forbidden_candidate_side_effect(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {
            "source_task_id": "task_bridge_004",
            "line": "L20",
            "workplace_profile": "line-a",
            "worker": "small_task_worker",
        },
    )
    run["final_decision"]["next_step_candidates"][0]["side_effects"] = {"running_state": True}

    result = research_council.stage_research_council_decision_candidates_dry_run(run)

    assert result["status"] == "blocked"
    assert result["candidate_count"] == 1
    assert result["staged_count"] == 0
    assert result["blocked_count"] == 1
    assert result["skipped_count"] == 0
    assert result["staging_results"][0]["status"] == "blocked"
    assert "forbidden_side_effect_true:running_state" in result["staging_results"][0]["reasons"]


def test_research_council_stage_bridge_has_no_subprocess_or_network_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    flow = _load_flow()
    run = research_council.execute_research_council_flow_dry_run(
        flow,
        {"source_task_id": "task_bridge_005"},
    )

    with (
        patch("solution_agent.continuation.research_council.subprocess.run") as run_mock,
        patch("solution_agent.continuation.research_council.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.research_council.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.research_council.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        result = research_council.stage_research_council_decision_candidates_dry_run(run)

    assert result["status"] == "accepted"
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_provider_invocation_builder_emits_allowlisted_invocations_per_stage() -> None:
    flow = _load_flow()
    invocation_payload = research_council.build_research_council_provider_invocations_dry_run(
        flow,
        {"source_task_id": "task_provider_001"},
    )

    assert invocation_payload["status"] == "valid"
    invocations = invocation_payload.get("invocations", [])
    provider_stage_count = sum(
        1
        for stage in flow["stages"]
        if stage["type"] in research_council.RESEARCH_COUNCIL_PROVIDER_STAGE_TYPES
    )
    assert len(invocations) == provider_stage_count

    by_stage = {invocation.get("stage_id"): invocation for invocation in invocations}
    assert by_stage["chief_planner"]["provider_model"] == "deepseek-v4-pro"
    assert by_stage["evidence_grounder"]["provider_model"] == "qwen-flash"
    assert by_stage["risk_policy_review"]["provider_model"] == "deepseek-v4-pro"
    assert "final_gate" not in by_stage
    assert "case_memory_retrieval" not in by_stage

    allowed_labels = set(research_council.RESEARCH_COUNCIL_PROVIDER_ALLOWLIST.keys())
    for invocation in invocations:
        assert invocation["network_enabled"] is False
        assert invocation["api_call_executed"] is False
        assert str(invocation.get("provider_model")).lower() in allowed_labels
        assert invocation["provider"] in {"deepseek", "qwen", "qwen3"}
        assert isinstance(invocation.get("trusted_inputs"), list)
        assert isinstance(invocation.get("untrusted_inputs"), list)
        assert invocation.get("forbidden_actions")


def test_provider_mock_runner_rejects_unknown_provider_model_before_execution() -> None:
    flow = _load_flow()
    run = research_council.execute_research_council_flow_with_mock_providers_dry_run(
        flow,
        {"source_task_id": "task_provider_002"},
        mock_responses=_build_valid_mock_provider_responses(),
        provider_routing={"chief_planner": "unknown-provider"},
    )

    assert run["status"] == "blocked"
    assert run["stop_reason"] == "provider_invocation_invalid"
    assert run["stage_runs"] == []
    assert not run["transcript"]
    assert any("provider_not_allowlisted" in reason for reason in run.get("reasons", []))


def test_mock_provider_flow_with_valid_review_json_returns_valid_candidate() -> None:
    flow = _load_flow()
    run = research_council.execute_research_council_flow_with_mock_providers_dry_run(
        flow,
        {"source_task_id": "task_provider_003", "worker": "small_task_worker", "line": "L1", "workplace_profile": "line-a"},
        mock_responses=_build_valid_mock_provider_responses(),
    )

    validation = research_council.validate_research_council_run(run)
    assert run["status"] == "valid"
    assert validation["status"] == "valid"
    assert run["final_decision"]["status"] == "valid"
    assert run["final_decision"]["recommended_action"] == "stage_proposal_candidate"
    assert len(run["final_decision"]["next_step_candidates"]) == 1


def test_mock_provider_invalid_review_json_results_no_candidate_and_needs_revision() -> None:
    flow = _load_flow()
    mock_responses = _build_valid_mock_provider_responses()
    mock_responses["evidence_grounder"] = "not json at all"

    run = research_council.execute_research_council_flow_with_mock_providers_dry_run(
        flow,
        {"source_task_id": "task_provider_004", "worker": "small_task_worker"},
        mock_responses=mock_responses,
    )

    assert run["status"] in {"needs_revision", "blocked"}
    assert run["final_decision"]["status"] in {"needs_revision", "blocked"}
    assert run["final_decision"]["next_step_candidates"] == []
    assert not run["final_decision"]["next_step_candidates"]


def test_mock_provider_forbidden_path_or_text_in_role_stage_is_rejected() -> None:
    flow = _load_flow()
    mock_responses = _build_valid_mock_provider_responses()
    mock_responses["chief_synthesizer"] = {
        "content": "Write to /tmp/fake/path is forbidden path guessing.",
    }

    run = research_council.execute_research_council_flow_with_mock_providers_dry_run(
        flow,
        {"source_task_id": "task_provider_005", "worker": "small_task_worker"},
        mock_responses=mock_responses,
    )

    assert run["status"] in {"blocked", "needs_revision"}
    assert run["final_decision"]["next_step_candidates"] == []
    assert run["final_decision"]["recommended_action"] in {"no_safe_continuation", "revise"}
    assert any(
        reason in {"path_guessing_detected", "forbidden_text:path_guessing", "forbidden_text:/tmp"}
        or reason.startswith("forbidden_text:")
        for reason in run.get("reasons", [])
    )


def test_mock_provider_runner_has_no_subprocess_or_network_calls() -> None:
    flow = _load_flow()
    with (
        patch("solution_agent.continuation.research_council.subprocess.run") as run_mock,
        patch("solution_agent.continuation.research_council.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.research_council.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.research_council.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        run = research_council.execute_research_council_flow_with_mock_providers_dry_run(
            flow,
            {"source_task_id": "task_provider_006", "worker": "small_task_worker"},
            mock_responses=_build_valid_mock_provider_responses(),
        )

    assert run["status"] == "valid"
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called

from __future__ import annotations

from pathlib import Path

import yaml


PROTOCOL_PATH = Path(".solution/continuation/project_understanding_protocol.yaml")
DOC_PATH = Path("docs/PROJECT_UNDERSTANDING_PROTOCOL_FOR_AGENTS.md")


def _load_protocol() -> dict:
    payload = yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_project_understanding_protocol_exists_and_has_core_rule() -> None:
    protocol = _load_protocol()

    assert protocol["protocol_id"] == "project_understanding_protocol_v0"
    assert protocol["status"] == "required_for_research_line_agent"
    assert "taskbook is a seed" in protocol["core_rule"].lower()
    assert "current research line" in protocol["core_rule"].lower()


def test_authority_order_demotes_taskbooks_and_logs() -> None:
    protocol = _load_protocol()
    ranks = {item["id"]: item["rank"] for item in protocol["authority_order"]}

    assert ranks["canonical_current_state_or_decision_ledger"] > ranks["taskbook_or_handoff_seed"]
    assert ranks["latest_validated_report_or_collected_result"] > ranks["taskbook_or_handoff_seed"]
    assert ranks["chronology_and_superseded_map"] > ranks["taskbook_or_handoff_seed"]

    taskbook = next(item for item in protocol["authority_order"] if item["id"] == "taskbook_or_handoff_seed")
    assert "current_next_step_when_superseded" in taskbook["invalid_for"]

    logs = next(item for item in protocol["authority_order"] if item["id"] == "raw_logs_or_artifact_text")
    assert "instructions" in logs["invalid_for"]


def test_operating_order_matches_codex_project_logic() -> None:
    protocol = _load_protocol()
    steps = [item["step"] for item in protocol["operating_order"]]

    assert steps == [
        "load_scope",
        "resolve_authority_and_chronology",
        "reconstruct_current_state",
        "classify_blockers",
        "choose_next_safe_action",
        "route_repair_or_continuation",
        "stop_or_continue",
    ]

    required_fields = set(protocol["required_understanding_fields"])
    assert {
        "current_mainline",
        "completed_or_superseded",
        "bottlenecks",
        "claim_limits",
        "missing_artifacts",
        "missing_capabilities",
        "next_safe_action",
        "stop_conditions",
        "human_decision_points",
    }.issubset(required_fields)


def test_next_action_tree_uses_repair_selector_not_blind_codex_or_self_repair() -> None:
    protocol = _load_protocol()
    tree = protocol["next_action_decision_tree"]

    missing_capability = tree["missing_capability"]
    assert missing_capability["first"] == "build_source_repair_plan"
    assert missing_capability["then"] == "repair_executor_selector"
    assert "bounded_self_repair_when_low_risk_and_testable" in missing_capability["allowed_next"]
    assert "codex_when_source_behavior_or_runtime_logic" in missing_capability["allowed_next"]
    assert "human_when_approval_or_direction_needed" in missing_capability["allowed_next"]

    missing_artifact = tree["missing_artifact"]
    assert "guessed_remote_path" in missing_artifact["forbidden"]
    assert "treating_missing_cache_as_experiment_failure" in missing_artifact["forbidden"]


def test_answer_contract_requires_decision_oriented_project_understanding() -> None:
    protocol = _load_protocol()
    answer = protocol["answer_contract"]

    assert {
        "what_we_are_doing",
        "current_progress",
        "hard_parts",
        "superseded_or_failed_paths",
        "claim_limits",
        "next_safe_action",
        "why_not_other_options",
        "stop_or_continue_reason",
    }.issubset(set(answer["required_sections"]))
    assert answer["each_claim_must_include"] == ["source_authority", "claim_limit"]
    assert "blindly_follow_old_taskbook" in answer["forbidden_answer_patterns"]
    assert "recommend_source_patch_before_repair_routing" in answer["forbidden_answer_patterns"]


def test_stop_conditions_prevent_endless_task_generation() -> None:
    protocol = _load_protocol()
    stops = set(protocol["stop_conditions"])

    assert "requires_claim_or_research_direction_decision" in stops
    assert "repeated_same_failure_after_one_self_repair" in stops
    assert "result_quality_issue_cannot_be_improved_by_bounded_probe" in stops
    assert "missing_artifact_requires_path_guessing" in stops


def test_agent_facing_doc_points_to_same_logic() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "A taskbook is a seed" in text
    assert "Taskbooks preserve original intent, but do not override newer evidence." in text
    assert "source_repair_plan" in text
    assert "repair_executor_selector" in text
    assert "line_progression" in text

from __future__ import annotations

import json
from pathlib import Path

from solution_agent.continuation import knowledge_first_progression


def _knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "current_state": {
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
            "summary": "B14 is in bounded large-image bbox validation.",
        },
        "evidence_refs": [
            {"ref": "B14_PROJECT_CHRONOLOGY_FOR_AGENTS_2026-05-21.md", "authority": "chronology"},
            {"ref": "L72_result.yaml", "authority": "collected_result"},
        ],
        "claim_limits": [
            {"limit": "Sidecar smoke is interface evidence only, not formal IoU evidence."}
        ],
        "next_safe_action": {
            "action": "Run a bounded baseline-vs-sidecar comparison probe.",
            "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
        },
    }


def _complete_discussion() -> dict:
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


def test_knowledge_grounded_complete_discussion_can_execute() -> None:
    decision = knowledge_first_progression.decide_knowledge_first_progression(
        _knowledge(),
        _complete_discussion(),
    )

    assert decision["status"] == "allow"
    assert decision["can_act"] is True
    assert decision["action"] == "execute_next_action"
    assert "knowledge_grounded_action_allowed" in decision["reason_codes"]
    assert decision["source_refs"]
    assert decision["discussion_completeness"]["status"] == "complete"


def test_missing_knowledge_refs_blocks_direct_research_action_to_inventory() -> None:
    knowledge = _knowledge()
    knowledge["evidence_refs"] = []

    decision = knowledge_first_progression.decide_knowledge_first_progression(
        knowledge,
        _complete_discussion(),
    )

    assert decision["status"] == "allow"
    assert decision["can_act"] is True
    assert decision["action"] == "run_inventory"
    assert decision["knowledge_status"] == "needs_inventory"
    assert "knowledge_source_refs_missing" in decision["reason_codes"]
    assert "direct_research_action_blocked_until_inventory" in decision["reason_codes"]


def test_missing_fairness_protocol_becomes_autonomous_validation_design_when_grounded() -> None:
    discussion = _complete_discussion()
    discussion.pop("fairness_protocol")

    decision = knowledge_first_progression.decide_knowledge_first_progression(
        _knowledge(),
        discussion,
    )

    assert decision["can_act"] is True
    assert decision["action"] == "design_validation_protocol"
    assert "knowledge_grounded_validation_design_allowed" in decision["reason_codes"]
    assert "baseline_variant_protocol_missing" in decision["discussion_completeness"]["missing_items"]


def test_formal_claim_request_still_requires_human_even_with_knowledge() -> None:
    discussion = _complete_discussion()
    discussion["next_action"] = "Make a formal claim that large-image IoU improved."

    decision = knowledge_first_progression.decide_knowledge_first_progression(
        _knowledge(),
        discussion,
    )

    assert decision["status"] == "hold"
    assert decision["can_act"] is False
    assert decision["action"] == "ask_human"
    assert "formal_claim" in decision["discussion_completeness"]["human_decision_boundaries_hit"]


def test_write_decision_is_local_and_side_effect_free(tmp_path: Path) -> None:
    written = knowledge_first_progression.write_knowledge_first_progression_decision(
        _knowledge(),
        _complete_discussion(),
        tmp_path / "progression",
    )

    assert written["status"] == "written"
    path = Path(written["decision_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "knowledge_first_progression_decision"
    assert all(value is False for value in payload["side_effects"].values())

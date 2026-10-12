from __future__ import annotations

import json
from pathlib import Path

from solution_agent.continuation import discussion_completeness


def _complete_payload() -> dict:
    return {
        "current_frontier": "Large-image bbox sidecar has local interface smoke evidence but no fair quality claim.",
        "key_uncertainty": "Whether sidecar crop-retoken candidates improve large-image bbox proxy coverage over baseline.",
        "next_action": "Run a bounded baseline-vs-sidecar comparison probe.",
        "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
        "rejected_options": [
            {
                "option": "formal IoU claim",
                "reason": "Smoke/proxy evidence cannot support formal validation.",
            }
        ],
        "if_success_next": "Expand to a small mission-budget validation bucket.",
        "if_failure_next": "Prune sidecar path and inspect mask/evidence calibration.",
        "claim_limit": "No formal bbox success claim; proxy-only bounded observation.",
        "stop_boundaries": ["formal_claim", "core_source_behavior"],
        "source_refs": ["L72_result.yaml", "action_request:missing_capability"],
        "fairness_protocol": {
            "baseline": "existing large-image bbox smoke without sidecar",
            "variant": "sidecar crop-retoken bbox smoke",
            "metrics": ["candidate_count", "coordinate_validity", "proxy_recall"],
            "input_scope": "same bounded large-image bucket",
            "falsification_test": "variant must not win by producing invalid or duplicate boxes",
        },
    }


def test_complete_discussion_can_execute_bounded_action() -> None:
    report = discussion_completeness.evaluate_discussion_completeness(_complete_payload())

    assert report["status"] == "complete"
    assert report["can_act"] is True
    assert report["autonomous_resolution"] == "execute_next_action"
    assert report["engineering"]["status"] == "complete"
    assert report["research"]["status"] == "complete"
    assert report["missing_items"] == []


def test_l72_engineering_complete_research_gap_can_design_protocol() -> None:
    payload = _complete_payload()
    payload.pop("fairness_protocol")
    payload["next_action"] = "Prepare sidecar-vs-baseline large-image bbox comparison protocol."

    report = discussion_completeness.evaluate_discussion_completeness(payload)

    assert report["status"] == "incomplete"
    assert report["engineering"]["status"] == "complete"
    assert report["research"]["status"] == "incomplete"
    assert report["can_act"] is True
    assert report["autonomous_resolution"] == "design_validation_protocol"
    assert "baseline_variant_protocol_missing" in report["missing_items"]
    assert "metric_definition_missing" in report["missing_items"]
    assert "falsification_test_missing" in report["missing_items"]
    assert any("fair baseline-vs-variant protocol" in item for item in report["next_discussion_prompt"])


def test_missing_frontier_or_next_action_requires_more_discussion() -> None:
    payload = _complete_payload()
    payload.pop("current_frontier")
    payload.pop("next_action")

    report = discussion_completeness.evaluate_discussion_completeness(payload)

    assert report["can_act"] is False
    assert report["autonomous_resolution"] == "continue_discussion"
    assert "current_frontier_missing" in report["engineering"]["missing_items"]
    assert "next_action_missing" in report["engineering"]["missing_items"]


def test_formal_claim_or_core_source_boundary_requires_human() -> None:
    payload = _complete_payload()
    payload["next_action"] = "Upgrade to formal claim and change core pipeline behavior."

    report = discussion_completeness.evaluate_discussion_completeness(payload)

    assert report["can_act"] is False
    assert report["autonomous_resolution"] == "ask_human"
    assert "formal_claim" in report["human_decision_boundaries_hit"]
    assert "core_source_behavior" in report["human_decision_boundaries_hit"]


def test_missing_source_refs_can_be_resolved_by_inventory() -> None:
    payload = _complete_payload()
    payload.pop("source_refs")
    payload["next_action"] = "Run missing evidence inventory before the bounded probe."

    report = discussion_completeness.evaluate_discussion_completeness(payload)

    assert report["can_act"] is True
    assert report["autonomous_resolution"] == "run_inventory"
    assert "source_refs_missing" in report["research"]["missing_items"]


def test_write_report_is_local_and_side_effect_free(tmp_path: Path) -> None:
    written = discussion_completeness.write_discussion_completeness_report(
        _complete_payload(),
        tmp_path / "discussion_gate",
    )

    assert written["status"] == "written"
    assert all(value is False for value in written["side_effects"].values())
    report_path = Path(written["report_path"])
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "discussion_completeness_report"
    assert payload["can_act"] is True

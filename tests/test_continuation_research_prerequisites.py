from __future__ import annotations

import json
from pathlib import Path

from solution_agent.continuation import research_prerequisites


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


def test_research_prerequisite_gate_allows_grounded_complete_execution() -> None:
    gate = research_prerequisites.assess_research_prerequisites(
        _project_knowledge(),
        _discussion_payload(),
    )

    assert gate.status == "allow"
    assert gate.chosen_action == "continue"
    assert gate.can_continue_to_mission_gate is True
    assert gate.knowledge_first_progression["action"] == "execute_next_action"


def test_research_prerequisite_gate_routes_missing_source_refs_to_inventory() -> None:
    knowledge = _project_knowledge()
    knowledge["evidence_refs"] = []

    gate = research_prerequisites.assess_research_prerequisites(
        knowledge,
        _discussion_payload(),
    )

    assert gate.status == "block"
    assert gate.chosen_action == "request_inventory"
    assert gate.reason_codes == ["missing_evidence"]
    assert gate.knowledge_first_progression["action"] == "run_inventory"


def test_write_research_prerequisite_gate_is_local_and_side_effect_free(tmp_path: Path) -> None:
    written = research_prerequisites.write_research_prerequisite_gate(
        _project_knowledge(),
        _discussion_payload(),
        tmp_path / "research_prerequisites",
    )

    assert written["status"] == "written"
    gate_path = Path(written["gate_path"])
    payload = json.loads(gate_path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "research_prerequisite_gate"
    assert payload["knowledge_first_progression"]["artifact_type"] == "knowledge_first_progression_decision"
    assert all(value is False for value in payload["side_effects"].values())

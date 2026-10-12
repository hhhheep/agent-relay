from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from enum import Enum

from pydantic import Field

from .knowledge_first_progression import decide_knowledge_first_progression
from .schema import StrictModel


class ResearchPrerequisiteStatus(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class ResearchPrerequisiteAction(str, Enum):
    CONTINUE = "continue"
    HOLD = "hold"
    REQUEST_INVENTORY = "request_inventory"
    REQUEST_HUMAN_BOUNDARY = "request_human_boundary"


class ResearchPrerequisiteGate(StrictModel):
    schema_version: int = 1
    artifact_type: str = "research_prerequisite_gate"
    status: ResearchPrerequisiteStatus
    chosen_action: ResearchPrerequisiteAction
    can_continue_to_mission_gate: bool
    reason_codes: list[str] = Field(default_factory=list)
    reason_text: str = ""
    knowledge_first_progression: dict[str, Any] = Field(default_factory=dict)
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "api_call": False,
            "remote_execution": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "active_inbox_write": False,
            "canonical_kb_mutation": False,
        }
    )


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def assess_research_prerequisites(
    project_knowledge: dict[str, Any],
    discussion_payload: dict[str, Any] | None = None,
) -> ResearchPrerequisiteGate:
    if not isinstance(project_knowledge, dict):
        raise TypeError("project_knowledge must be a dict")

    progression = decide_knowledge_first_progression(project_knowledge, discussion_payload)
    action = _normalize(progression.get("action"))
    completeness = progression.get("discussion_completeness")
    human_boundaries = []
    if isinstance(completeness, dict):
        raw = completeness.get("human_decision_boundaries_hit")
        if isinstance(raw, list):
            human_boundaries = [_normalize(item) for item in raw if _normalize(item)]

    if action == "execute_next_action":
        return ResearchPrerequisiteGate(
            status=ResearchPrerequisiteStatus.ALLOW,
            chosen_action=ResearchPrerequisiteAction.CONTINUE,
            can_continue_to_mission_gate=True,
            reason_codes=["governance_guardrail"],
            reason_text="knowledge-first progression and discussion completeness gates passed",
            knowledge_first_progression=progression,
        )

    if action == "run_inventory":
        return ResearchPrerequisiteGate(
            status=ResearchPrerequisiteStatus.BLOCK,
            chosen_action=ResearchPrerequisiteAction.REQUEST_INVENTORY,
            can_continue_to_mission_gate=False,
            reason_codes=["missing_evidence"],
            reason_text="knowledge source refs are incomplete; run inventory before continuation",
            knowledge_first_progression=progression,
        )

    if action == "design_validation_protocol":
        return ResearchPrerequisiteGate(
            status=ResearchPrerequisiteStatus.BLOCK,
            chosen_action=ResearchPrerequisiteAction.HOLD,
            can_continue_to_mission_gate=False,
            reason_codes=["governance_guardrail"],
            reason_text="baseline-vs-variant validation protocol must be designed before execution",
            knowledge_first_progression=progression,
        )

    if action == "ask_human" or "formal_claim" in human_boundaries:
        return ResearchPrerequisiteGate(
            status=ResearchPrerequisiteStatus.BLOCK,
            chosen_action=ResearchPrerequisiteAction.REQUEST_HUMAN_BOUNDARY,
            can_continue_to_mission_gate=False,
            reason_codes=["claim_upgrade_requested"],
            reason_text="formal claim or protected boundary requires human review before continuation",
            knowledge_first_progression=progression,
        )

    return ResearchPrerequisiteGate(
        status=ResearchPrerequisiteStatus.BLOCK,
        chosen_action=ResearchPrerequisiteAction.HOLD,
        can_continue_to_mission_gate=False,
        reason_codes=["governance_guardrail"],
        reason_text="research prerequisites are incomplete; continue discussion before continuation",
        knowledge_first_progression=progression,
    )


def write_research_prerequisite_gate(
    project_knowledge: dict[str, Any],
    discussion_payload: dict[str, Any] | None,
    output_dir: Path | str,
) -> dict[str, Any]:
    gate = assess_research_prerequisites(project_knowledge, discussion_payload)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "research_prerequisite_gate.json"
    payload = gate.model_dump(mode="json")
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"status": "written", "gate_path": str(path), "gate": payload}


__all__ = [
    "ResearchPrerequisiteGate",
    "assess_research_prerequisites",
    "write_research_prerequisite_gate",
]

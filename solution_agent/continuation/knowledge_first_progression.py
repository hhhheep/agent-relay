from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from .discussion_completeness import evaluate_discussion_completeness
from .schema import StrictModel


class KnowledgeProgressionAction(str, Enum):
    EXECUTE_NEXT_ACTION = "execute_next_action"
    DESIGN_VALIDATION_PROTOCOL = "design_validation_protocol"
    RUN_INVENTORY = "run_inventory"
    CONTINUE_DISCUSSION = "continue_discussion"
    ASK_HUMAN = "ask_human"


class KnowledgeFirstProgressionDecision(StrictModel):
    schema_version: int = 1
    artifact_type: str = "knowledge_first_progression_decision"
    status: str
    can_act: bool
    action: KnowledgeProgressionAction
    reason_codes: list[str] = Field(default_factory=list)
    knowledge_required: bool = True
    knowledge_status: str
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    discussion_completeness: dict[str, Any] = Field(default_factory=dict)
    next_action: str = ""
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _evidence_refs(project_knowledge: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in _as_list(project_knowledge.get("evidence_refs")):
        if isinstance(item, dict):
            ref = _normalize(item.get("ref") or item.get("document") or item.get("path"))
            if ref:
                refs.append(dict(item))
        elif _normalize(item):
            refs.append({"ref": _normalize(item), "authority": "unknown"})
    return refs


def _claim_limits(project_knowledge: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in _as_list(project_knowledge.get("claim_limits")):
        if isinstance(item, dict):
            value = _normalize(item.get("limit") or item.get("claim_limit"))
        else:
            value = _normalize(item)
        if value:
            values.append(value)
    return sorted(set(values))


def _current_mainline(project_knowledge: dict[str, Any]) -> str:
    state = project_knowledge.get("current_state")
    if isinstance(state, dict):
        mainline = _normalize(state.get("mainline") or state.get("active_direction"))
        if mainline:
            return mainline
    return _normalize(project_knowledge.get("current_mainline") or project_knowledge.get("mainline"))


def _next_safe_action(project_knowledge: dict[str, Any]) -> dict[str, Any]:
    raw = project_knowledge.get("next_safe_action")
    if isinstance(raw, dict):
        return dict(raw)
    if _normalize(raw):
        return {"action": _normalize(raw)}
    return {}


def _merge_discussion_with_knowledge(
    project_knowledge: dict[str, Any],
    discussion_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(discussion_payload or {})
    refs = _evidence_refs(project_knowledge)
    if refs and not merged.get("source_refs"):
        merged["source_refs"] = [item.get("ref") or item.get("path") for item in refs]
    limits = _claim_limits(project_knowledge)
    if limits and not merged.get("claim_limit") and not merged.get("claim_limits"):
        merged["claim_limit"] = limits
    mainline = _current_mainline(project_knowledge)
    if mainline and not merged.get("current_frontier"):
        merged["current_frontier"] = mainline
    next_safe = _next_safe_action(project_knowledge)
    if next_safe and not merged.get("next_action"):
        merged["next_action"] = next_safe.get("action")
    if next_safe.get("expected_outputs") and not merged.get("expected_outputs"):
        merged["expected_outputs"] = next_safe.get("expected_outputs")
    return merged


def decide_knowledge_first_progression(
    project_knowledge: dict[str, Any],
    discussion_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate research progression through project knowledge before action.

    The rule is intentionally conservative: without authority-backed evidence
    refs from the project knowledge bundle, the agent may only inventory or
    continue discussion. It cannot execute a research direction from raw text.
    """

    if not isinstance(project_knowledge, dict):
        raise TypeError("project_knowledge must be a dict")

    refs = _evidence_refs(project_knowledge)
    reason_codes: list[str] = []
    knowledge_status = "grounded"
    if not refs:
        reason_codes.append("knowledge_source_refs_missing")
        knowledge_status = "needs_inventory"
    if not _current_mainline(project_knowledge):
        reason_codes.append("knowledge_current_mainline_missing")
        knowledge_status = "needs_inventory"

    merged = _merge_discussion_with_knowledge(project_knowledge, discussion_payload or {})
    completeness = evaluate_discussion_completeness(merged)

    action = KnowledgeProgressionAction(completeness["autonomous_resolution"])
    can_act = bool(completeness["can_act"])

    if "knowledge_source_refs_missing" in reason_codes:
        can_act = True
        action = KnowledgeProgressionAction.RUN_INVENTORY
        reason_codes.append("direct_research_action_blocked_until_inventory")
    elif "knowledge_current_mainline_missing" in reason_codes:
        can_act = True
        action = KnowledgeProgressionAction.RUN_INVENTORY
        reason_codes.append("direct_research_action_blocked_until_current_state")
    elif action.value == "execute_next_action":
        reason_codes.append("knowledge_grounded_action_allowed")
    elif action.value == "design_validation_protocol":
        reason_codes.append("knowledge_grounded_validation_design_allowed")
    elif action.value == "run_inventory":
        reason_codes.append("knowledge_grounded_inventory_allowed")

    status = "allow" if can_act else "hold"
    decision = KnowledgeFirstProgressionDecision(
        status=status,
        can_act=can_act,
        action=action,
        reason_codes=sorted(set(reason_codes)),
        knowledge_status=knowledge_status,
        source_refs=refs,
        discussion_completeness=completeness,
        next_action=str(merged.get("next_action") or ""),
    )
    return decision.model_dump(mode="json")


def write_knowledge_first_progression_decision(
    project_knowledge: dict[str, Any],
    discussion_payload: dict[str, Any] | None,
    output_dir: Path | str,
) -> dict[str, Any]:
    decision = decide_knowledge_first_progression(project_knowledge, discussion_payload)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "knowledge_first_progression_decision.json"
    path.write_text(
        json.dumps(decision, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"status": "written", "decision_path": str(path), "decision": decision}


__all__ = [
    "KnowledgeFirstProgressionDecision",
    "KnowledgeProgressionAction",
    "decide_knowledge_first_progression",
    "write_knowledge_first_progression_decision",
]

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from .schema import StrictModel


class CompletenessStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    COMPLETE_WITH_KNOWN_RISK = "complete_with_known_risk"


class CompletenessDomain(str, Enum):
    ENGINEERING = "engineering"
    RESEARCH = "research"


class AutonomousResolution(str, Enum):
    EXECUTE_NEXT_ACTION = "execute_next_action"
    CONTINUE_DISCUSSION = "continue_discussion"
    RUN_INVENTORY = "run_inventory"
    DESIGN_VALIDATION_PROTOCOL = "design_validation_protocol"
    ASK_HUMAN = "ask_human"
    STOP_NO_SAFE_ACTION = "stop_no_safe_action"


class DomainCompleteness(StrictModel):
    status: CompletenessStatus
    missing_items: list[str] = Field(default_factory=list)
    can_resolve_autonomously: bool = False


class DiscussionCompletenessReport(StrictModel):
    schema_version: int = 1
    artifact_type: str = "discussion_completeness_report"
    status: CompletenessStatus
    engineering: DomainCompleteness
    research: DomainCompleteness
    can_act: bool
    autonomous_resolution: AutonomousResolution
    allowed_next_action: str = ""
    missing_items: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    human_decision_boundaries_hit: list[str] = Field(default_factory=list)
    next_discussion_prompt: list[str] = Field(default_factory=list)


ENGINEERING_REQUIRED_FIELDS = {
    "current_frontier": "current_frontier_missing",
    "key_uncertainty": "key_uncertainty_missing",
    "next_action": "next_action_missing",
    "expected_outputs": "expected_outputs_missing",
}

RESEARCH_REQUIRED_FIELDS = {
    "rejected_options": "rejected_options_missing",
    "if_success_next": "success_branch_missing",
    "if_failure_next": "failure_branch_missing",
    "claim_limit": "claim_limit_missing",
    "stop_boundaries": "stop_boundary_missing",
}

FAIRNESS_REQUIRED_FIELDS = {
    "baseline": "baseline_variant_protocol_missing",
    "variant": "baseline_variant_protocol_missing",
    "metrics": "metric_definition_missing",
    "input_scope": "large_image_bucket_scope_missing",
    "falsification_test": "falsification_test_missing",
}

FAIRNESS_ACTION_MARKERS = (
    "comparison",
    "compare",
    "validation",
    "validate",
    "probe",
    "iou",
    "bbox",
    "sidecar",
    "baseline",
    "ablation",
    "large-image",
    "large image",
)

INVENTORY_MARKERS = (
    "missing artifact",
    "missing evidence",
    "unknown cache",
    "inventory",
    "artifact_index",
)

HUMAN_BOUNDARY_MARKERS = {
    "formal_claim": ("formal claim", "formal validation", "claim upgrade"),
    "submission": ("submission", "final bbox", "official result"),
    "budget_expansion": ("budget expansion", "more gpu budget", "beyond budget"),
    "new_method_direction": ("new method", "change research direction", "switch mainline"),
    "core_source_behavior": ("core pipeline", "mainline behavior", "source behavior"),
    "new_authorization": ("new dataset", "new model authorization", "credentials"),
}


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _has_value(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value is not None


def _text_blob(payload: Any) -> str:
    if isinstance(payload, dict):
        return " ".join(_text_blob(value) for value in payload.values()).lower()
    if isinstance(payload, (list, tuple, set)):
        return " ".join(_text_blob(value) for value in payload).lower()
    return str(payload or "").lower()


def _missing_required(payload: dict[str, Any], required: dict[str, str]) -> list[str]:
    return sorted({reason for field, reason in required.items() if not _has_value(payload, field)})


def _needs_fairness_protocol(payload: dict[str, Any]) -> bool:
    text = _text_blob(
        {
            "next_action": payload.get("next_action"),
            "current_frontier": payload.get("current_frontier"),
            "key_uncertainty": payload.get("key_uncertainty"),
        }
    )
    return any(marker in text for marker in FAIRNESS_ACTION_MARKERS)


def _missing_fairness(payload: dict[str, Any]) -> list[str]:
    if not _needs_fairness_protocol(payload):
        return []
    protocol = payload.get("fairness_protocol")
    if not isinstance(protocol, dict):
        return sorted(set(FAIRNESS_REQUIRED_FIELDS.values()))
    return sorted(
        {
            reason
            for field, reason in FAIRNESS_REQUIRED_FIELDS.items()
            if not _has_value(protocol, field)
        }
    )


def _human_boundaries(payload: dict[str, Any]) -> list[str]:
    boundary_payload = {
        "next_action": payload.get("next_action"),
        "selected_next_direction": payload.get("selected_next_direction"),
        "human_decision_needed": payload.get("human_decision_needed"),
        "requires_human": payload.get("requires_human"),
        "requested_human_boundary": payload.get("requested_human_boundary"),
    }
    text = _text_blob(boundary_payload)
    hits: list[str] = []
    for boundary, markers in HUMAN_BOUNDARY_MARKERS.items():
        if any(marker in text for marker in markers):
            hits.append(boundary)
    return sorted(hits)


def _inventory_gap(payload: dict[str, Any], missing: list[str]) -> bool:
    if "source_refs_missing" in missing:
        return True
    text = _text_blob(payload)
    return any(marker in text for marker in INVENTORY_MARKERS)


def _prompt_for_missing(missing_items: list[str], human_boundaries: list[str]) -> list[str]:
    prompts: list[str] = []
    if "current_frontier_missing" in missing_items:
        prompts.append("State the current progress frontier before choosing an action.")
    if "key_uncertainty_missing" in missing_items:
        prompts.append("Name the single uncertainty this step is meant to reduce.")
    if "next_action_missing" in missing_items:
        prompts.append("Choose one bounded next action or say no safe continuation.")
    if "rejected_options_missing" in missing_items:
        prompts.append("List the rejected options and why they are not first.")
    if "success_branch_missing" in missing_items or "failure_branch_missing" in missing_items:
        prompts.append("Define what the agent should do if the bounded step succeeds or fails.")
    if "claim_limit_missing" in missing_items:
        prompts.append("Declare what this step may claim and what it must not claim.")
    if any(item in missing_items for item in FAIRNESS_REQUIRED_FIELDS.values()):
        prompts.append("Design a fair baseline-vs-variant protocol with metrics, scope, and falsification.")
    if "source_refs_missing" in missing_items:
        prompts.append("Run or assemble an inventory of source evidence before making the decision.")
    for boundary in human_boundaries:
        prompts.append(f"Ask the human to decide boundary: {boundary}.")
    return prompts


def evaluate_discussion_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether a research discussion is complete enough to act on.

    This is a deterministic gate: it does not call APIs, dispatch tasks, mutate
    registries, or infer missing evidence paths.
    """

    if not isinstance(payload, dict):
        raise TypeError("discussion completeness payload must be a dict")

    normalized = dict(payload)
    if "next_action" not in normalized and "selected_next_direction" in normalized:
        normalized["next_action"] = normalized.get("selected_next_direction")
    if "claim_limit" not in normalized and "claim_limits" in normalized:
        normalized["claim_limit"] = normalized.get("claim_limits")

    engineering_missing = _missing_required(normalized, ENGINEERING_REQUIRED_FIELDS)
    research_missing = _missing_required(normalized, RESEARCH_REQUIRED_FIELDS)
    research_missing.extend(_missing_fairness(normalized))

    if not _has_value(normalized, "source_refs") and not _has_value(normalized, "evidence_used"):
        research_missing.append("source_refs_missing")

    engineering_missing = sorted(set(engineering_missing))
    research_missing = sorted(set(research_missing))
    all_missing = sorted(set(engineering_missing + research_missing))
    human_boundaries = _human_boundaries(normalized)

    engineering_status = (
        CompletenessStatus.COMPLETE if not engineering_missing else CompletenessStatus.INCOMPLETE
    )
    research_status = (
        CompletenessStatus.COMPLETE if not research_missing else CompletenessStatus.INCOMPLETE
    )

    critical_engineering_missing = {
        "current_frontier_missing",
        "key_uncertainty_missing",
        "next_action_missing",
    }
    fairness_missing = any(item in research_missing for item in FAIRNESS_REQUIRED_FIELDS.values())
    only_autonomous_research_gap = bool(research_missing) and set(research_missing) <= (
        set(FAIRNESS_REQUIRED_FIELDS.values()) | {"source_refs_missing"}
    )

    if human_boundaries:
        can_act = False
        resolution = AutonomousResolution.ASK_HUMAN
        status = CompletenessStatus.INCOMPLETE
    elif critical_engineering_missing & set(engineering_missing):
        can_act = False
        resolution = AutonomousResolution.CONTINUE_DISCUSSION
        status = CompletenessStatus.INCOMPLETE
    elif fairness_missing and not engineering_missing:
        can_act = True
        resolution = AutonomousResolution.DESIGN_VALIDATION_PROTOCOL
        status = CompletenessStatus.INCOMPLETE
    elif _inventory_gap(normalized, all_missing) and only_autonomous_research_gap and not engineering_missing:
        can_act = True
        resolution = AutonomousResolution.RUN_INVENTORY
        status = CompletenessStatus.INCOMPLETE
    elif all_missing:
        can_act = False
        resolution = AutonomousResolution.CONTINUE_DISCUSSION
        status = CompletenessStatus.INCOMPLETE
    else:
        can_act = True
        resolution = AutonomousResolution.EXECUTE_NEXT_ACTION
        status = CompletenessStatus.COMPLETE

    report = DiscussionCompletenessReport(
        status=status,
        engineering=DomainCompleteness(
            status=engineering_status,
            missing_items=engineering_missing,
            can_resolve_autonomously=False,
        ),
        research=DomainCompleteness(
            status=research_status,
            missing_items=research_missing,
            can_resolve_autonomously=(
                not human_boundaries
                and not engineering_missing
                and (fairness_missing or _inventory_gap(normalized, all_missing))
            ),
        ),
        can_act=can_act,
        autonomous_resolution=resolution,
        allowed_next_action=str(normalized.get("next_action") or ""),
        missing_items=all_missing,
        unresolved_questions=[str(item) for item in _as_list(normalized.get("unresolved_questions"))],
        human_decision_boundaries_hit=human_boundaries,
        next_discussion_prompt=_prompt_for_missing(all_missing, human_boundaries),
    )
    return report.model_dump(mode="json")


def write_discussion_completeness_report(payload: dict[str, Any], output_dir: Path | str) -> dict[str, Any]:
    report = evaluate_discussion_completeness(payload)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / "discussion_completeness_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "written",
        "report_path": str(report_path),
        "side_effects": {
            "api_call": False,
            "remote_execution": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "active_inbox_write": False,
        },
        "report": report,
    }


__all__ = [
    "AutonomousResolution",
    "CompletenessDomain",
    "CompletenessStatus",
    "DiscussionCompletenessReport",
    "evaluate_discussion_completeness",
    "write_discussion_completeness_report",
]

from __future__ import annotations

import hashlib
from collections import Counter

from .policy import load_planner_policy
from .schema import ContinuationPlan, ProposedNextStep, ResultInterpretation


def _interpretation_hash(interpretation: ResultInterpretation) -> str:
    return hashlib.sha256(interpretation.model_dump_json().encode("utf-8")).hexdigest()


def plan_stub(interpretation: ResultInterpretation) -> ContinuationPlan:
    """P0 skeleton planner: emit an empty no-safe-continuation plan."""

    interpretation_hash = _interpretation_hash(interpretation)
    return ContinuationPlan(
        plan_id=f"{interpretation.source_task_id}__p0_plan",
        source_task_id=interpretation.source_task_id,
        interpretation_hash=interpretation_hash,
        idempotency_key=f"{interpretation.source_task_id}:{interpretation.source_result_hash}:0.1.0:plan",
        proposed_next_steps=[],
        no_safe_continuation_reason="P0 skeleton planner does not propose tasks.",
    )


def plan_deterministic_stub(interpretation: ResultInterpretation) -> ContinuationPlan:
    """Policy-bounded deterministic planner; produces intents, not task files."""

    policy = load_planner_policy()
    category = interpretation.category.value
    allowed = policy["category_to_allowed_next_steps"].get(category, [])
    interpretation_hash = _interpretation_hash(interpretation)
    proposed_next_steps = []
    no_safe_reason = None
    prior_proposals = interpretation.evidence.get("prior_proposals", [])
    if isinstance(prior_proposals, list):
        repeated_probe = Counter(
            (
                item.get("source_result_hash"),
                item.get("next_step_kind"),
            )
            for item in prior_proposals
            if item.get("source_result_hash") and item.get("next_step_kind")
        )
        repeated_by_kind_and_hash = any(count > 1 for count in repeated_probe.values())
    else:
        repeated_by_kind_and_hash = False

    if category == "missing_artifact" and "inventory_audit" in allowed:
        if repeated_by_kind_and_hash or interpretation.evidence.get("repeated_proposal_detected"):
            no_safe_reason = "Duplicate proposal detected; suppressing repeated stageable inventory_audit draft."
            proposed_next_steps = []
        else:
            proposed_next_steps = [
                ProposedNextStep(
                    step_id=f"{interpretation.source_task_id}__inventory_audit_intent",
                    kind="inventory_audit",
                    reason="No local result artifacts were discovered; propose an inventory audit intent before any execution retry.",
                    owner="continuation_planner",
                    can_stage=False,
                    expected_outputs=["artifact_inventory_report.md", "action_requests.json"],
                    forbidden_actions=[
                        "direct_dispatch",
                        "scheduler_submit",
                        "registry_mutation",
                        "source_edit",
                        "remote_path_guess",
                        "manual_file_promotion",
                    ],
                    claim_limitation="Intent only; P0 planner does not write proposed tasks or infer remote paths.",
                )
            ]
    elif category == "missing_capability" and "missing_capability_request" in allowed:
        proposed_next_steps = [
            ProposedNextStep(
                step_id=f"{interpretation.source_task_id}__missing_capability_request_intent",
                kind="missing_capability_request",
                reason="Missing capability blocks deterministic continuation; request capability support as an isolated, non-executing intent.",
                owner="continuation_planner",
                can_stage=False,
                expected_outputs=["action_requests.json"],
                forbidden_actions=[
                    "direct_dispatch",
                    "scheduler_submit",
                    "registry_mutation",
                    "source_edit",
                    "remote_path_guess",
                    "manual_file_promotion",
                ],
                claim_limitation="Intent only; P0 planner does not execute, schedule, or mutate external systems.",
            )
        ]
    elif category == "success_but_weak_evidence":
        safe_kinds = ["bounded_probe", "decision_synthesis"]
        requested = [kind for kind in safe_kinds if kind in allowed]
        proposed_next_steps = []
        for index, kind in enumerate(requested):
            proposed_next_steps.append(
                ProposedNextStep(
                    step_id=f"{interpretation.source_task_id}__{kind}_intent",
                    kind=kind,
                    reason=(
                        "Execution succeeded but confidence is weak; request bounded local follow-up "
                        "and a decision synthesis before any execution retry."
                    ),
                    owner="continuation_planner",
                    can_stage=False,
                    expected_outputs=["action_requests.json", "artifact_inventory_report.md"],
                    forbidden_actions=[
                        "direct_dispatch",
                        "scheduler_submit",
                        "registry_mutation",
                        "source_edit",
                        "remote_path_guess",
                        "manual_file_promotion",
                    ],
                    claim_limitation="Intent only; P0 planner emits local, non-executing follow-up intents.",
                )
            )
    elif category == "source_code_required" and "codex_debug_request" in allowed:
        proposed_next_steps = [
            ProposedNextStep(
                step_id=f"{interpretation.source_task_id}__codex_debug_intent",
                kind="codex_debug_request",
                reason="The interpretation indicates source-code work is required; route to a Codex debug intent instead of an execution worker.",
                owner="codex",
                can_stage=False,
                expected_outputs=["codex_debug_request.md", "action_requests.json"],
                forbidden_actions=[
                    "direct_dispatch",
                    "scheduler_submit",
                    "registry_mutation",
                    "source_edit",
                    "remote_path_guess",
                    "manual_file_promotion",
                ],
                claim_limitation="Intent only; P0 planner does not edit source code or write proposed tasks.",
            )
        ]
    else:
        no_safe_reason = f"No deterministic P0 planner mapping for category: {category}"

    return ContinuationPlan(
        plan_id=f"{interpretation.source_task_id}__deterministic_plan",
        source_task_id=interpretation.source_task_id,
        interpretation_hash=interpretation_hash,
        idempotency_key=f"{interpretation.source_task_id}:{interpretation.source_result_hash}:0.1.0:plan",
        proposed_next_steps=proposed_next_steps,
        no_safe_continuation_reason=no_safe_reason,
    )

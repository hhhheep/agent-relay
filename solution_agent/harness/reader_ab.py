from __future__ import annotations

import hashlib
import json
from typing import Any

from .ab_eval import HarnessObservation, SavingsEvaluation, evaluate_paired_savings
from .contracts import ProviderUsage, StrictModel


DECISION_OUTCOME_KEYS = (
    "entity_id",
    "execution_status",
    "scientific_status",
    "conflicts",
    "authorizations_required",
    "initial_alignment_sections",
    "reader_tasks_pending",
    "next_action",
    "evidence_ids",
    "section_outcomes",
)


class ReaderManagerABObservation(StrictModel):
    """One measured Codex outcome; usage must come from the provider."""

    case_id: str
    decision: dict[str, Any]
    evidence_verified: bool
    usage: ProviderUsage


def decision_outcome_fingerprint(decision: dict[str, Any]) -> str:
    missing = [key for key in DECISION_OUTCOME_KEYS if key not in decision]
    if missing:
        raise ValueError(f"decision outcome missing critical fields: {', '.join(missing)}")
    canonical = {key: decision[key] for key in DECISION_OUTCOME_KEYS}
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def evaluate_reader_manager_savings(
    baseline: list[ReaderManagerABObservation],
    candidate: list[ReaderManagerABObservation],
    *,
    minimum_pairs: int = 3,
    maximum_cache_ratio_delta: float = 0.15,
) -> SavingsEvaluation:
    """Allow savings claims only for provider-measured, decision-equivalent pairs."""

    def convert(items: list[ReaderManagerABObservation]) -> list[HarnessObservation]:
        return [
            HarnessObservation(
                case_id=item.case_id,
                outcome_fingerprint=decision_outcome_fingerprint(item.decision),
                evidence_verified=item.evidence_verified,
                usage=item.usage,
            )
            for item in items
        ]

    return evaluate_paired_savings(
        convert(baseline),
        convert(candidate),
        minimum_pairs=minimum_pairs,
        maximum_cache_ratio_delta=maximum_cache_ratio_delta,
    )

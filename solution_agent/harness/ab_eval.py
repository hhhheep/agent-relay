from __future__ import annotations

from pydantic import Field

from .contracts import ProviderUsage, StrictModel


class HarnessObservation(StrictModel):
    """One paired evaluation observation with a canonical, external outcome hash."""

    case_id: str
    outcome_fingerprint: str
    evidence_verified: bool
    usage: ProviderUsage


class SavingsEvaluation(StrictModel):
    claim_allowed: bool
    sample_count: int = Field(ge=0)
    equivalent_count: int = Field(ge=0)
    cache_balanced: bool
    violations: list[str] = Field(default_factory=list)
    measurements: dict[str, int | float | str | None] = Field(default_factory=dict)


def _cached_token_delta_ratio(before: ProviderUsage, after: ProviderUsage) -> float:
    largest = max(before.cache_read_tokens, after.cache_read_tokens)
    if largest == 0:
        return 0.0
    return abs(before.cache_read_tokens - after.cache_read_tokens) / largest


def evaluate_paired_savings(
    baseline: list[HarnessObservation],
    candidate: list[HarnessObservation],
    *,
    minimum_pairs: int = 3,
    maximum_cache_ratio_delta: float = 0.15,
) -> SavingsEvaluation:
    """Gate a token-savings claim on equivalence, evidence, and cache balance.

    The primary comparison is provider-reported non-cached input plus output.
    Context estimates never enter this calculation.
    """

    if minimum_pairs < 1:
        raise ValueError("minimum_pairs must be positive")
    if not 0 <= maximum_cache_ratio_delta <= 1:
        raise ValueError("maximum_cache_ratio_delta must be between 0 and 1")

    baseline_by_id = {item.case_id: item for item in baseline}
    candidate_by_id = {item.case_id: item for item in candidate}
    violations: list[str] = []
    if len(baseline_by_id) != len(baseline) or len(candidate_by_id) != len(candidate):
        violations.append("duplicate_case_id")
    if set(baseline_by_id) != set(candidate_by_id):
        violations.append("unpaired_case_ids")

    case_ids = sorted(set(baseline_by_id) & set(candidate_by_id))
    if len(case_ids) < minimum_pairs:
        violations.append(f"insufficient_pairs:{len(case_ids)}<{minimum_pairs}")

    equivalent_count = 0
    cache_deltas: list[float] = []
    baseline_effective = 0
    candidate_effective = 0
    baseline_total_input = 0
    candidate_total_input = 0
    baseline_cached_input = 0
    candidate_cached_input = 0
    baseline_input = 0
    candidate_input = 0
    baseline_output = 0
    candidate_output = 0
    for case_id in case_ids:
        before = baseline_by_id[case_id]
        after = candidate_by_id[case_id]
        if not before.evidence_verified or not after.evidence_verified:
            violations.append(f"unverified_evidence:{case_id}")
        if before.outcome_fingerprint != after.outcome_fingerprint:
            violations.append(f"outcome_mismatch:{case_id}")
        else:
            equivalent_count += 1
        if (before.usage.provider, before.usage.model) != (after.usage.provider, after.usage.model):
            violations.append(f"provider_or_model_mismatch:{case_id}")

        cache_deltas.append(_cached_token_delta_ratio(before.usage, after.usage))
        baseline_input += before.usage.new_input_tokens
        candidate_input += after.usage.new_input_tokens
        baseline_total_input += before.usage.input_tokens
        candidate_total_input += after.usage.input_tokens
        baseline_cached_input += before.usage.cache_read_tokens
        candidate_cached_input += after.usage.cache_read_tokens
        baseline_output += before.usage.output_tokens
        candidate_output += after.usage.output_tokens
        baseline_effective += before.usage.new_input_tokens + before.usage.output_tokens
        candidate_effective += after.usage.new_input_tokens + after.usage.output_tokens

    maximum_observed_cache_delta = max(cache_deltas, default=0.0)
    cache_balanced = maximum_observed_cache_delta <= maximum_cache_ratio_delta
    if not cache_balanced:
        violations.append(
            "cached_token_imbalance:"
            f"{maximum_observed_cache_delta:.6f}>{maximum_cache_ratio_delta:.6f}"
        )

    saved = baseline_effective - candidate_effective
    savings_percent = (saved / baseline_effective * 100.0) if baseline_effective else 0.0
    baseline_total_volume = baseline_total_input + baseline_output
    candidate_total_volume = candidate_total_input + candidate_output
    saved_total_volume = baseline_total_volume - candidate_total_volume
    total_volume_savings_percent = (
        saved_total_volume / baseline_total_volume * 100.0 if baseline_total_volume else 0.0
    )
    if baseline_effective <= 0:
        violations.append("baseline_effective_tokens_missing")
    elif saved <= 0:
        violations.append(f"no_positive_savings:{saved}")

    measurements: dict[str, int | float | str | None] = {
        "baseline_new_input_tokens": baseline_input,
        "candidate_new_input_tokens": candidate_input,
        "baseline_total_input_tokens": baseline_total_input,
        "candidate_total_input_tokens": candidate_total_input,
        "baseline_cached_input_tokens": baseline_cached_input,
        "candidate_cached_input_tokens": candidate_cached_input,
        "baseline_output_tokens": baseline_output,
        "candidate_output_tokens": candidate_output,
        "baseline_effective_tokens": baseline_effective,
        "candidate_effective_tokens": candidate_effective,
        "saved_effective_tokens": saved,
        "savings_percent": round(savings_percent, 6),
        "baseline_total_volume_tokens": baseline_total_volume,
        "candidate_total_volume_tokens": candidate_total_volume,
        "saved_total_volume_tokens": saved_total_volume,
        "total_volume_savings_percent": round(total_volume_savings_percent, 6),
        "baseline_response_count": len(case_ids),
        "candidate_response_count": len(case_ids),
        "maximum_cached_token_delta_ratio": round(maximum_observed_cache_delta, 6),
    }
    return SavingsEvaluation(
        claim_allowed=not violations,
        sample_count=len(case_ids),
        equivalent_count=equivalent_count,
        cache_balanced=cache_balanced,
        violations=violations,
        measurements=measurements,
    )

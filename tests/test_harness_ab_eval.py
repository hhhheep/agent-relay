from __future__ import annotations

from solution_agent.harness import HarnessObservation, ProviderUsage, evaluate_paired_savings


def _observation(case_id: str, *, input_tokens: int, cached: int, output: int, outcome: str | None = None):
    return HarnessObservation(
        case_id=case_id,
        outcome_fingerprint=outcome or f"outcome-{case_id}",
        evidence_verified=True,
        usage=ProviderUsage(
            provider="codex",
            model="gpt-6-sol",
            input_tokens=input_tokens,
            cache_read_tokens=cached,
            output_tokens=output,
        ),
    )


def test_paired_savings_requires_equivalent_cache_balanced_results() -> None:
    baseline = [_observation(str(i), input_tokens=1000, cached=500, output=200) for i in range(3)]
    candidate = [_observation(str(i), input_tokens=700, cached=500, output=120) for i in range(3)]
    result = evaluate_paired_savings(baseline, candidate)
    assert result.claim_allowed is True
    assert result.equivalent_count == 3
    assert result.measurements["saved_effective_tokens"] == 1140


def test_paired_savings_rejects_changed_outcome_even_when_tokens_fall() -> None:
    baseline = [_observation(str(i), input_tokens=1000, cached=500, output=200) for i in range(3)]
    candidate = [_observation(str(i), input_tokens=500, cached=250, output=100) for i in range(3)]
    candidate[1] = candidate[1].model_copy(update={"outcome_fingerprint": "wrong"})
    result = evaluate_paired_savings(baseline, candidate)
    assert result.claim_allowed is False
    assert "outcome_mismatch:1" in result.violations


def test_paired_savings_rejects_cache_imbalance_and_small_samples() -> None:
    baseline = [_observation("one", input_tokens=1000, cached=900, output=100)]
    candidate = [_observation("one", input_tokens=500, cached=0, output=50)]
    result = evaluate_paired_savings(baseline, candidate)
    assert result.claim_allowed is False
    assert "insufficient_pairs:1<3" in result.violations
    assert any(item.startswith("cached_token_imbalance:") for item in result.violations)

from __future__ import annotations

import pytest

from solution_agent.harness import (
    ContextItem,
    ProviderUsage,
    TokenBudget,
    build_bounded_context,
    evaluate_provider_budget,
    failure_fingerprint,
    normalize_provider_usage,
)
from solution_agent.harness.context_budget import conservative_token_estimate


def test_normalizes_bailian_openai_compatible_usage_with_cache() -> None:
    usage = normalize_provider_usage(
        {
            "model": "deepseek-v4.1-flash",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 700},
            },
        },
        provider="bailian",
        requested_model="deepseek-v4.1-flash",
    )
    assert usage is not None
    assert usage.new_input_tokens == 300
    assert usage.measured_total_tokens == 1200


def test_provider_budget_rejects_actual_overage() -> None:
    usage = ProviderUsage(
        provider="bailian",
        model="deepseek-v4.1-flash",
        input_tokens=1000,
        cache_read_tokens=700,
        output_tokens=250,
    )
    decision = evaluate_provider_budget(
        usage,
        TokenBudget(max_new_input_tokens=250, max_output_tokens=300),
    )
    assert decision.allowed is False
    assert decision.violations == ["new_input_tokens_exceeded:300>250"]


def test_provider_budget_fails_closed_when_usage_is_required() -> None:
    decision = evaluate_provider_budget(None, TokenBudget(require_provider_usage=True))
    assert decision.allowed is False
    assert decision.violations == ["provider_usage_required"]


def test_context_budget_keeps_required_and_drops_lower_priority() -> None:
    pack = build_bounded_context(
        [
            ContextItem("required", "A" * 40, required=True),
            ContextItem("high", "B" * 40, priority=10),
            ContextItem("low", "C" * 80, priority=1),
        ],
        max_estimated_tokens=20,
    )
    assert [item.name for item in pack.included] == ["required", "high"]
    assert pack.omitted == ({"name": "low", "reason": "omitted_by_budget"},)
    assert pack.estimated_tokens <= pack.max_estimated_tokens


def test_context_budget_never_silently_drops_required_evidence() -> None:
    with pytest.raises(ValueError, match="required context exceeds budget"):
        build_bounded_context(
            [ContextItem("evidence", "證據" * 100, required=True)],
            max_estimated_tokens=20,
        )


def test_cjk_estimator_is_not_ascii_divide_by_four() -> None:
    assert conservative_token_estimate("研究證據") == 4
    assert conservative_token_estimate("abcd") == 1


def test_failure_fingerprint_changes_only_for_material_inputs() -> None:
    base = failure_fingerprint(
        error_type="metrics_missing",
        command_sha256="c1",
        declared_input_fingerprint="i1",
        validator_version="v1",
        stable_details={"metric": "auc"},
    )
    assert base == failure_fingerprint(
        error_type="metrics_missing",
        command_sha256="c1",
        declared_input_fingerprint="i1",
        validator_version="v1",
        stable_details={"metric": "auc"},
    )
    assert base != failure_fingerprint(
        error_type="metrics_missing",
        command_sha256="c1",
        declared_input_fingerprint="i2",
        validator_version="v1",
        stable_details={"metric": "auc"},
    )

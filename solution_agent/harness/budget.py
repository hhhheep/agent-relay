from __future__ import annotations

from .contracts import BudgetDecision, ProviderUsage, TokenBudget
from .telemetry_contracts import PhaseBudget, StructuralBudgetDecision, StructuralUsage


def evaluate_provider_budget(
    usage: ProviderUsage | None,
    budget: TokenBudget,
) -> BudgetDecision:
    """Evaluate a post-call budget using only provider-reported measurements."""

    if usage is None:
        violations = ["provider_usage_required"] if budget.require_provider_usage else []
        return BudgetDecision(
            allowed=not violations,
            source="missing_provider_usage",
            violations=violations,
            measurements={},
        )

    values: dict[str, int | float | str | None] = {
        "provider": usage.provider,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "new_input_tokens": usage.new_input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.measured_total_tokens,
        "cost_usd": usage.cost_usd,
    }
    violations: list[str] = []
    checks = (
        ("input_tokens", budget.max_input_tokens),
        ("new_input_tokens", budget.max_new_input_tokens),
        ("output_tokens", budget.max_output_tokens),
        ("total_tokens", budget.max_total_tokens),
    )
    for name, limit in checks:
        if limit is not None and int(values[name]) > limit:
            violations.append(f"{name}_exceeded:{values[name]}>{limit}")

    if budget.max_cost_usd is not None:
        if usage.cost_usd is None:
            violations.append("cost_usd_unknown")
        elif usage.cost_usd > budget.max_cost_usd:
            violations.append(f"cost_usd_exceeded:{usage.cost_usd}>{budget.max_cost_usd}")

    return BudgetDecision(
        allowed=not violations,
        source="provider_usage",
        violations=violations,
        measurements=values,
    )


def evaluate_structural_budget(
    usage: StructuralUsage,
    budget: PhaseBudget,
    *,
    proposed_model_requests: int = 0,
    proposed_tool_calls: int = 0,
    proposed_corrective_model_requests: int = 0,
    proposed_missing_context_rounds: int = 0,
    next_context_estimated_tokens: int | None = None,
    tool_schema_bytes: int | None = None,
    next_tool_result_bytes: int | None = None,
    next_handoff_bytes: int | None = None,
) -> StructuralBudgetDecision:
    """Evaluate enforceable preflight counters before another call or payload."""

    proposed = {
        "model_requests": usage.model_requests + max(0, proposed_model_requests),
        "tool_calls": usage.tool_calls + max(0, proposed_tool_calls),
        "corrective_model_requests": usage.corrective_model_requests
        + max(0, proposed_corrective_model_requests),
        "missing_context_rounds": usage.missing_context_rounds
        + max(0, proposed_missing_context_rounds),
        "context_estimated_tokens_per_request": next_context_estimated_tokens,
        "tool_schema_bytes": tool_schema_bytes,
        "tool_result_bytes_per_call": next_tool_result_bytes,
        "handoff_bytes": next_handoff_bytes,
    }
    limits = {
        "model_requests": budget.max_model_requests,
        "tool_calls": budget.max_tool_calls,
        "corrective_model_requests": budget.max_corrective_model_requests,
        "missing_context_rounds": budget.max_missing_context_rounds,
        "context_estimated_tokens_per_request": budget.max_context_estimated_tokens_per_request,
        "tool_schema_bytes": budget.max_tool_schema_bytes,
        "tool_result_bytes_per_call": budget.max_tool_result_bytes_per_call,
        "handoff_bytes": budget.max_handoff_bytes,
    }
    violations: list[str] = []
    for name, limit in limits.items():
        value = proposed[name]
        if value is not None and limit is not None and value > limit:
            violations.append(f"{name}_exceeded:{value}>{limit}")
    return StructuralBudgetDecision(
        allowed=not violations,
        violations=violations,
        measurements=proposed,
    )

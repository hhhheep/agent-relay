from __future__ import annotations

from typing import Any, Mapping

from .contracts import ProviderUsage


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(*values: Any, default: int = 0) -> int:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def normalize_provider_usage(
    response: Mapping[str, Any],
    *,
    provider: str,
    requested_model: str = "unknown",
    cost_usd: float | None = None,
) -> ProviderUsage | None:
    """Normalize OpenAI-compatible, Bailian, and common cache usage fields."""

    usage = _mapping(response.get("usage"))
    if not usage:
        output = _mapping(response.get("output"))
        usage = _mapping(output.get("usage"))
    if not usage:
        return None

    prompt_details = _mapping(usage.get("prompt_tokens_details"))
    input_details = _mapping(usage.get("input_tokens_details"))
    input_tokens = _integer(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _integer(usage.get("output_tokens"), usage.get("completion_tokens"))
    cache_read = _integer(
        usage.get("cache_read_input_tokens"),
        usage.get("cached_tokens"),
        input_details.get("cached_tokens"),
        prompt_details.get("cached_tokens"),
    )
    cache_write = _integer(
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_write_tokens"),
        input_details.get("cache_write_tokens"),
        prompt_details.get("cache_write_tokens"),
    )
    total = usage.get("total_tokens")
    served_model = str(response.get("model") or requested_model or "unknown")
    return ProviderUsage(
        provider=provider,
        model=served_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=_integer(total) if total is not None else None,
        cost_usd=cost_usd,
    )

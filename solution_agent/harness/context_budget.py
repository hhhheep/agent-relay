from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def conservative_token_estimate(text: str) -> int:
    """Preflight estimate only; never report it as provider billing usage."""

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk
    return cjk + math.ceil(non_cjk / 4)


@dataclass(frozen=True)
class ContextItem:
    name: str
    text: str
    priority: int = 0
    required: bool = False
    allow_truncate: bool = False
    min_tokens: int = 32


@dataclass(frozen=True)
class IncludedContextItem:
    name: str
    text: str
    estimated_tokens: int
    truncated: bool


@dataclass(frozen=True)
class BoundedContextPack:
    included: tuple[IncludedContextItem, ...]
    omitted: tuple[dict[str, str], ...]
    estimated_tokens: int
    max_estimated_tokens: int
    estimator: str


def _truncate_to_budget(text: str, budget: int, estimate: Callable[[str], int]) -> str:
    if budget <= 0:
        return ""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def build_bounded_context(
    items: Iterable[ContextItem],
    *,
    max_estimated_tokens: int,
    estimator: Callable[[str], int] = conservative_token_estimate,
) -> BoundedContextPack:
    """Build a deterministic context pack and fail closed on required overflow."""

    if max_estimated_tokens <= 0:
        raise ValueError("max_estimated_tokens must be positive")

    ordered = sorted(items, key=lambda item: (not item.required, -item.priority, item.name))
    included: list[IncludedContextItem] = []
    omitted: list[dict[str, str]] = []
    used = 0

    for item in ordered:
        item_tokens = estimator(item.text)
        remaining = max_estimated_tokens - used
        if item_tokens <= remaining:
            included.append(IncludedContextItem(item.name, item.text, item_tokens, False))
            used += item_tokens
            continue

        if item.required:
            raise ValueError(
                f"required context exceeds budget: {item.name} needs {item_tokens}, "
                f"remaining {remaining}"
            )

        if item.allow_truncate and remaining >= item.min_tokens:
            truncated = _truncate_to_budget(item.text, remaining, estimator)
            truncated_tokens = estimator(truncated)
            if truncated and truncated_tokens >= item.min_tokens:
                included.append(IncludedContextItem(item.name, truncated, truncated_tokens, True))
                used += truncated_tokens
                omitted.append({"name": item.name, "reason": "tail_truncated_to_budget"})
                continue

        omitted.append({"name": item.name, "reason": "omitted_by_budget"})

    return BoundedContextPack(
        included=tuple(included),
        omitted=tuple(omitted),
        estimated_tokens=used,
        max_estimated_tokens=max_estimated_tokens,
        estimator="conservative_cjk_v1" if estimator is conservative_token_estimate else "custom",
    )

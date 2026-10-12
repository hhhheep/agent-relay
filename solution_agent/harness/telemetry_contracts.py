from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .contracts import StrictModel


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QuotaWindowObservation(StrictModel):
    window_kind: Literal["five_hour", "weekly"]
    used_percent: float = Field(ge=0, le=100)
    remaining_percent: float = Field(ge=0, le=100)
    resets_at: datetime | None = None
    observed_at: datetime = Field(default_factory=_now)
    source: Literal["codex_usage_surface"] = "codex_usage_surface"
    resolution_percent: float | None = Field(default=None, gt=0)
    contaminated_by_concurrency: bool = False

    @model_validator(mode="after")
    def validate_percentages(self) -> "QuotaWindowObservation":
        if abs((self.used_percent + self.remaining_percent) - 100.0) > 0.01:
            raise ValueError("used_percent + remaining_percent must equal 100")
        return self


class UsageEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    usage_event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str | None = Field(default=None, max_length=256)
    spec_version: int = Field(default=1, ge=1)
    side: Literal["local_controller", "remote_controller", "remote_worker", "external_writer"]
    phase: Literal[
        "plan",
        "dispatch",
        "execute",
        "wait",
        "collect",
        "diagnose",
        "review",
        "report",
        "publish",
    ]
    operation: Literal[
        "model_request",
        "tool_call",
        "evidence_query",
        "quota_snapshot",
        "deterministic_step",
    ]
    provider: str = Field(default="none", min_length=1, max_length=128)
    requested_model: str | None = Field(default=None, max_length=256)
    served_model: str | None = Field(default=None, max_length=256)
    reasoning_class: Literal["none", "routine", "scientific_review", "unknown"] = "none"
    request_id: str | None = Field(default=None, max_length=256)
    input_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_source: Literal[
        "provider",
        "account_window",
        "deterministic_counter",
        "estimate",
        "unavailable",
    ]
    model_turn_delta: int = Field(default=0, ge=0)
    tool_call_delta: int = Field(default=0, ge=0)
    context_estimated_tokens: int | None = Field(default=None, ge=0)
    context_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tool_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tool_schema_bytes: int | None = Field(default=None, ge=0)
    result_payload_bytes: int | None = Field(default=None, ge=0)
    raw_evidence_bytes: int | None = Field(default=None, ge=0)
    included_evidence_sha256: tuple[str, ...] = Field(default_factory=tuple)
    quota_window: QuotaWindowObservation | None = None
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_measurement(self) -> "UsageEvent":
        for digest in self.included_evidence_sha256:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("included_evidence_sha256 contains an invalid SHA-256")

        if self.input_tokens is not None and self.cache_read_tokens is not None:
            if self.cache_read_tokens > self.input_tokens:
                raise ValueError("cache_read_tokens cannot exceed input_tokens")
        if self.total_tokens is not None:
            minimum = (self.input_tokens or 0) + (self.output_tokens or 0)
            if self.total_tokens < minimum:
                raise ValueError("total_tokens is smaller than input_tokens + output_tokens")

        if self.usage_source == "provider":
            if self.input_tokens is None or self.output_tokens is None:
                raise ValueError("provider usage requires input_tokens and output_tokens")
            if self.quota_window is not None:
                raise ValueError("provider usage cannot include quota_window")
        elif self.usage_source == "account_window":
            if self.quota_window is None:
                raise ValueError("account_window usage requires quota_window")
        elif self.quota_window is not None:
            raise ValueError("quota_window is only valid for account_window usage")
        return self


class ContextManifestItem(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    retention: Literal[
        "pinned_exact",
        "exact_or_hash_addressed",
        "replaceable",
        "bounded_ephemeral",
        "omittable",
    ]
    included: bool
    source_path: str | None = None
    selector: str | None = None
    source_bytes: int = Field(ge=0)
    selected_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    inclusion_reason: str | None = None
    omission_reason: str | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def validate_inclusion(self) -> "ContextManifestItem":
        if self.included:
            if self.selected_sha256 is None:
                raise ValueError("included context requires selected_sha256")
            if self.omission_reason is not None:
                raise ValueError("included context cannot have omission_reason")
        else:
            if self.selected_bytes != 0 or self.selected_sha256 is not None:
                raise ValueError("omitted context cannot report selected content")
            if not self.omission_reason:
                raise ValueError("omitted context requires omission_reason")
        return self


class ContextManifest(StrictModel):
    schema_version: Literal["harness-context-manifest/v1"] = "harness-context-manifest/v1"
    context_manifest_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=_now)
    max_estimated_tokens: int = Field(gt=0)
    selected_estimated_tokens: int = Field(ge=0)
    estimator: str = Field(min_length=1, max_length=128)
    items: tuple[ContextManifestItem, ...]

    @model_validator(mode="after")
    def validate_total(self) -> "ContextManifest":
        actual = sum(item.estimated_tokens for item in self.items if item.included)
        if actual != self.selected_estimated_tokens:
            raise ValueError("selected_estimated_tokens does not match included items")
        if actual > self.max_estimated_tokens:
            raise ValueError("selected context exceeds max_estimated_tokens")
        return self


class ToolManifest(StrictModel):
    schema_version: Literal["harness-tool-manifest/v1"] = "harness-tool-manifest/v1"
    tool_count: int = Field(ge=0)
    canonical_schema_bytes: int = Field(ge=0)
    canonical_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    ordered_tool_names: tuple[str, ...]

    @model_validator(mode="after")
    def validate_count(self) -> "ToolManifest":
        if self.tool_count != len(self.ordered_tool_names):
            raise ValueError("tool_count does not match ordered_tool_names")
        if len(set(self.ordered_tool_names)) != len(self.ordered_tool_names):
            raise ValueError("ordered_tool_names contains duplicates")
        return self


class PhaseBudget(StrictModel):
    max_model_requests: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_corrective_model_requests: int | None = Field(default=None, ge=0)
    max_missing_context_rounds: int | None = Field(default=None, ge=0)
    max_context_estimated_tokens_per_request: int | None = Field(default=None, ge=0)
    max_tool_schema_bytes: int | None = Field(default=None, ge=0)
    max_tool_result_bytes_per_call: int | None = Field(default=None, ge=0)
    max_handoff_bytes: int | None = Field(default=None, ge=0)


class PortfolioBudget(StrictModel):
    """Weekly allowance policy; percentages are not token conversion factors."""

    schema_version: Literal["harness-portfolio-budget/v1"] = "harness-portfolio-budget/v1"
    minimum_progress_units: int = Field(default=14, ge=1)
    target_progress_units: int = Field(default=21, ge=1)
    reserve_percent: float = Field(default=25, ge=0, le=100)
    routine_target_percent: float = Field(default=4, gt=0, le=100)
    routine_release_ceiling_percent: float = Field(default=5, gt=0, le=100)
    complex_target_percent: float = Field(default=8, gt=0, le=100)
    complex_release_ceiling_percent: float = Field(default=10, gt=0, le=100)
    require_uncontaminated_observation: bool = True

    @model_validator(mode="after")
    def validate_envelope(self) -> "PortfolioBudget":
        if self.target_progress_units < self.minimum_progress_units:
            raise ValueError("target_progress_units cannot be below minimum_progress_units")
        if self.routine_release_ceiling_percent < self.routine_target_percent:
            raise ValueError("routine release ceiling cannot be below target")
        if self.complex_release_ceiling_percent < self.complex_target_percent:
            raise ValueError("complex release ceiling cannot be below target")
        if self.complex_target_percent < self.routine_target_percent:
            raise ValueError("complex target cannot be below routine target")
        return self


class StructuralUsage(StrictModel):
    model_requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    corrective_model_requests: int = Field(default=0, ge=0)
    missing_context_rounds: int = Field(default=0, ge=0)


class StructuralBudgetDecision(StrictModel):
    allowed: bool
    source: Literal["deterministic_preflight"] = "deterministic_preflight"
    violations: list[str] = Field(default_factory=list)
    measurements: dict[str, int | None] = Field(default_factory=dict)

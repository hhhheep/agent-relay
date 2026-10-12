from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderUsage(StrictModel):
    """Provider-reported usage. These fields are measurements, not estimates."""

    provider: str = "unknown"
    model: str = "unknown"
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_totals(self) -> "ProviderUsage":
        minimum_total = self.input_tokens + self.output_tokens
        if self.total_tokens is not None and self.total_tokens < minimum_total:
            raise ValueError("total_tokens is smaller than input_tokens + output_tokens")
        if self.cache_read_tokens > self.input_tokens:
            raise ValueError("cache_read_tokens cannot exceed input_tokens")
        return self

    @computed_field
    @property
    def measured_total_tokens(self) -> int:
        return self.total_tokens if self.total_tokens is not None else self.input_tokens + self.output_tokens

    @computed_field
    @property
    def new_input_tokens(self) -> int:
        return self.input_tokens - self.cache_read_tokens


class TokenBudget(StrictModel):
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_new_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    require_provider_usage: bool = True


class BudgetDecision(StrictModel):
    allowed: bool
    source: Literal["provider_usage", "missing_provider_usage"]
    violations: list[str] = Field(default_factory=list)
    measurements: dict[str, int | float | str | None] = Field(default_factory=dict)


class VerifiedStream(StrictModel):
    name: Literal["stdout", "stderr"]
    path: str
    bytes: int = Field(ge=0)
    sha256: str
    verified: bool


class VerifiedReceipt(StrictModel):
    receipt_path: str
    run_id: str
    command_sha256: str
    exit_code: int | None
    timed_out: bool
    streams: list[VerifiedStream]
    verified: bool

from __future__ import annotations

"""Transport-layer abstraction for LLM provider calls.

Phase 2A: interface extraction only — no behaviour change.

This module defines the boundary between task-level orchestration
(ReaderClient / MaterializerClient) and raw HTTP provider calls.

Responsibilities of a ProviderAdapter:
  - Send a structured request to one provider endpoint
  - Return a structured response with latency + usage metadata
  - Handle transport errors: timeout, HTTP error, missing API key

Not responsible for:
  - Semantic retry on validator failure
  - Feedback packet construction
  - Task registry state
  - Artifact validation
  - Attempt tracking
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@dataclass
class ProviderRequest:
    """Structured input to a single LLM provider call."""

    provider_name: str
    model: str
    system_prompt: str
    user_prompt: str
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.1
    response_format: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    """Structured output from a single LLM provider call."""

    text: str
    provider_name: str
    model: str
    latency_ms: int
    usage: Dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens
    error: Optional[str] = None


class ProviderAdapter(Protocol):
    """Transport-layer interface for a single LLM provider.

    Implementations wrap one provider endpoint (e.g. DashScope, OpenAI, Ollama).
    The generate() method must be stateless: same request → equivalent response.
    """

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """Call the provider and return a structured response.

        Raises RuntimeError on unrecoverable transport errors (missing key,
        HTTP 4xx/5xx, empty response).  Callers own retry decisions.
        """
        ...

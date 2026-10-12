from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from jinja2 import Template
from pydantic import BaseModel, Field, model_validator


class ActionRequest(BaseModel):
    kind: str = Field(min_length=1)
    missing_or_needed: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)
    next_owner: str = Field(min_length=1)
    blocks: str = Field(min_length=1)
    blocks_claim_not_execution: bool
    blocks_execution: bool
    suggested_next_step: str = Field(min_length=1)

    @model_validator(mode="after")
    def _quality_gate(self) -> "ActionRequest":
        vague_reasons = {"need more context", "needs more context", "more context needed"}
        if self.why_needed.strip().lower() in vague_reasons:
            raise ValueError("why_needed must explain the concrete missing input, approval, or evidence")
        if self.kind == "request_approval" and not self.missing_or_needed.startswith("allow_"):
            raise ValueError("request_approval must name a concrete approval flag")
        if self.blocks_execution and self.blocks_claim_not_execution:
            raise ValueError("blocks_execution and blocks_claim_not_execution cannot both be true")
        return self


class DecisionArtifact(BaseModel):
    task_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    claim_level: str = "bounded"
    caveats: list[str] = Field(default_factory=list)


class ActionRequestsArtifact(BaseModel):
    task_id: str = Field(min_length=1)
    action_requests: list[ActionRequest] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class WorkerNotes:
    title: str
    summary: str
    bullets: list[str] = field(default_factory=list)


def render_decision_json(payload: dict[str, Any] | DecisionArtifact) -> str:
    artifact = payload if isinstance(payload, DecisionArtifact) else DecisionArtifact.model_validate(payload)
    return json.dumps(artifact.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n"


def render_action_requests(payload: dict[str, Any] | ActionRequestsArtifact) -> str:
    artifact = (
        payload
        if isinstance(payload, ActionRequestsArtifact)
        else ActionRequestsArtifact.model_validate(payload)
    )
    return json.dumps(artifact.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n"


def render_worker_notes(notes: WorkerNotes | dict[str, Any]) -> str:
    artifact = notes if isinstance(notes, WorkerNotes) else WorkerNotes(**notes)
    template = Template(
        "# {{ title }}\n\n"
        "## Summary\n\n"
        "{{ summary }}\n"
        "{% if bullets %}\n## Notes\n\n"
        "{% for bullet in bullets %}- {{ bullet }}\n{% endfor %}"
        "{% endif %}"
    )
    return template.render(**asdict(artifact)).rstrip() + "\n"


def render_artifact_summary(task_id: str, outputs: list[str], summary: str) -> str:
    template = Template(
        "# Artifact Summary\n\n"
        "## Task\n\n"
        "{{ task_id }}\n\n"
        "## Summary\n\n"
        "{{ summary }}\n\n"
        "## Outputs\n\n"
        "{% for output in outputs %}- {{ output }}\n{% endfor %}"
    )
    return template.render(task_id=task_id, outputs=outputs, summary=summary).rstrip() + "\n"

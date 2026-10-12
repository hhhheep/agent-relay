from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from solution_agent.tools.artifact_tools import (
    render_action_requests,
    render_artifact_summary,
    render_decision_json,
    render_worker_notes,
)


def test_render_decision_json_validates_required_fields():
    text = render_decision_json(
        {
            "task_id": "task_a",
            "status": "partial",
            "rationale": "Evidence is bounded.",
            "claim_level": "replay_plan_only",
        }
    )

    payload = json.loads(text)
    assert payload["task_id"] == "task_a"
    assert payload["claim_level"] == "replay_plan_only"


def test_render_decision_json_rejects_missing_rationale():
    with pytest.raises(ValidationError):
        render_decision_json({"task_id": "task_a", "status": "partial"})


def test_render_action_requests_is_stable_json():
    text = render_action_requests(
        {
            "task_id": "task_a",
            "action_requests": [
                {
                    "kind": "request_approval",
                    "missing_or_needed": "allow_model_file_patch",
                    "why_needed": "Model files are outside low-risk scope.",
                    "next_owner": "user",
                    "blocks": "execution",
                    "blocks_claim_not_execution": False,
                    "blocks_execution": True,
                    "suggested_next_step": "Approve allow_model_file_patch only if source edits are intended.",
                }
            ],
        }
    )

    payload = json.loads(text)
    assert payload["action_requests"][0]["missing_or_needed"] == "allow_model_file_patch"


def test_render_action_requests_rejects_vague_reason():
    with pytest.raises(ValidationError):
        render_action_requests(
            {
                "task_id": "task_a",
                "action_requests": [
                    {
                        "kind": "request_context",
                        "missing_or_needed": "project map",
                        "why_needed": "need more context",
                        "next_owner": "scheduler",
                        "blocks": "claim",
                        "blocks_claim_not_execution": True,
                        "blocks_execution": False,
                        "suggested_next_step": "Build project map.",
                    }
                ],
            }
        )


def test_render_worker_notes_uses_template():
    text = render_worker_notes(
        {
            "title": "Worker Notes",
            "summary": "Bounded judgment.",
            "bullets": ["No real execution.", "No patch applied."],
        }
    )

    assert text.startswith("# Worker Notes")
    assert "- No real execution." in text


def test_render_artifact_summary_lists_outputs():
    text = render_artifact_summary("task_a", ["decision.json", "worker_notes.md"], "Done.")

    assert "decision.json" in text
    assert "worker_notes.md" in text

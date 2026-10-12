from __future__ import annotations

import json

import pytest

from solution_agent.harness.model_dispatch_guard import (
    ModelDispatchDenied,
    admit_model_dispatch,
    complete_model_dispatch,
    model_request_fingerprint,
)
from solution_agent.harness.telemetry_contracts import PhaseBudget


def _fingerprint(suffix: str) -> str:
    return model_request_fingerprint(
        provider="codex",
        model="codex-cli",
        phase="execute",
        payload_sha256=(suffix * 64)[:64],
    )


def test_dispatch_guard_admits_once_and_persists_terminal_metadata(tmp_path) -> None:
    permit = admit_model_dispatch(
        tmp_path,
        run_id="run-1",
        provider="codex",
        model="codex-cli",
        phase="execute",
        fingerprint=_fingerprint("a"),
        budget=PhaseBudget(max_model_requests=1, max_context_estimated_tokens_per_request=100),
        context_estimated_tokens=80,
    )
    complete_model_dispatch(permit, status="succeeded", returncode=0)

    state = json.loads(permit.state_path.read_text(encoding="utf-8"))
    assert len(state["requests"]) == 1
    assert state["requests"][0]["status"] == "succeeded"
    assert state["requests"][0]["returncode"] == 0
    assert "prompt" not in json.dumps(state).lower()


def test_dispatch_guard_blocks_duplicate_request_before_provider(tmp_path) -> None:
    fingerprint = _fingerprint("b")
    admit_model_dispatch(
        tmp_path,
        run_id="run-2",
        provider="codex",
        model="codex-cli",
        phase="execute",
        fingerprint=fingerprint,
        budget=PhaseBudget(max_model_requests=3),
    )

    with pytest.raises(ModelDispatchDenied) as caught:
        admit_model_dispatch(
            tmp_path,
            run_id="run-2",
            provider="codex",
            model="codex-cli",
            phase="execute",
            fingerprint=fingerprint,
            budget=PhaseBudget(max_model_requests=3),
        )

    assert caught.value.reason == "duplicate_model_request"
    assert caught.value.decision["violations"] == ["duplicate_model_request_fingerprint"]


def test_dispatch_guard_enforces_request_and_context_budgets(tmp_path) -> None:
    with pytest.raises(ModelDispatchDenied) as context_block:
        admit_model_dispatch(
            tmp_path,
            run_id="run-3",
            provider="codex",
            model="codex-cli",
            phase="execute",
            fingerprint=_fingerprint("c"),
            budget=PhaseBudget(max_model_requests=1, max_context_estimated_tokens_per_request=50),
            context_estimated_tokens=51,
        )
    assert "context_estimated_tokens_per_request_exceeded" in " ".join(
        context_block.value.decision["violations"]
    )

    admit_model_dispatch(
        tmp_path,
        run_id="run-3",
        provider="codex",
        model="codex-cli",
        phase="execute",
        fingerprint=_fingerprint("d"),
        budget=PhaseBudget(max_model_requests=1),
    )
    with pytest.raises(ModelDispatchDenied) as request_block:
        admit_model_dispatch(
            tmp_path,
            run_id="run-3",
            provider="codex",
            model="codex-cli",
            phase="execute",
            fingerprint=_fingerprint("e"),
            budget=PhaseBudget(max_model_requests=1),
        )
    assert "model_requests_exceeded" in " ".join(request_block.value.decision["violations"])


def test_dispatch_guard_blocks_repeated_failure_fingerprint(tmp_path) -> None:
    failure = "f" * 64
    admit_model_dispatch(
        tmp_path,
        run_id="run-4",
        provider="codex",
        model="codex-cli",
        phase="diagnose",
        fingerprint=_fingerprint("1"),
        budget=PhaseBudget(max_model_requests=3, max_corrective_model_requests=3),
        corrective=True,
        failure_fingerprint=failure,
    )
    with pytest.raises(ModelDispatchDenied) as caught:
        admit_model_dispatch(
            tmp_path,
            run_id="run-4",
            provider="codex",
            model="codex-cli",
            phase="diagnose",
            fingerprint=_fingerprint("2"),
            budget=PhaseBudget(max_model_requests=3, max_corrective_model_requests=3),
            corrective=True,
            failure_fingerprint=failure,
        )
    assert caught.value.reason == "repeated_failure_fingerprint"

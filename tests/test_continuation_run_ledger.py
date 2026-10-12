from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import json

import pytest

from solution_agent.continuation import paths as continuation_paths
from solution_agent.continuation.ledger import (
    append_continuation_run_event,
    read_continuation_run_events,
    summarize_continuation_run_events,
)


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_append_writes_one_jsonl_event_locally(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)

    run_id = "run_ledger_append"
    result = append_continuation_run_event(
        run_id=run_id,
        stage="interpret",
        status="started",
        actor="continuation_planner",
        idempotency_key="run_ledger_append:interpret:001",
        source_task_id="task_001",
        reasons=["collect started"],
    )

    path = Path(result["path"])
    assert path == continuation_paths.continuation_run_ledger_path(run_id=run_id)
    assert path.parent == tmp_path / "continuation" / "run_ledger"
    assert result["written"] is True
    assert result["replayed"] is False
    assert path.exists()
    assert len(_read_lines(path)) == 1

    first = _read_lines(path)[0]
    assert first["schema_version"] == 1
    assert first["stage"] == "interpret"
    assert first["status"] == "started"
    assert first["actor"] == "continuation_planner"
    assert first["source_task_id"] == "task_001"


def test_read_returns_events_in_append_order(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_id = "run_ledger_read_order"

    first = append_continuation_run_event(
        run_id=run_id,
        stage="interpret",
        status="started",
        actor="continuation_planner",
        idempotency_key="run_ledger_read_order:interpret:001",
        reasons=["first"],
    )
    second = append_continuation_run_event(
        run_id=run_id,
        stage="plan",
        status="succeeded",
        actor="continuation_planner",
        idempotency_key="run_ledger_read_order:plan:001",
        reasons=["second"],
    )

    events = read_continuation_run_events(run_id=run_id)
    assert [event["event_id"] for event in events] == [first["event_id"], second["event_id"]]
    assert [event["stage"] for event in events] == ["interpret", "plan"]


def test_invalid_stage_or_status_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="invalid stage"):
        append_continuation_run_event(
            run_id="run_invalid_stage",
            stage="invalid_stage",
            status="started",
            actor="continuation_planner",
            idempotency_key="run_invalid_stage:invalid:001",
        )

    with pytest.raises(ValueError, match="invalid status"):
        append_continuation_run_event(
            run_id="run_invalid_status",
            stage="interpret",
            status="bad_status",
            actor="continuation_planner",
            idempotency_key="run_invalid_status:interpret:001",
        )



def test_forbidden_side_effect_true_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="forbidden side effect"):
        append_continuation_run_event(
            run_id="run_forbidden_side_effect",
            stage="plan",
            status="started",
            actor="continuation_planner",
            idempotency_key="run_forbidden_side_effect:plan:001",
            side_effects={"dispatch": True},
        )



def test_idempotent_replay_when_same_key_and_payload(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_id = "run_idempotent_replay"

    first = append_continuation_run_event(
        run_id=run_id,
        stage="plan",
        status="succeeded",
        actor="continuation_planner",
        idempotency_key="run_idempotent_replay:plan:001",
        reasons=["plan complete"],
        artifact_path="continuation/plans/plan.json",
    )
    second = append_continuation_run_event(
        run_id=run_id,
        stage="plan",
        status="succeeded",
        actor="continuation_planner",
        idempotency_key="run_idempotent_replay:plan:001",
        reasons=["plan complete"],
        artifact_path="continuation/plans/plan.json",
    )

    assert second["written"] is False
    assert second["replayed"] is True
    assert first["event_id"] != "" and second["event_id"] == first["event_id"]
    assert len(_read_lines(continuation_paths.continuation_run_ledger_path(run_id=run_id))) == 1


def test_idempotent_collision_with_same_key_and_different_payload_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_id = "run_idempotent_collision"

    append_continuation_run_event(
        run_id=run_id,
        stage="collect",
        status="started",
        actor="continuation_planner",
        idempotency_key="run_idempotent_collision:collect:001",
        reasons=["collect start"],
    )

    path = continuation_paths.continuation_run_ledger_path(run_id=run_id)
    first_lines = _read_lines(path)
    assert len(first_lines) == 1

    with pytest.raises(ValueError, match="idempotency key collision"):
        append_continuation_run_event(
            run_id=run_id,
            stage="collect",
            status="blocked",
            actor="continuation_planner",
            idempotency_key="run_idempotent_collision:collect:001",
            reasons=["different payload"],
        )

    assert len(_read_lines(path)) == 1
    assert _read_lines(path)[0] == first_lines[0]


def test_summarize_counts_statuses_stages_and_latest_blocking_reason(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_id = "run_summary"

    append_continuation_run_event(
        run_id=run_id,
        stage="interpret",
        status="started",
        actor="continuation_planner",
        idempotency_key="run_summary:interpret:001",
        reasons=["intake started"],
    )
    append_continuation_run_event(
        run_id=run_id,
        stage="plan",
        status="succeeded",
        actor="continuation_planner",
        idempotency_key="run_summary:plan:001",
        reasons=["plan ready"],
    )
    append_continuation_run_event(
        run_id=run_id,
        stage="plan",
        status="blocked",
        actor="continuation_planner",
        idempotency_key="run_summary:plan:002",
        reasons=["first block"],
    )
    append_continuation_run_event(
        run_id=run_id,
        stage="gate",
        status="manual_review",
        actor="continuation_planner",
        idempotency_key="run_summary:gate:001",
        reasons=["second block"],
    )

    summary = summarize_continuation_run_events(run_id=run_id)
    assert summary["count"] == 4
    assert summary["stage_counts"] == {"interpret": 1, "plan": 2, "gate": 1}
    assert summary["status_counts"] == {
        "started": 1,
        "succeeded": 1,
        "blocked": 1,
        "manual_review": 1,
    }
    assert summary["latest_blocking_reason"] == "second block"
    assert summary["latest_blocking_event_id"] == read_continuation_run_events(run_id=run_id)[-1]["event_id"]


def test_append_does_not_call_subprocess_or_network(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("socket.create_connection") as create_connection_mock,
    ):
        result = append_continuation_run_event(
            run_id="run_no_side_effect_runtime",
            stage="decision",
            status="succeeded",
            actor="continuation_planner",
            idempotency_key="run_no_side_effect_runtime:decision:001",
        )

    assert result["written"] is True
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from solution_agent.harness.wait_contracts import WaitEvent
from solution_agent.harness.wait_runtime import (
    apply_event,
    evaluate_stuck_breaker,
    initialize_wait,
    load_wait_state,
    record_progress,
    persist_event,
    synchronize_collection_result,
    synchronize_execution_state,
    synchronize_observation,
    wait_disposition,
)


def _payload_sha(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _running(job_dir: Path, job_id: str = "run-1") -> dict:
    job_dir.mkdir(parents=True, exist_ok=True)
    return {"job_id": job_id, "task_id": "task-1", "status": "running"}


def test_two_hour_wait_simulation_has_zero_model_dispatches(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    execution = _running(job_dir)
    initial = initialize_wait(job_dir, execution)

    # 120 deterministic minute markers model two hours without wall-clock sleep.
    for minute in range(1, 121):
        synchronize_execution_state(job_dir, execution)
        record_progress(
            job_dir,
            f"minute:{minute}",
            observed_at=initial.progress_observed_at + timedelta(minutes=minute),
        )

    state = load_wait_state(job_dir)
    disposition = wait_disposition(job_dir, state)
    assert state.lifecycle_state == "WAITING_EXTERNAL"
    assert state.model_dispatch_count == 0
    assert state.execution_submission_count == 1
    assert disposition["suppress_model_continuation"] is True
    assert disposition["next_model_action"] == "END_TURN"


def test_manual_host_resume_is_reported_honestly_without_blocking_finalization(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir))

    disposition = wait_disposition(job_dir, state, host_resume_mode="manual")

    assert disposition["host_resume_mode"] == "manual"
    assert disposition["manual_resume_required"] is True
    assert disposition["suppress_model_continuation"] is True
    assert disposition["next_model_action"] == "END_TURN"


def test_same_completion_event_delivered_ten_times_is_applied_once(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir))
    event = WaitEvent(
        event_id="completion-1",
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        state_version=state.state_version,
        kind="COMPLETION_CANDIDATE",
        payload_sha256=_payload_sha({"execution_status": "exited"}),
        payload={"execution_status": "exited"},
    )

    acknowledgements = [apply_event(job_dir, event) for _ in range(10)]

    assert acknowledgements[0].result == "applied"
    assert all(item.result == "duplicate" for item in acknowledgements[1:])
    reloaded = load_wait_state(job_dir)
    assert reloaded.lifecycle_state == "COLLECTING"
    assert reloaded.collect_transition_count == 1
    assert reloaded.applied_event_ids == ["completion-1"]


def test_restart_reloads_original_wait_without_resubmission(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    initialize_wait(job_dir, _running(job_dir))

    # Loading from disk represents a fresh controller process.
    reloaded = load_wait_state(job_dir)

    assert reloaded.lifecycle_state == "WAITING_EXTERNAL"
    assert reloaded.execution_submission_count == 1
    assert reloaded.model_dispatch_count == 0


def test_observation_loss_and_restore_never_resubmits_execution(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    initialize_wait(job_dir, _running(job_dir))

    unavailable = synchronize_observation(job_dir, available=False, detail="ssh timeout")
    restored = synchronize_observation(job_dir, available=True)

    assert unavailable.observation_status == "unavailable"
    assert unavailable.lifecycle_state == "WAITING_EXTERNAL"
    assert restored.observation_status == "available"
    assert restored.execution_submission_count == 1
    assert restored.model_dispatch_count == 0


def test_incomplete_collection_requires_review_but_complete_negative_result_closes(tmp_path: Path) -> None:
    incomplete_dir = tmp_path / "incomplete"
    initialize_wait(incomplete_dir, _running(incomplete_dir, "incomplete-run"))
    synchronize_execution_state(
        incomplete_dir,
        {
            "job_id": "incomplete-run",
            "task_id": "task-1",
            "status": "exited",
            "finished_at": "2026-09-28T00:00:00+00:00",
        },
    )
    incomplete = synchronize_collection_result(
        incomplete_dir,
        {"status": "artifact_missing", "collected_at": "2026-09-28T00:01:00+00:00"},
    )
    assert incomplete.lifecycle_state == "REVIEW_REQUIRED"
    assert incomplete.evidence_status == "incomplete"

    negative_dir = tmp_path / "negative"
    initialize_wait(negative_dir, _running(negative_dir, "negative-run"))
    synchronize_execution_state(
        negative_dir,
        {
            "job_id": "negative-run",
            "task_id": "task-1",
            "status": "exited",
            "finished_at": "2026-09-28T00:00:00+00:00",
        },
    )
    negative = synchronize_collection_result(
        negative_dir,
        {
            "status": "succeeded",
            "collected_at": "2026-09-28T00:01:00+00:00",
            "metrics_summary": {"science_status": "negative"},
        },
    )
    assert negative.lifecycle_state == "CLOSED"
    assert negative.evidence_status == "complete"
    assert negative.scientific_status == "hypothesis_not_supported"


def test_event_and_ack_are_durable_json_artifacts(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir))
    event = WaitEvent(
        event_id="decision-1",
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        state_version=state.state_version,
        kind="DECISION_REQUIRED",
        payload_sha256=_payload_sha({"detail": "scientific interpretation required"}),
        payload={"detail": "scientific interpretation required"},
    )
    ack = apply_event(job_dir, event)

    event_files = list((job_dir / "wait" / "events").glob("*.json"))
    ack_files = list((job_dir / "wait" / "acks").glob("*.json"))
    assert len(event_files) == 1
    assert len(ack_files) == 1
    assert json.loads(ack_files[0].read_text(encoding="utf-8"))["event_id"] == event.event_id
    assert ack.wake_required is True
    assert ack.model_dispatch_allowed is False
    receipt = json.loads((job_dir / "wait" / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["last_event_kind"] == "DECISION_REQUIRED"
    assert receipt["route"] == {
        "route": "human",
        "reason": "review_required_without_declared_model_route",
    }
    assert receipt["counters"]["model_dispatches"] == 0
    assert (job_dir / "wait" / "HANDOFF.md").is_file()


def test_stuck_breaker_fires_once_only_with_fresh_observation(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir), stall_after_seconds=60)
    tripped = evaluate_stuck_breaker(job_dir, observed_at=state.progress_observed_at + timedelta(seconds=61))
    repeated = evaluate_stuck_breaker(job_dir, observed_at=state.progress_observed_at + timedelta(seconds=120))

    assert tripped.lifecycle_state == "REVIEW_REQUIRED"
    assert tripped.stalled_event_emitted is True
    assert repeated.applied_event_ids == tripped.applied_event_ids
    stalled = [event_id for event_id in repeated.applied_event_ids if event_id.startswith("stalled:")]
    assert len(stalled) == 1

    unavailable_dir = tmp_path / "unavailable"
    unavailable = initialize_wait(unavailable_dir, _running(unavailable_dir, "unavailable-run"), stall_after_seconds=60)
    synchronize_observation(unavailable_dir, available=False, detail="ssh timeout")
    not_tripped = evaluate_stuck_breaker(
        unavailable_dir,
        observed_at=unavailable.progress_observed_at + timedelta(seconds=120),
    )
    assert not_tripped.lifecycle_state == "WAITING_EXTERNAL"
    assert not_tripped.stalled_event_emitted is False


def test_crash_before_publish_rebuilds_and_applies_event_once(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir))
    event = WaitEvent(
        event_id="completion-before-publish",
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        state_version=state.state_version,
        kind="COMPLETION_CANDIDATE",
        payload_sha256=_payload_sha({"execution_status": "exited"}),
        payload={"execution_status": "exited"},
    )
    # No durable event exists at the crash point. The deterministic producer rebuilds it.
    assert list((job_dir / "wait" / "events").glob("*.json")) == []
    acknowledgement = apply_event(job_dir, event)
    assert acknowledgement.result == "applied"
    assert load_wait_state(job_dir).collect_transition_count == 1


def test_crash_after_publish_before_ack_redelivery_finishes_once(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    state = initialize_wait(job_dir, _running(job_dir))
    event = WaitEvent(
        event_id="completion-after-publish",
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        state_version=state.state_version,
        kind="COMPLETION_CANDIDATE",
        payload_sha256=_payload_sha({"execution_status": "exited"}),
        payload={"execution_status": "exited"},
    )
    persist_event(job_dir, event)
    acknowledgement = apply_event(job_dir, event)
    assert acknowledgement.result == "applied"
    ack_path = next((job_dir / "wait" / "acks").glob("*.json"))
    ack_path.unlink()  # emulate state commit followed by crash before ack survives
    replay = apply_event(job_dir, event)
    assert replay.result == "duplicate"
    assert load_wait_state(job_dir).collect_transition_count == 1

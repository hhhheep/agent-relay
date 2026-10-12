from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from solution_agent.harness.task_capsule import (
    CapsuleConflict,
    ModelBudget,
    ScopeContract,
    TaskCapsule,
    TaskCapsuleStore,
    WaitRef,
    WriterLeaseRef,
)


def capsule(tmp_path) -> TaskCapsule:
    return TaskCapsule(
        task_id="focus-test",
        objective="keep one bounded work unit active",
        source_release_id="release-1",
        approval_owner="user",
        scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
        model_budget=ModelBudget(max_context_bytes=4096, max_output_bytes=2048),
    )


def test_capsule_cas_rejects_stale_version_and_second_work_unit(tmp_path) -> None:
    store = TaskCapsuleStore(tmp_path / "state")
    created = store.create(capsule(tmp_path))
    active = store.start_work_unit(created.task_id, "unit-1", expected_version=0)
    with pytest.raises(CapsuleConflict):
        store.start_work_unit(created.task_id, "unit-2", expected_version=active.version)
    with pytest.raises(CapsuleConflict):
        store.finish_work_unit(created.task_id, "unit-1", expected_version=0)


def test_capsule_writer_lease_conflict_and_restart_recovery(tmp_path) -> None:
    state = tmp_path / "state"
    store = TaskCapsuleStore(state)
    created = store.create(capsule(tmp_path))
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    first = store.acquire_writer_lease(
        created.task_id,
        WriterLeaseRef(
            lease_id="lease-1",
            target=str(tmp_path / "target.md"),
            owner_work_unit="unit-1",
            expires_at=expiry,
        ),
        expected_version=0,
    )
    with pytest.raises(CapsuleConflict):
        store.acquire_writer_lease(
            created.task_id,
            WriterLeaseRef(
                lease_id="lease-2",
                target=str(tmp_path / "target.md"),
                owner_work_unit="unit-2",
                expires_at=expiry,
            ),
            expected_version=first.version,
        )
    waiting = store.set_wait(
        created.task_id,
        WaitRef(
            wait_handle="wait:abc",
            run_id="run-1",
            attempt_id="run-1:1",
            state_version=2,
            resume_trigger=["completed"],
            manual_resume_required=True,
        ),
        expected_version=first.version,
    )
    restored = TaskCapsuleStore(state).load(created.task_id)
    assert restored.wait == waiting.wait
    assert restored.next_material_event is None
    assert restored.writer_leases == waiting.writer_leases


def test_capsule_allows_only_one_active_writer_even_for_different_targets(tmp_path) -> None:
    store = TaskCapsuleStore(tmp_path / "state")
    created = store.create(capsule(tmp_path))
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    first = store.acquire_writer_lease(
        created.task_id,
        WriterLeaseRef(
            lease_id="lease-1",
            target=str(tmp_path / "first.md"),
            owner_work_unit="unit-1",
            expires_at=expiry,
        ),
        expected_version=0,
    )
    with pytest.raises(CapsuleConflict, match="active writer lease"):
        store.acquire_writer_lease(
            created.task_id,
            WriterLeaseRef(
                lease_id="lease-2",
                target=str(tmp_path / "second.md"),
                owner_work_unit="unit-1",
                expires_at=expiry,
            ),
            expected_version=first.version,
        )


def test_background_event_does_not_replace_active_work_unit(tmp_path) -> None:
    store = TaskCapsuleStore(tmp_path / "state")
    store.create(capsule(tmp_path))
    active = store.start_work_unit("focus-test", "unit-1", expected_version=0)
    queued = store.queue_event("focus-test", "event-1", expected_version=active.version)
    assert queued.active_work_unit == "unit-1"
    assert queued.queued_events == ["event-1"]
    assert queued.next_material_event == "event-1"

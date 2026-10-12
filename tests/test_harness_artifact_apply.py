from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from solution_agent.harness.artifact_apply import ApplyError, ArtifactApplyEngine, WriteConflict
from solution_agent.harness.artifact_runtime import ArtifactStore
from solution_agent.harness.task_capsule import ScopeContract, TaskCapsule, TaskCapsuleStore, WriterLeaseRef


def setup_runtime(tmp_path):
    state = tmp_path / "state"
    capsules = TaskCapsuleStore(state)
    capsule = capsules.create(
        TaskCapsule(
            task_id="apply-test",
            objective="atomic apply",
            source_release_id="release-1",
            approval_owner="user",
            scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
        )
    )
    artifacts = ArtifactStore(state)
    return capsule, capsules, artifacts, ArtifactApplyEngine(state, artifacts, capsules)


def test_materialize_plan_commit_and_idempotent_receipt(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "target.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    source = artifacts.put_bytes(
        task,
        io.BytesIO(b'{"new": true}\n'),
        media_type="application/json",
        source_kind="generated",
        source_ref="test",
    )
    lease_task = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-1",
            target=str(target),
            base_sha256=before,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    plan = engine.plan_materialize(lease_task, source.artifact_id, str(target), before)
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    receipt = engine.commit(lease_task, plan.plan_id, "lease-1")
    assert target.read_text(encoding="utf-8") == '{"new": true}\n'
    assert receipt.after_sha256 == source.sha256
    assert engine.commit(capsules.load(task.task_id), plan.plan_id, "lease-1") == receipt
    assert [item.artifact_id for item in capsules.load(task.task_id).working_artifacts] == [source.artifact_id]


def test_materialize_large_artifact_by_hash_without_echoing_content(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "large-payload.txt"
    payload = (("exact-transfer-\u50b3\u8f38-" + "x" * 211) * 100).encode("utf-8")
    assert len(payload) > 20_000
    source = artifacts.put_bytes(
        task,
        io.BytesIO(payload),
        media_type="text/plain",
        source_kind="generated",
        source_ref="large-transfer-test",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-large",
            target=str(target),
            base_sha256=None,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    plan = engine.plan_materialize(leased, source.artifact_id, str(target), None)
    receipt = engine.commit(leased, plan.plan_id, "lease-large")

    assert target.read_bytes() == payload
    assert receipt.after_sha256 == hashlib.sha256(payload).hexdigest() == source.sha256
    model_facing = plan.model_dump_json() + receipt.model_dump_json()
    assert "exact-transfer-\u50b3\u8f38" not in model_facing
    assert len(model_facing.encode("utf-8")) < 5_000


def test_commit_rejects_target_changed_after_plan(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    source = artifacts.put_bytes(
        task,
        io.BytesIO(b"new\n"),
        media_type="text/plain",
        source_kind="generated",
        source_ref="test",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-1",
            target=str(target),
            base_sha256=before,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=0,
    )
    plan = engine.plan_materialize(leased, source.artifact_id, str(target), before)
    target.write_text("raced\n", encoding="utf-8")
    with pytest.raises(WriteConflict, match="changed after plan"):
        engine.commit(leased, plan.plan_id, "lease-1")
    assert target.read_text(encoding="utf-8") == "raced\n"


def test_patch_plan_uses_temporary_tree_and_changes_only_target(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("old\n", encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    patch = artifacts.put_bytes(
        task,
        io.BytesIO(b"--- a/note.md\n+++ b/note.md\n@@ -1 +1 @@\n-old\n+new\n"),
        media_type="text/x-diff",
        source_kind="patch",
        source_ref="test",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-1",
            target=str(target),
            base_sha256=before,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=0,
    )
    plan = engine.plan_patch(leased, patch.artifact_id, str(target), before)
    assert target.read_text() == "old\n"
    engine.commit(leased, plan.plan_id, "lease-1")
    assert target.read_text() == "new\n"


def test_compose_three_selectors_and_registered_literal_matches_golden(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "report.md"
    markdown = artifacts.put_bytes(
        task,
        io.BytesIO(b"# Summary\nfocused result\n# Appendix\nignore\n"),
        media_type="text/markdown",
        source_kind="generated",
        source_ref="summary.md",
    )
    metrics = artifacts.put_bytes(
        task,
        io.BytesIO(b'{"metrics":{"accuracy":0.9},"raw":"ignore"}'),
        media_type="application/json",
        source_kind="generated",
        source_ref="metrics.json",
    )
    notes = artifacts.put_bytes(
        task,
        io.BytesIO(b"ignore\nselected note\nignore\n"),
        media_type="text/plain",
        source_kind="generated",
        source_ref="notes.txt",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-compose",
            target=str(target),
            base_sha256=None,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    plan = engine.plan_compose(
        leased,
        {
            "parts": [
                {
                    "artifact_id": markdown.artifact_id,
                    "selector": {"type": "markdown_heading", "value": "Summary"},
                },
                {"literal_template_id": "report-divider-v1"},
                {
                    "artifact_id": metrics.artifact_id,
                    "selector": {"type": "json_pointer", "value": "/metrics"},
                    "separator": "blank_line",
                },
                {
                    "artifact_id": notes.artifact_id,
                    "selector": {"type": "line_range", "value": {"start": 2, "end": 2}},
                },
            ]
        },
        str(target),
        None,
    )
    receipt = engine.commit(leased, plan.plan_id, "lease-compose")
    expected = '# Summary\nfocused result\n\n---\n\n{\n  "accuracy": 0.9\n}\n\nselected note'
    assert target.read_text(encoding="utf-8") == expected
    assert receipt.after_sha256 == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert receipt.rollback_path is None
    assert receipt.rollback_policy == "disabled_no_backup"


def test_invalid_candidate_is_preserved_but_never_published(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "target.json"
    target.write_text('{"canonical": true}\n', encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    invalid = artifacts.put_bytes(
        task,
        io.BytesIO(b'{"broken":'),
        media_type="application/json",
        source_kind="generated",
        source_ref="invalid.json",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-invalid",
            target=str(target),
            base_sha256=before,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    plan = engine.plan_materialize(leased, invalid.artifact_id, str(target), before)
    with pytest.raises(ApplyError, match="not published"):
        engine.commit(leased, plan.plan_id, "lease-invalid")
    assert target.read_text(encoding="utf-8") == '{"canonical": true}\n'
    failed = engine._plan_dir(task.task_id, plan.plan_id) / "failed_validation.json"
    assert failed.is_file()
    assert '"canonical_target_published": false' in failed.read_text(encoding="utf-8")


def test_commit_recovers_after_atomic_replace_before_receipt(tmp_path) -> None:
    task, capsules, artifacts, engine = setup_runtime(tmp_path)
    target = tmp_path / "recover.txt"
    target.write_text("old\n", encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    source = artifacts.put_bytes(
        task,
        io.BytesIO(b"new\n"),
        media_type="text/plain",
        source_kind="generated",
        source_ref="recovery-test",
    )
    leased = capsules.acquire_writer_lease(
        task.task_id,
        WriterLeaseRef(
            lease_id="lease-recovery",
            target=str(target),
            base_sha256=before,
            owner_work_unit="unit-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    plan = engine.plan_materialize(leased, source.artifact_id, str(target), before)
    candidate = engine._plan_dir(task.task_id, plan.plan_id) / "candidate.bin"
    target.write_bytes(candidate.read_bytes())  # emulate crash after os.replace, before receipt
    receipt = engine.commit(leased, plan.plan_id, "lease-recovery")
    assert receipt.recovered_after_replace is True
    assert receipt.before_sha256 == before
    assert receipt.after_sha256 == source.sha256
    assert capsules.load(task.task_id).writer_leases == []

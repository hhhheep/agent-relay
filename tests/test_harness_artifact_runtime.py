from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from solution_agent.harness.artifact_runtime import ArtifactAccessError, ArtifactStore, Selector
from solution_agent.harness.task_capsule import CapsuleConflict, ScopeContract, TaskCapsule


def task(tmp_path, task_id: str = "task-a") -> TaskCapsule:
    return TaskCapsule(
        task_id=task_id,
        objective="select only required evidence",
        source_release_id="release-1",
        approval_owner="user",
        scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
    )


def test_cas_deduplicates_content_and_isolates_task_manifests(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# Evidence\nalpha\n", encoding="utf-8")
    second.write_bytes(first.read_bytes())
    manifest_a = store.put_path(task(tmp_path), first, alias="evidence")
    manifest_same = store.put_path(task(tmp_path), second)
    assert manifest_a.artifact_id == manifest_same.artifact_id
    assert len(list((store.root / "objects" / "sha256").glob("*/*"))) == 1
    with pytest.raises(ArtifactAccessError):
        store.describe(task(tmp_path, "task-b"), manifest_a.artifact_id)


def test_selective_read_receipt_replays_no_content(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    source = tmp_path / "large.md"
    source.write_text("\n".join(f"line {index}" for index in range(1, 1001)), encoding="utf-8")
    manifest = store.put_path(task(tmp_path), source)
    selector = Selector(type="line_range", value={"start": 500, "end": 520})
    first = store.read(task(tmp_path), manifest.artifact_id, selector)
    second = store.read(task(tmp_path), manifest.artifact_id, selector)
    assert first["content"].splitlines() == [f"line {index}" for index in range(500, 521)]
    assert first["returned_bytes"] < source.stat().st_size
    assert second == {
        "unchanged": True,
        "read_receipt_id": first["read_receipt_id"],
        "content_replayed": False,
        "returned_bytes": 0,
        "truncated": False,
    }


def test_alias_compare_and_swap_and_changed_content_gets_new_receipt(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    current_task = task(tmp_path)
    source = tmp_path / "data.json"
    source.write_text('{"value": 1}', encoding="utf-8")
    first = store.put_path(current_task, source, alias="current")
    with pytest.raises(CapsuleConflict):
        store.update_alias(current_task, "current", first.artifact_id, expected_version=2)
    receipt_a = store.read(current_task, "current", Selector(type="json_pointer", value="/value"))
    source.write_text('{"value": 2}', encoding="utf-8")
    second = store.put_path(current_task, source, alias="next")
    receipt_b = store.read(current_task, second.artifact_id, Selector(type="json_pointer", value="/value"))
    assert first.artifact_id != second.artifact_id
    assert receipt_a["read_receipt_id"] != receipt_b["read_receipt_id"]


def test_binary_empty_cjk_secret_and_symlink_boundaries(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    current_task = task(tmp_path)
    empty = store.put_bytes(
        current_task,
        io.BytesIO(b""),
        media_type="application/octet-stream",
        source_kind="generated",
        source_ref="empty",
    )
    cjk = store.put_bytes(
        current_task,
        io.BytesIO("繁體中文\n".encode()),
        media_type="text/plain",
        source_kind="generated",
        source_ref="cjk",
    )
    assert empty.byte_size == 0
    assert store.read(current_task, cjk.artifact_id, Selector(type="whole"))["content"] == "繁體中文\n"
    with pytest.raises(ArtifactAccessError):
        store.put_bytes(
            current_task,
            io.BytesIO(b"api_key=" + b"sk-" + b"fake-supersecretvalue123456"),
            media_type="text/plain",
            source_kind="generated",
            source_ref="secret",
        )
    assert not list((store.root / "objects" / ".tmp").glob("*.tmp"))
    target = tmp_path / "target.txt"
    target.write_text("ok", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(ArtifactAccessError):
        store.put_path(current_task, link)


def test_yaml_table_regex_and_remote_hash_negotiation(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    current_task = task(tmp_path)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("root:\n  items:\n    - one\n    - two\n", encoding="utf-8")
    yaml_artifact = store.put_path(current_task, yaml_path)
    yaml_result = store.read(current_task, yaml_artifact.artifact_id, Selector(type="yaml_path", value="root.items.1"))
    assert yaml_result["content"] == "two\n..."

    csv_path = tmp_path / "table.csv"
    csv_path.write_text("id,kind,value\n1,a,10\n2,b,20\n3,a,30\n", encoding="utf-8")
    csv_artifact = store.put_path(current_task, csv_path)
    table = store.read(
        current_task,
        csv_artifact.artifact_id,
        Selector(
            type="table_slice",
            value={"columns": ["id", "value"], "start": 1, "end": 3, "filter": {"kind": "a"}},
        ),
    )
    assert table["content"] == "id,value\n1,10\n3,30"
    regex = store.read(
        current_task,
        csv_artifact.artifact_id,
        Selector(type="search", value={"query": r"^[23],", "mode": "regex"}),
    )
    assert regex["content"].splitlines() == ["3:2,b,20", "4:3,a,30"]
    assert store.missing_hashes([csv_artifact.sha256, "f" * 64]) == ["f" * 64]


def test_large_selectors_return_only_requested_content_and_whole_requires_reason(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state", object_max_bytes=32 * 1024 * 1024)
    current_task = task(tmp_path)
    log_path = tmp_path / "large.log"
    line = "x" * 90
    log_path.write_text("\n".join(f"{index:06d}:{line}" for index in range(100000)), encoding="utf-8")
    manifest = store.put_path(current_task, log_path)
    selected = store.read(current_task, manifest.artifact_id, Selector(type="line_range", value={"start": 50000, "end": 50019}))
    assert len(selected["content"].splitlines()) == 20
    assert selected["returned_bytes"] < 4096
    with pytest.raises(ValueError, match="requires override"):
        store.read(current_task, manifest.artifact_id, Selector(type="whole"))

    large_json = tmp_path / "large.json"
    large_json.write_text(json.dumps({"noise": [0] * 200000, "wanted": {"answer": 42}}), encoding="utf-8")
    json_artifact = store.put_path(current_task, large_json)
    pointer = store.read(current_task, json_artifact.artifact_id, Selector(type="json_pointer", value="/wanted"))
    assert pointer["content"] == '{\n  "answer": 42\n}'


def test_selective_read_budget_covers_10mb_log_100k_source_and_large_csv(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state", object_max_bytes=32 * 1024 * 1024)
    current_task = task(tmp_path)

    log_path = tmp_path / "ten-megabytes.log"
    log_path.write_text("x" * (10 * 1024 * 1024 + 1), encoding="utf-8")
    log_artifact = store.put_path(current_task, log_path)
    log_match = store.read(
        current_task,
        log_artifact.artifact_id,
        Selector(type="search", value={"query": "x" * 32, "max_matches": 1}),
        max_output_bytes=1024,
    )
    assert log_path.stat().st_size > 10 * 1024 * 1024
    assert log_match["returned_bytes"] <= 1024

    source_path = tmp_path / "hundred_thousand.py"
    source_path.write_text("".join(f"VALUE_{index} = {index}\n" for index in range(100000)), encoding="utf-8")
    source_artifact = store.put_path(current_task, source_path)
    source_slice = store.read(
        current_task,
        source_artifact.artifact_id,
        Selector(type="line_range", value={"start": 99990, "end": 100000}),
    )
    assert len(source_slice["content"].splitlines()) == 11
    assert source_slice["returned_bytes"] < 1024

    csv_path = tmp_path / "large.csv"
    csv_path.write_text(
        "id,kind,value\n" + "".join(f"{index},{'selected' if index == 87654 else 'noise'},{index * 2}\n" for index in range(100000)),
        encoding="utf-8",
    )
    csv_artifact = store.put_path(current_task, csv_path)
    table = store.read(
        current_task,
        csv_artifact.artifact_id,
        Selector(
            type="table_slice",
            value={
                "columns": ["id", "value"],
                "start": 87000,
                "end": 87999,
                "filter": {"kind": "selected"},
            },
        ),
    )
    assert table["content"] == "id,value\n87654,175308"
    assert table["returned_bytes"] < 1024


def test_scope_credential_traversal_and_alias_swap_are_rejected(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    current_task = TaskCapsule(
        task_id="scope-test",
        objective="negative scope checks",
        source_release_id="release-1",
        approval_owner="user",
        scope=ScopeContract(readable_paths=[str(allowed)], writable_paths=[str(allowed)]),
    )
    store = ArtifactStore(tmp_path / "state")
    outside_file = outside / "data.txt"
    outside_file.write_text("outside", encoding="utf-8")
    with pytest.raises(ArtifactAccessError, match="outside task readable scope"):
        store.put_path(current_task, outside_file)

    credential = allowed / "credentials.json"
    credential.write_text('{"account":"redacted fixture"}', encoding="utf-8")
    with pytest.raises(ArtifactAccessError, match="credential file"):
        store.put_path(current_task, credential)

    safe = allowed / "safe.txt"
    safe.write_text("one", encoding="utf-8")
    first = store.put_path(current_task, safe, alias="current")
    safe.write_text("two", encoding="utf-8")
    second = store.put_path(current_task, safe)
    with pytest.raises(CapsuleConflict, match="alias expected version"):
        store.update_alias(current_task, "current", second.artifact_id, expected_version=-1)
    assert store.resolve(current_task, "current").artifact_id == first.artifact_id
    with pytest.raises(ValueError, match="unsupported characters"):
        store.update_alias(current_task, "../escape", second.artifact_id, expected_version=None)


def test_task_global_quota_and_gc_are_deterministic_and_non_destructive(tmp_path) -> None:
    current_task = task(tmp_path)
    task_limited = ArtifactStore(tmp_path / "task-state", task_max_bytes=4, global_soft_max_bytes=100)
    task_limited.put_bytes(
        current_task,
        io.BytesIO(b"1234"),
        media_type="text/plain",
        source_kind="generated",
        source_ref="quota-1",
    )
    with pytest.raises(ValueError, match="task quota"):
        task_limited.put_bytes(
            current_task,
            io.BytesIO(b"5"),
            media_type="text/plain",
            source_kind="generated",
            source_ref="quota-2",
        )

    global_limited = ArtifactStore(tmp_path / "global-state", task_max_bytes=100, global_soft_max_bytes=4)
    global_limited.put_bytes(
        current_task,
        io.BytesIO(b"1234"),
        media_type="text/plain",
        source_kind="generated",
        source_ref="quota-1",
    )
    with pytest.raises(ValueError, match="global soft quota"):
        global_limited.put_bytes(
            current_task,
            io.BytesIO(b"different"),
            media_type="text/plain",
            source_kind="generated",
            source_ref="quota-2",
        )
    status = global_limited.quota_status(current_task.task_id)
    assert status["task_bytes"] == 4
    assert status["global_bytes"] == 4
    proposal = global_limited.gc_proposal(retention_before=datetime.now(timezone.utc) + timedelta(days=1))
    assert proposal == {"delete_performed": False, "candidates": []}

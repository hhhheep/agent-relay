from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation.manual_dispatch import create_manual_dispatch_request


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _write_active_task(
    root: Path,
    *,
    task_id: str,
    continuation: dict | None,
    body: str = "Safe continuation body.",
    front_updates: dict | None = None,
    missing_front: bool = False,
) -> Path:
    inbox = root / "tasks" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{task_id}.md"

    if missing_front:
        path.write_text(body, encoding="utf-8")
        return path

    front = {
        "task_id": task_id,
        "source_task_id": "source_001",
        "source_result_hash": "hash_001",
        "continuation": continuation,
        "worker": "small_task_worker",
        "line": "A_continuation_line",
        "batch": "continuation_batch",
        "dag_root": "continuation_root",
        "prefix": "cont_",
        "resource_hint": "cpu",
    }
    if front_updates:
        front.update(front_updates)

    front_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(f"---\n{front_text}---\n{body}", encoding="utf-8")
    return path


def _request_files(root: Path) -> list[Path]:
    return list((root / "continuation" / "dispatch_requests").glob("*.json"))


def _read_request(root: Path) -> dict:
    paths = _request_files(root)
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _assert_no_request(root: Path) -> None:
    assert not (root / "continuation" / "dispatch_requests").exists()


def test_request_not_created_when_task_is_not_admissible(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_not_admissible",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": False,
            "scheduler_admission": {"status": "passed"},
        },
    )

    result = create_manual_dispatch_request(path, actor="human")

    assert result["status"] == "blocked"
    assert result["request_path"].endswith(".json")
    assert result["created"] is False
    _assert_no_request(tmp_path)


def test_request_helper_reads_fresh_admission_each_call(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_freshness",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    first = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")
    assert first["status"] == "request_created"
    assert first["created"] is True
    snapshot = first["request_path"]
    payload = json.loads(Path(snapshot).read_text(encoding="utf-8"))

    _write_active_task(
        tmp_path,
        task_id="task_freshness",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": False,
            "scheduler_admission": {"status": "passed"},
        },
    )
    second = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")

    assert second["status"] in {"blocked", "disabled"}
    assert json.loads(Path(snapshot).read_text(encoding="utf-8")) == payload
    assert not Path(second["request_path"]).exists()
    assert second["request_path"] != snapshot
    assert len(_request_files(tmp_path)) == 1
    assert _read_request(tmp_path) == payload


def test_worker_mismatch_blocks_request_creation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_worker_mismatch_request",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": True,
            "scheduler_admission": {"status": "passed"},
            "worker": "small_task_worker",
        },
        front_updates={"worker": "small_task_worker"},
    )
    result = create_manual_dispatch_request(
        path,
        actor="human",
        requested_worker="materialized_execution",
        scope={"line": "A_continuation_line", "batch": "continuation_batch", "dag_root": "continuation_root", "prefix": "cont_"},
    )

    assert result["status"] == "blocked"
    assert "worker_scope_mismatch" in result["reasons"]
    _assert_no_request(tmp_path)


def test_scope_mismatch_blocks_request_creation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_scope_mismatch_request",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": True,
            "scheduler_admission": {"status": "passed"},
        },
    )
    result = create_manual_dispatch_request(
        path,
        actor="human",
        requested_worker="small_task_worker",
        scope={"line": "wrong_line", "batch": "continuation_batch", "dag_root": "continuation_root", "prefix": "cont_"},
    )

    assert result["status"] == "blocked"
    assert "line_scope_mismatch" in result["reasons"]
    _assert_no_request(tmp_path)


def test_dispatch_allowed_false_block_request(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_dispatch_false_request",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": False,
            "scheduler_admission": {"status": "passed"},
        },
    )

    result = create_manual_dispatch_request(path, actor="human")
    assert result["status"] == "blocked"
    assert result["request_path"].endswith(".json")
    assert not _request_files(tmp_path)

def test_missing_scheduler_admission_block_request(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_scheduler_missing_request",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True},
    )
    result = create_manual_dispatch_request(path, actor="human")
    assert result["status"] == "blocked"
    assert not _request_files(tmp_path)
    assert "scheduler_admission_missing" in result["reasons"]


def test_failed_scheduler_admission_block_request(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_scheduler_failed_request",
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": True,
            "scheduler_admission": {"status": "failed"},
        },
    )
    result = create_manual_dispatch_request(path, actor="human")
    assert result["status"] == "blocked"
    assert not _request_files(tmp_path)
    assert "scheduler_admission_not_passed" in result["reasons"]


def test_admissible_task_creates_local_request_under_dispatch_requests(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_request_created",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    result = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")

    assert result["status"] == "request_created"
    assert result["created"] is True
    assert Path(result["request_path"]).parent == tmp_path / "continuation" / "dispatch_requests"
    assert len(_request_files(tmp_path)) == 1

    request = _read_request(tmp_path)
    assert request["request_id"] == result["request_id"]


def test_request_side_effects_are_all_false(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_side_effects",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    result = create_manual_dispatch_request(path, actor="human")
    side_effects = result["side_effects"]

    assert side_effects["execution"] is False
    assert side_effects["registry_mutation"] is False
    assert side_effects["remote_execution"] is False
    assert side_effects["running_state"] is False
    assert side_effects["subprocess"] is False


def test_request_creation_has_no_registry_running_remote_or_subprocess_markers(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    registry = tmp_path / "registry.json"
    registry.write_text("{\"tasks\": {}}", encoding="utf-8")

    path = _write_active_task(
        tmp_path,
        task_id="task_no_markers",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    before = registry.read_text(encoding="utf-8")
    create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")

    assert registry.read_text(encoding="utf-8") == before
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "continuation" / "running").exists()


def test_request_idempotent_replay_returns_noop(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_idempotent",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    first = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker", scope={"line": "A_continuation_line"})
    second = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker", scope={"line": "A_continuation_line"})

    assert first["status"] == "request_created"
    assert second["status"] == "request_created"
    assert second["created"] is False
    assert second["replayed"] is True
    assert first["request_id"] == second["request_id"]
    assert first["request_path"] == second["request_path"]
    assert _read_request(tmp_path)["request_id"] == first["request_id"]


def test_request_same_idempotency_key_different_content_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_idempotency_collision",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    first = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")
    request_path = Path(first["request_path"])
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    request_payload["actor"] = "different_actor"
    request_path.write_text(json.dumps(request_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    second = create_manual_dispatch_request(path, actor="human", requested_worker="small_task_worker")
    assert second["status"] == "blocked"
    assert "request_id_collision" in second["reasons"]
    assert json.loads(request_path.read_text(encoding="utf-8"))["actor"] == "different_actor"


def test_requested_worker_is_recorded_filter_only_and_declared_worker_not_mutated(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    task_id = "task_worker_recorded"
    path = _write_active_task(
        tmp_path,
        task_id=task_id,
        continuation={
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": True,
            "scheduler_admission": {"status": "passed"},
            "worker": "small_task_worker",
        },
        front_updates={"worker": "small_task_worker"},
    )

    result = create_manual_dispatch_request(
        path,
        actor="human",
        requested_worker="small_task_worker",
        scope={"line": "A_continuation_line", "batch": "continuation_batch", "dag_root": "continuation_root", "prefix": "cont_"},
    )
    assert result["status"] == "request_created"
    assert result["requested_worker"] == "small_task_worker"
    request = _read_request(tmp_path)
    assert request["requested_worker"] == "small_task_worker"
    assert request["declared_worker"] == "small_task_worker"



def test_no_direct_scheduler_dispatch_calls_from_request_helper(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_no_dispatch_call",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    with (
        patch("solution_agent.orchestrator.core.dispatch") as dispatch_mock,
        patch("solution_agent.orchestrator.core.run_queue") as run_queue_mock,
    ):
        create_manual_dispatch_request(path, actor="human")

    assert not dispatch_mock.called
    assert not run_queue_mock.called

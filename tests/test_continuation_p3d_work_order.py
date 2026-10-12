from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from solution_agent.continuation import remote_execution
from solution_agent.continuation.remote_execution import create_remote_execution_work_order_dry_run


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return path


def _dispatch_request(tmp_path: Path, *, request_id: str = "req_p3d", overrides: dict | None = None) -> tuple[Path, dict]:
    payload = {
        "request_id": request_id,
        "task_id": "task_p3d",
        "active_inbox_path": str(tmp_path / "tasks" / "inbox" / "task_p3d.md"),
        "status": "request_created",
        "actor": "human",
        "requested_worker": "materialized_execution",
        "scope": {
            "worker": "materialized_execution",
            "line": "line_p3d",
            "batch": "batch_p3d",
            "dag_root": "dag_p3d",
            "prefix": "p3d_",
        },
        "admission_report_hash": "a" * 64,
        "task_metadata": {
            "declared_worker": "materialized_execution",
            "required_worker": "materialized_execution",
            "line": "line_p3d",
            "batch": "batch_p3d",
            "dag_root": "dag_p3d",
            "prefix": "p3d_",
            "resource_hint": "cpu",
            "resource_lane": "cpu",
        },
        "workplace_metadata": {
            "profile": "demo_profile",
            "host": "host-a",
            "remote_workdir": "/home/demo/run",
            "remote_root": "/home/demo",
        },
        "required_artifacts": [{"id": "cache_a", "required": True, "present": True}],
        "required_capabilities": [{"id": "capability_a", "required": True, "present": True}],
    }
    if overrides:
        payload.update(overrides)
    path = tmp_path / "continuation" / "dispatch_requests" / f"{request_id}.json"
    return _write_json(path, payload), payload


def _preflight_report(dispatch_request_path: Path, dispatch_request: dict, *, overrides: dict | None = None) -> dict:
    payload = {
        "request_id": dispatch_request["request_id"],
        "dispatch_request_path": str(dispatch_request_path),
        "dispatch_request_hash": "b" * 64,
        "task_id": dispatch_request["task_id"],
        "active_inbox_path": dispatch_request["active_inbox_path"],
        "actor": "human",
        "status": "pass",
        "reasons": [],
        "task_metadata": dict(dispatch_request["task_metadata"]),
        "workplace_metadata": dict(dispatch_request["workplace_metadata"]),
        "checks": [],
        "action_requests": [],
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
        },
    }
    if overrides:
        payload.update(overrides)
    return payload


def _work_order_dir(tmp_path: Path) -> Path:
    return tmp_path / "continuation" / "remote_execution_work_orders"


def _install_work_order_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        remote_execution,
        "continuation_remote_execution_work_orders_dir",
        lambda: _work_order_dir(tmp_path),
    )


def test_p3d_passed_preflight_creates_local_dry_run_work_order(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(tmp_path)
    report = _preflight_report(request_path, request_payload)

    result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "ready_for_materialized_execution_handoff"
    assert result["created"] is True
    assert result["execution_mode"] == "dry_run"
    assert result["worker"] == "materialized_execution"
    assert result["workplace_profile"] == "demo_profile"
    assert result["side_effects"] == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    assert Path(result["work_order_path"]).exists()


def test_p3d_non_pass_preflight_blocks_without_work_order_file(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(tmp_path)
    report = _preflight_report(request_path, request_payload, overrides={"status": "hold", "reasons": ["resource_unavailable"]})

    result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "blocked"
    assert "preflight_status_not_pass" in result["reasons"]
    assert not list(_work_order_dir(tmp_path).glob("*.json"))


def test_p3d_missing_required_artifact_blocks_without_work_order_file(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(
        tmp_path,
        overrides={"required_artifacts": [{"id": "cache_a", "required": True, "present": False}]},
    )
    report = _preflight_report(request_path, request_payload)

    result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "blocked"
    assert result["reasons"] == ["missing_required_artifact"]
    assert result["action_requests"] == [{"type": "missing_artifact"}]
    assert not list(_work_order_dir(tmp_path).glob("*.json"))


def test_p3d_non_materialized_worker_blocks(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    task_metadata = {
        "declared_worker": "small_task_worker",
        "required_worker": "small_task_worker",
        "line": "line_p3d",
        "batch": "batch_p3d",
        "dag_root": "dag_p3d",
        "prefix": "p3d_",
        "resource_hint": "cpu",
        "resource_lane": "cpu",
    }
    request_path, request_payload = _dispatch_request(
        tmp_path,
        overrides={"requested_worker": "small_task_worker", "task_metadata": task_metadata},
    )
    report = _preflight_report(request_path, request_payload)

    result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "blocked"
    assert "materialized_execution_worker_required" in result["reasons"]
    assert not list(_work_order_dir(tmp_path).glob("*.json"))


def test_p3d_repeated_same_content_is_idempotent_replay(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(tmp_path)
    report = _preflight_report(request_path, request_payload)

    first = create_remote_execution_work_order_dry_run(report)
    second = create_remote_execution_work_order_dry_run(report)

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["status"] == "ready_for_materialized_execution_handoff"


def test_p3d_same_idempotency_key_different_content_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(tmp_path)
    report = _preflight_report(request_path, request_payload)
    first = create_remote_execution_work_order_dry_run(report)
    existing_path = Path(first["work_order_path"])
    existing = json.loads(existing_path.read_text(encoding="utf-8"))
    existing["task_id"] = "different_task"
    existing_path.write_text(json.dumps(existing, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "blocked"
    assert result["reasons"] == ["idempotency_collision"]
    assert result["created"] is False


def test_p3d_helper_does_not_call_subprocess_or_network(tmp_path: Path, monkeypatch) -> None:
    _install_work_order_dir(monkeypatch, tmp_path)
    request_path, request_payload = _dispatch_request(tmp_path)
    report = _preflight_report(request_path, request_payload)

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess.run must remain disabled")
        check_output_mock.side_effect = AssertionError("subprocess.check_output must remain disabled")
        popen_mock.side_effect = AssertionError("subprocess.Popen must remain disabled")
        create_connection_mock.side_effect = AssertionError("network access must remain disabled")
        result = create_remote_execution_work_order_dry_run(report)

    assert result["status"] == "ready_for_materialized_execution_handoff"
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called

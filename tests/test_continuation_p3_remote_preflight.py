from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from solution_agent.continuation.remote_execution import assess_remote_execution_preflight_stub


def _write_active_task(
    root: Path,
    *,
    task_id: str,
    body: str = "Remote execution preflight test task.",
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
        "continuation": {
            "activation_state": "activated_in_inbox",
            "dispatch_allowed": True,
            "scheduler_admission": {"status": "passed"},
        },
        "worker": "materialized_execution",
        "line": "line_preflight",
        "batch": "batch_preflight",
        "dag_root": "dag_preflight",
        "prefix": "pf_",
        "resource_hint": "cpu",
        "resource_lane": "cpu",
    }
    if front_updates:
        front.update(front_updates)

    front_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(f"---\n{front_text}---\n{body}", encoding="utf-8")
    return path


def _admission_report_stub(task_path: Path, *, actor: str, requested_worker: str | None, scope: dict[str, str] | None) -> dict[str, Any]:
    return {
        "task_id": task_path.stem,
        "status": "admissible",
        "admissible": True,
        "reasons": [],
        "requested_worker": requested_worker or "",
        "declared_worker": "materialized_execution",
        "selector": {
            "task": {
                "worker": "materialized_execution",
                "line": (scope or {}).get("line", ""),
                "batch": (scope or {}).get("batch", ""),
                "dag_root": (scope or {}).get("dag_root", ""),
                "prefix": (scope or {}).get("prefix", ""),
                "resource_lane": (scope or {}).get("resource_lane", "cpu"),
                "resource_hint": (scope or {}).get("resource_hint", "cpu"),
            },
            "request": {
                "worker": (scope or {}).get("worker", ""),
                "line": (scope or {}).get("line", ""),
                "batch": (scope or {}).get("batch", ""),
                "dag_root": (scope or {}).get("dag_root", ""),
                "prefix": (scope or {}).get("prefix", ""),
            },
        },
        "scheduler_explain_summary": {
            "visible_in_active_inbox": True,
            "status": True,
            "eligible": True,
            "reasons": [],
            "checks": [],
            "raw": {},
        },
    }


def _admission_report_hash(task_path: Path, *, actor: str, requested_worker: str | None, scope: dict[str, str] | None) -> str:
    return hashlib.sha256(
        json.dumps(
            _admission_report_stub(task_path, actor=actor, requested_worker=requested_worker, scope=scope),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(autouse=True)
def _force_admissible_admission(monkeypatch):
    def _assess(task_path: Path, *, actor: str, requested_worker: str | None = None, scope: dict[str, str] | None = None):
        return _admission_report_stub(task_path, actor=actor, requested_worker=requested_worker, scope=scope)

    monkeypatch.setattr(
        "solution_agent.continuation.remote_execution.assess_manual_dispatch_admission",
        _assess,
    )


def _write_dispatch_request(
    root: Path,
    *,
    request_id: str,
    task_path: Path,
    actor: str = "human",
    requested_worker: str = "materialized_execution",
    requested_scope: dict[str, str] | None = None,
    admission_report_hash: str | None = None,
    overrides: dict | None = None,
) -> Path:
    scope = requested_scope or {
        "worker": requested_worker,
        "line": "line_preflight",
        "batch": "batch_preflight",
        "dag_root": "dag_preflight",
        "prefix": "pf_",
    }
    if requested_scope is None:
        requested_scope = scope

    payload: dict = {
        "request_id": request_id,
        "task_id": task_path.stem,
        "active_inbox_path": str(task_path),
        "status": "request_created",
        "actor": actor,
        "requested_worker": requested_worker,
        "scope": scope,
        "admission_report_hash": admission_report_hash
        if admission_report_hash is not None
        else _admission_report_hash(task_path, actor=actor, requested_worker=requested_worker, scope=requested_scope),
        "task_metadata": {
            "declared_worker": requested_worker,
            "required_worker": requested_worker,
            "line": "line_preflight",
            "batch": "batch_preflight",
            "dag_root": "dag_preflight",
            "prefix": "pf_",
            "resource_hint": "cpu",
            "resource_lane": "cpu",
        },
        "workplace_metadata": {
            "profile": "demo_profile",
            "host": "host-a",
            "remote_workdir": "/home/demo/run",
            "remote_root": "/home/demo",
        },
        "host": "host-a",
        "remote_workdir": "/home/demo/run",
        "remote_root": "/home/demo",
        "required_artifacts": [
            {"id": "source_artifact", "required": True, "present": True},
            {"id": "cache_artifact", "required": True, "present": True},
        ],
        "required_capabilities": [
            {"id": "capability_a", "required": True, "present": True},
        ],
    }
    if overrides:
        payload.update(overrides)

    path = root / "continuation" / "dispatch_requests" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_side_effects_stay_disabled(report: dict) -> None:
    side_effects = report["side_effects"]
    assert side_effects["ssh"] is False
    assert side_effects["remote_write"] is False
    assert side_effects["scheduler_dispatch"] is False
    assert side_effects["registry_mutation"] is False
    assert side_effects["running_state"] is False
    assert side_effects["subprocess"] is False


def _assert_no_remote_execution_artifacts(root: Path) -> None:
    assert not (root / "continuation" / "remote_execution_work_orders").exists()
    assert not (root / "runs").exists()
    assert not (root / "continuation" / "running").exists()
    continuation_root = root / "continuation"
    if continuation_root.exists():
        assert not any(
            child.is_file() and child.name.endswith("remote_execution_work_order.json")
            for child in continuation_root.glob("**/*")
        )


def test_remote_preflight_blocks_when_request_missing(tmp_path: Path) -> None:
    _write_active_task(root=tmp_path, task_id="task_missing_request")
    result = assess_remote_execution_preflight_stub(
        tmp_path / "continuation" / "dispatch_requests" / "missing.json",
        actor="human",
    )

    assert result["status"] == "blocked"
    assert result["reasons"] == ["dispatch_request_missing"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_non_admissible_request_status(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_bad_status")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_bad_status",
        task_path=task,
        overrides={"status": "blocked"},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")

    assert result["status"] == "blocked"
    assert "dispatch_request_status_invalid" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_malformed_request_payload(tmp_path: Path) -> None:
    _write_active_task(root=tmp_path, task_id="task_malformed_request")
    request_path = tmp_path / "continuation" / "dispatch_requests" / "bad_payload.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("{", encoding="utf-8")
    result = assess_remote_execution_preflight_stub(request_path, actor="human")

    assert result["status"] == "blocked"
    assert "dispatch_request_parse_error" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_missing_request_id(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_request_id")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_request_id",
        task_path=task,
        overrides={"request_id": ""},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "request_id_missing" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_missing_task_id(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_task_id")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_task_id",
        task_path=task,
        overrides={"task_id": ""},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "task_id_missing" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_missing_active_inbox_path_or_hash(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_active_inbox")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_active_inbox",
        task_path=task,
        overrides={"active_inbox_path": "", "admission_report_hash": ""},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "active_inbox_path_missing" in result["reasons"]
    assert "admission_report_hash_missing" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_happy_path_passes_and_has_no_work_order_artifacts(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_happy")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_happy",
        task_path=task,
    )
    # keep behavior deterministic by reusing the computed valid admission hash
    request_data = json.loads(request.read_text(encoding="utf-8"))
    request_data["workplace_metadata"] = {
        "profile": "demo_profile",
        "host": "host-a",
        "remote_workdir": "/home/demo/run",
        "remote_root": "/home/demo",
    }
    request.write_text(json.dumps(request_data, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    result = assess_remote_execution_preflight_stub(
        request,
        actor="human",
        workplace_profile={
            "profile": "demo_profile",
            "host": "host-a",
            "remote_workdir": "/home/demo/run",
            "remote_root": "/home/demo",
        },
    )

    assert result["status"] == "pass"
    assert result["reasons"] == []
    _assert_side_effects_stay_disabled(result)
    _assert_no_remote_execution_artifacts(tmp_path)


def test_remote_preflight_requires_workplace_profile_and_emits_action_request(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_no_profile")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_no_profile",
        task_path=task,
        overrides={"workplace_metadata": {}},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")

    assert result["status"] == "blocked"
    assert "workplace_profile_missing" in result["reasons"]
    assert {"missing_workplace_profile"} <= {item["type"] for item in result["action_requests"]}
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_worker_mismatch(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_worker_mismatch", front_updates={"worker": "small_task_worker"})
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_worker_mismatch",
        task_path=task,
        requested_worker="materialized_execution",
        overrides={
            "task_metadata": {
                "declared_worker": "small_task_worker",
                "required_worker": "materialized_execution",
                "line": "line_preflight",
                "batch": "batch_preflight",
                "dag_root": "dag_preflight",
                "prefix": "pf_",
                "resource_hint": "cpu",
                "resource_lane": "cpu",
            },
            "scope": {
                "worker": "materialized_execution",
                "line": "line_preflight",
                "batch": "batch_preflight",
                "dag_root": "dag_preflight",
                "prefix": "pf_",
            },
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert result["task_metadata"]["declared_worker"] == "small_task_worker"
    assert "worker_mismatch" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_scope_mismatch(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_scope_mismatch")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_scope_mismatch",
        task_path=task,
        requested_scope={"worker": "materialized_execution", "line": "wrong_line", "batch": "batch_preflight", "dag_root": "dag_preflight", "prefix": "pf_"},
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "scope_mismatch" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_missing_remote_host_root_and_workdir(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_remote_fields")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_host_root_workdir",
        task_path=task,
        overrides={
            "workplace_metadata": {"profile": "demo_profile"},
            "host": "",
            "remote_root": "",
            "remote_workdir": "",
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "missing_host" in result["reasons"]
    assert "missing_remote_root" in result["reasons"]
    assert "missing_remote_workdir" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_blocks_cross_profile_remote_root(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_cross_profile_root")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_cross_profile_root",
        task_path=task,
        overrides={
            "workplace_metadata": {
                "profile": "demo_profile",
                "host": "host-a",
                "remote_workdir": "/allowed/owned/work",
                "remote_root": "/disallowed/root",
            },
            "remote_workdir": "/allowed/owned/work",
            "remote_root": "/disallowed/root",
        },
    )

    result = assess_remote_execution_preflight_stub(
        request,
        actor="human",
        workplace_profile={
            "profile": "demo_profile",
            "host": "host-a",
            "remote_workdir": "/allowed/owned/work",
            "remote_root": "/allowed/owned",
        },
    )

    assert result["status"] == "blocked"
    assert "remote_paths_not_from_profile" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


@pytest.mark.parametrize(
    "remote_root,remote_workdir",
    [
        ("/ssd6/guest/run", "/ssd6/guest/run/work"),
        ("/mnt/guest/run", "/mnt/guest/run/work"),
        ("/raid/guest/run", "/raid/guest/run/work"),
        ("C:\\\\guest\\run", "C:\\\\guest\\run\\work"),
        ("\\\\\\\\server\\\\share", "\\\\\\\\server\\\\share\\\\run"),
    ],
)
def test_remote_preflight_blocks_forbidden_guess_path_patterns(
    tmp_path: Path,
    remote_root: str,
    remote_workdir: str,
) -> None:
    sanitized = remote_root.replace("/", "_").replace("\\", "_")
    task = _write_active_task(root=tmp_path, task_id=f"task_forbidden_paths_{sanitized}")
    request = _write_dispatch_request(
        tmp_path,
        request_id=f"req_forbidden_paths_{sanitized}",
        task_path=task,
        overrides={
            "workplace_metadata": {
                "profile": "demo_profile",
                "host": "host-a",
                "remote_workdir": remote_workdir,
                "remote_root": remote_root,
            },
            "remote_root": remote_root,
            "remote_workdir": remote_workdir,
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "remote_path_guess" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_allows_forbidden_pattern_when_profile_owned_explicit(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_profile_owned_forbidden_path")
    remote_root = "/ssd6/owned/runroot"
    remote_workdir = "/ssd6/owned/runroot/task_a"
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_profile_owned_forbidden_path",
        task_path=task,
        overrides={
            "workplace_metadata": {
                "profile": "demo_profile",
                "host": "host-a",
                "remote_workdir": remote_workdir,
                "remote_root": remote_root,
            },
            "remote_root": remote_root,
            "remote_workdir": remote_workdir,
        },
    )

    result = assess_remote_execution_preflight_stub(
        request,
        actor="human",
        workplace_profile={
            "profile": "demo_profile",
            "host": "host-a",
            "remote_workdir": remote_workdir,
            "remote_root": remote_root,
        },
    )

    assert result["status"] in {"pass", "hold", "manual_review"}
    assert "remote_path_guess" not in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_missing_artifact_reports_hold_with_action_request(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_artifact")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_artifact",
        task_path=task,
        overrides={
            "required_artifacts": [
                {"id": "source_artifact", "required": True, "present": True},
                {"id": "cache_artifact", "required": True, "present": False},
            ],
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "hold"
    assert {"missing_artifact"} <= {item["type"] for item in result["action_requests"]}
    assert "missing_artifact" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_missing_capability_reports_manual_review_with_action_request(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_missing_capability")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_missing_capability",
        task_path=task,
        overrides={
            "required_capabilities": [
                {"id": "capability_a", "required": True, "present": False},
            ],
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "manual_review"
    assert {"missing_capability"} <= {item["type"] for item in result["action_requests"]}
    assert "missing_capability" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_gpu_requests_require_memory_guard_and_hold_when_unavailable(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_gpu_resource")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_gpu_unavailable",
        task_path=task,
        overrides={
            "task_metadata": {
                "declared_worker": "materialized_execution",
                "required_worker": "materialized_execution",
                "line": "line_preflight",
                "batch": "batch_preflight",
                "dag_root": "dag_preflight",
                "prefix": "pf_",
                "resource_hint": "gpu",
                "resource_lane": "gpu",
            },
            "required_gpu_mem_gb": 16,
            "available_gpu_mem_gb": 2,
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "hold"
    assert "resource_unavailable" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_gpu_missing_resource_guard_becomes_block_or_review(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_gpu_guard_missing")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_gpu_missing_guard",
        task_path=task,
        overrides={
            "task_metadata": {
                "declared_worker": "materialized_execution",
                "required_worker": "materialized_execution",
                "line": "line_preflight",
                "batch": "batch_preflight",
                "dag_root": "dag_preflight",
                "prefix": "pf_",
                "resource_hint": "gpu",
                "resource_lane": "gpu",
            },
            "required_capabilities": [{"id": "capability_a", "required": True, "present": True}],
            "required_gpu_mem_gb": None,
            "available_gpu_mem_gb": None,
        },
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "hold"
    assert any(item in {"resource_unavailable", "gpu_memory_guard_declared_when_gpu_present"} for item in result["reasons"])
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_stale_admission_hash_blocks(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_stale_hash")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_stale_hash",
        task_path=task,
        admission_report_hash="0" * 64,
    )

    result = assess_remote_execution_preflight_stub(request, actor="human")
    assert result["status"] == "blocked"
    assert "admission_report_hash_mismatch" in result["reasons"]
    _assert_side_effects_stay_disabled(result)


def test_remote_preflight_helper_has_no_blocking_subprocess_ssh_or_network_calls(tmp_path: Path) -> None:
    task = _write_active_task(root=tmp_path, task_id="task_no_calls")
    request = _write_dispatch_request(
        tmp_path,
        request_id="req_no_calls",
        task_path=task,
    )

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess.run must remain disabled")
        check_output_mock.side_effect = AssertionError("subprocess.check_output must remain disabled")
        popen_mock.side_effect = AssertionError("subprocess.POpen must remain disabled")
        create_connection_mock.side_effect = AssertionError("network access must remain disabled")
        result = assess_remote_execution_preflight_stub(request, actor="human")

    assert result["status"] == "pass"
    assert not result["side_effects"]["subprocess"]
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called
    _assert_side_effects_stay_disabled(result)

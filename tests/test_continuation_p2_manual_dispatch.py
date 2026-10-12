from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation.manual_dispatch import assess_manual_dispatch_admission


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


def test_manual_dispatch_helper_does_not_execute_or_dispatch_on_blocked_task(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_blocked_no_execute",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    registry = tmp_path / "registry.json"
    registry.write_text("{\"tasks\": {}}", encoding="utf-8")

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("socket.create_connection") as socket_mock,
    ):
        report = assess_manual_dispatch_admission(path, actor="human")

    assert report["admissible"] is False
    assert report["status"] in {"blocked", "disabled"}
    assert report["side_effects"] == {"dispatch": False, "registry_mutation": False, "remote_execution": False, "subprocess": False}
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not socket_mock.called
    assert registry.read_text(encoding="utf-8") == "{\"tasks\": {}}"


def test_dispatch_allowed_false_blocks_manual_dispatch(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_dispatch_false",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    report = assess_manual_dispatch_admission(path, actor="human")
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert "dispatch_allowed_false" in report["reasons"]


def test_missing_scheduler_admission_blocks_manual_dispatch(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_missing_admission",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True},
    )
    report = assess_manual_dispatch_admission(path, actor="human")
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert "scheduler_admission_missing" in report["reasons"]


def test_failed_scheduler_admission_blocks_manual_dispatch(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_failed_admission",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "failed"}},
    )
    report = assess_manual_dispatch_admission(path, actor="human")
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert "scheduler_admission_not_passed" in report["reasons"]


def test_activated_in_inbox_state_required_for_manual_dispatch(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_wrong_state",
        continuation={"activation_state": "pending", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    report = assess_manual_dispatch_admission(path, actor="human")
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert "activation_state_not_activated_in_inbox" in report["reasons"]


def test_blocked_continuation_states_are_not_admissible(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    for blocked in ["revoked", "superseded", "logically_revoked", "rejected", "expired"]:
        path = _write_active_task(
            tmp_path,
            task_id=f"task_{blocked}",
            continuation={"activation_state": blocked, "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
        )
        report = assess_manual_dispatch_admission(path, actor="human")
        assert report["admissible"] is False
        assert report["status"] == "blocked"
        assert f"continuation_{blocked}" in report["reasons"]


def test_requested_worker_mismatch_blocks_and_does_not_override_declared_worker(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_worker_mismatch",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}, "worker": "small_task_worker"},
        front_updates={"worker": "small_task_worker"},
    )
    report = assess_manual_dispatch_admission(path, actor="human", requested_worker="materialized_execution")
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert report["requested_worker"] == "materialized_execution"
    assert report["declared_worker"] == "small_task_worker"
    assert report["selector"]["task"]["worker"] == "small_task_worker"
    assert "worker_scope_mismatch" in report["reasons"]


def test_requested_worker_match_does_not_trigger_execution(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_worker_match",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}, "worker": "materialized_execution"},
        front_updates={"worker": "materialized_execution"},
    )
    report = assess_manual_dispatch_admission(
        path,
        actor="human",
        requested_worker="materialized_execution",
        scope={"line": "A_continuation_line", "batch": "continuation_batch", "dag_root": "continuation_root", "prefix": "cont_"},
    )
    assert report["selector"]["task"]["worker"] == "materialized_execution"
    assert report["requested_worker"] == "materialized_execution"
    assert report["side_effects"] == {"dispatch": False, "registry_mutation": False, "remote_execution": False, "subprocess": False}


def test_requested_scope_mismatch_blocks_manual_dispatch(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_scope_mismatch",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    report = assess_manual_dispatch_admission(
        path,
        actor="human",
        scope={"line": "wrong_line"},
        requested_worker="small_task_worker",
    )
    assert report["admissible"] is False
    assert report["status"] == "blocked"
    assert "line_scope_mismatch" in report["reasons"]


def test_manual_dispatch_summary_surfaces_scheduler_next_step_readiness(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_scheduler_readiness_summary",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )

    readiness = {
        "route": "bounded_self_repair",
        "status": "blocked",
        "can_stage": False,
        "blockers": ["source_repair_plan_missing"],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once_collect_missing_artifacts",
            "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
            "missing_artifact_ids": ["closure_decision"],
            "invalid_artifact_ids": [],
        },
    }

    with patch(
        "solution_agent.continuation.manual_dispatch.explain_active_inbox_task",
        return_value={
            "visible_in_active_inbox": True,
            "eligible": False,
            "reasons": ["claim_limit_no_scheduler_dispatch"],
            "checks": [{"id": "side_effects", "status": "passed"}],
            "next_step_readiness": readiness,
            "selector": {
                "task": {
                    "worker": "small_task_worker",
                    "line": "A_continuation_line",
                    "batch": "continuation_batch",
                    "dag_root": "continuation_root",
                    "prefix": "cont_",
                    "resource_lane": "",
                    "resource_hint": "cpu",
                }
            },
            "continuation_metadata": {
                "continuation": {
                    "activation_state": "activated_in_inbox",
                    "dispatch_allowed": True,
                    "scheduler_admission": {"status": "passed"},
                },
                "front_matter": {
                    "worker": "small_task_worker",
                    "line": "A_continuation_line",
                    "batch": "continuation_batch",
                    "dag_root": "continuation_root",
                    "prefix": "cont_",
                    "resource_hint": "cpu",
                },
            },
        },
    ):
        report = assess_manual_dispatch_admission(path, actor="human", requested_worker="small_task_worker")

    assert report["scheduler_explain_summary"]["next_step_readiness"] == readiness
    assert report["scheduler_explain_summary"]["bounded_validation_readiness"] == {
        "bucket_id": "B14-L74",
        "status": "repair_once",
        "next_recommendation": "repair_once_collect_missing_artifacts",
        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
        "missing_artifact_ids": ["closure_decision"],
        "invalid_artifact_ids": [],
    }
    assert report["scheduler_explain_summary"]["raw"]["next_step_readiness"] == readiness


def test_admissible_case_reports_admissible_and_never_executes(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_admissible",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
    ):
        report = assess_manual_dispatch_admission(
            path,
            actor="human",
            requested_worker="small_task_worker",
            scope={"line": "A_continuation_line", "batch": "continuation_batch", "dag_root": "continuation_root", "prefix": "cont_"},
        )
    assert report["admissible"] is True
    assert report["status"] == "admissible"
    assert report["side_effects"] == {"dispatch": False, "registry_mutation": False, "remote_execution": False, "subprocess": False}
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called


def test_no_scheduler_submit_registry_or_remote_files_created_during_admission(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_no_side_effect_files",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    registry = tmp_path / "registry.json"
    registry.write_text("{\"tasks\": {}}", encoding="utf-8")
    before_registry = registry.read_text(encoding="utf-8")

    report = assess_manual_dispatch_admission(path, actor="human", requested_worker="small_task_worker")

    assert report["admissible"] is True
    assert registry.read_text(encoding="utf-8") == before_registry
    assert not (tmp_path / "continuation" / "scheduler_dispatch").exists()
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "scheduler" / "run_queue.json").exists()


def test_no_running_state_or_remote_dir_created(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_no_running",
        continuation={"activation_state": "activated_in_inbox", "dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    report = assess_manual_dispatch_admission(path, actor="human", requested_worker="small_task_worker")
    assert report["admissible"] is True
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "continuation" / "running").exists()


def test_non_continuation_task_defaults_to_manual_review_or_blocked(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_task(
        tmp_path,
        task_id="task_non_continuation",
        continuation=None,
    )
    report = assess_manual_dispatch_admission(path, actor="human", requested_worker="small_task_worker")
    assert report["admissible"] is False
    assert report["status"] in {"manual_review", "blocked"}
    assert report["declared_worker"] == "small_task_worker"
    assert report["side_effects"]["dispatch"] is False
    assert not (tmp_path / "continuation" / "scheduler_dispatch").exists()

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from solution_agent.continuation import launch_intent
from solution_agent.continuation.ledger import (
    append_continuation_run_event,
    read_continuation_run_events,
)
from solution_agent.continuation import paths as continuation_paths


LAUNCH_INTENT_FILES = {
    "schema": Path(".solution/continuation/launch_intent_schema.yaml"),
    "policy": Path(".solution/continuation/launch_intent_policy.yaml"),
    "checklist": Path(".solution/continuation/launch_intent_preflight_checklist.yaml"),
    "doc": Path("docs/CONTINUATION_LAUNCH_INTENT_P3E.md"),
}


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path} should contain an object"
    return payload


def _write_work_order(tmp_path: Path, *, overrides: dict | None = None) -> tuple[Path, dict]:
    payload = {
        "work_order_id": "rewo_req_p3d_01",
        "dispatch_request_id": "req_p3d_01",
        "task_id": "task_p3d_01",
        "workplace_profile": "demo_profile",
        "host": "host-a",
        "remote_workdir": "/home/demo/run/task_p3d_01",
        "remote_root": "/home/demo",
        "worker": "materialized_execution",
        "line": "line_p3d",
        "batch": "batch_p3d",
        "dag_root": "dag_p3d",
        "prefix": "p3d_",
        "resource_hint": "cpu",
        "resource_lane": "cpu",
        "resource_fields": {"resource_hint": "cpu", "resource_lane": "cpu"},
        "execution_mode": "dry_run",
        "status": "ready_for_materialized_execution_handoff",
        "required_artifacts": [],
        "capability_checks": [],
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
    }
    if overrides:
        payload.update(overrides)

    work_order_path = tmp_path / "continuation" / "remote_execution_work_orders" / "rewo_req_p3d_01.json"
    work_order_path.parent.mkdir(parents=True, exist_ok=True)
    work_order_path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return work_order_path, payload


def _patch_launch_intent_dir(monkeypatch, tmp_path: Path) -> Path:
    launch_dir = tmp_path / "continuation" / "launch_intents"
    monkeypatch.setattr(
        "solution_agent.continuation.launch_intent.continuation_launch_intents_dir",
        lambda: launch_dir,
    )
    return launch_dir


def _assert_no_intent_written(launch_dir: Path, *, file_count: int = 0) -> None:
    assert not any(launch_dir.glob("*.json")), f"expected no intent files in {launch_dir}"


def test_launch_intent_contract_files_exist_and_are_noop_policy(tmp_path: Path) -> None:
    for path in LAUNCH_INTENT_FILES.values():
        assert path.exists(), f"missing contract artifact: {path}"

    schema = _load_yaml(LAUNCH_INTENT_FILES["schema"])
    policy = _load_yaml(LAUNCH_INTENT_FILES["policy"])
    checklist = _load_yaml(LAUNCH_INTENT_FILES["checklist"])
    doc_text = LAUNCH_INTENT_FILES["doc"].read_text(encoding="utf-8")

    required_false_side_effects = set(schema["required_false_side_effects"])
    assert required_false_side_effects >= {
        "ssh",
        "remote_write",
        "scheduler_dispatch",
        "registry_mutation",
        "running_state",
        "subprocess",
        "remote_execution",
    }

    assert policy["status"] == "local_launch_intent_only"
    assert policy["remote_launch_enabled"] is False
    assert policy["ssh_enabled"] is False
    assert policy["subprocess_enabled"] is False
    assert policy["remote_write"] is False
    assert policy["registry_mutation_enabled"] is False
    assert policy["running_state_enabled"] is False
    assert policy["scheduler_dispatch_enabled"] is False
    assert policy["launch_enabled_default"] is False
    assert policy["requires_explicit_launch_authorization"] is True

    assert checklist["policy"] == "continuation_launch_intent_preflight"
    assert "explicit_operator_approval" in {item["id"] for item in checklist["checks"]}
    assert "source_work_order_status_ready" in {item["id"] for item in checklist["checks"]}

    assert "launch_enabled" not in doc_text or "launch_enabled: false" in doc_text
    assert "No SSH" in doc_text
    assert "No remote write" in doc_text


def test_ready_work_order_needs_explicit_approval_for_launch_intent(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    no_approval = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=False,
    )
    assert no_approval["status"] == "blocked"
    assert "explicit_operator_approval_required" in no_approval["reasons"]
    assert no_approval["launch_enabled"] is False
    assert no_approval["created"] is False
    _assert_no_intent_written(launch_dir)

    with_approval = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert with_approval["status"] == "ready_for_launch_preflight"
    assert with_approval["created"] is True
    assert with_approval["launch_enabled"] is False
    assert with_approval["approval"]["explicit_operator_approval"] is True
    assert (launch_dir / Path(with_approval["launch_intent_id"]).with_suffix(".json")).exists()


def test_ready_launch_intent_has_launch_enabled_false_and_side_effects_closed(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )

    assert result["created"] is True
    assert result["launch_enabled"] is False
    assert result["side_effects"] == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    assert result["execution_mode"] == "bounded_remote_launch_intent"
    assert result["status"] == "ready_for_launch_preflight"
    assert result["workplace_profile"] == "demo_profile"
    assert result["remote_root"] == "/home/demo"
    assert (launch_dir / Path(result["launch_intent_id"]).with_suffix(".json")).exists()


def test_authorized_task_contract_workplace_allows_ssd_remote_paths(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(
        tmp_path,
        overrides={
            "remote_root": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
            "remote_workdir": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
            "path_authority": "task_contract_target_workplace",
        },
    )

    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )

    assert result["status"] == "ready_for_launch_preflight"
    assert result["created"] is True
    assert "remote_path_guess" not in result["reasons"]
    assert result["path_authority"] == "task_contract_target_workplace"
    assert (launch_dir / Path(result["launch_intent_id"]).with_suffix(".json")).exists()


def test_missing_actor_blocks_launch_intent_assessment(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="",
        approve_remote_launch=True,
    )
    assert result["status"] == "blocked"
    assert "actor_missing" in result["reasons"]
    assert result["created"] is False
    _assert_no_intent_written(launch_dir)


@pytest.mark.parametrize("override", [{"status": "blocked"}, {"execution_mode": "live"}, {"status": "ready", "execution_mode": "live"}])
def test_blocked_or_unready_work_order_blocks_and_writes_no_intent(
    monkeypatch,
    tmp_path: Path,
    override: dict,
) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path, overrides=override)
    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert result["status"] == "blocked"
    assert result["created"] is False
    assert result["launch_enabled"] is False
    assert not result["reasons"].count("explicit_operator_approval_required")
    _assert_no_intent_written(launch_dir)


def test_side_effects_true_in_work_order_blocks(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path, overrides={"side_effects": {"ssh": False, "remote_write": True}})
    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert result["status"] == "blocked"
    assert "work_order_side_effects_enabled" in result["reasons"]
    assert result["created"] is False
    _assert_no_intent_written(launch_dir)


def test_non_materialized_worker_blocks(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path, overrides={"worker": "small_task_worker"})
    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert result["status"] == "blocked"
    assert "non_materialized_execution_worker" in result["reasons"]
    assert result["created"] is False
    _assert_no_intent_written(launch_dir)


def test_missing_workplace_fields_block(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(
        tmp_path,
        overrides={"workplace_profile": "", "host": "", "remote_root": "", "remote_workdir": ""},
    )
    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert result["status"] == "blocked"
    assert "workplace_profile_missing" in result["reasons"] or "host_missing" in result["reasons"]
    assert result["launch_enabled"] is False
    assert result["created"] is False
    _assert_no_intent_written(launch_dir)


def test_ready_launch_intent_creation_is_idempotent_replay(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    first = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    second = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["status"] == "ready_for_launch_preflight"
    assert not second["reasons"]

    path = launch_dir / Path(first["launch_intent_id"]).with_suffix(".json")
    assert path.exists()
    assert len(list(launch_dir.glob("*.json"))) == 1


def test_same_idempotency_key_different_payload_fails_closed(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    first = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )
    assert first["created"] is True

    launch_path = launch_dir / Path(first["launch_intent_id"]).with_suffix(".json")
    existing = json.loads(launch_path.read_text(encoding="utf-8"))
    existing["launch_intent_id"] = f"{existing['launch_intent_id']}_tampered"
    launch_path.write_text(json.dumps(existing, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    blocked = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="human",
        approve_remote_launch=True,
    )

    assert blocked["status"] == "blocked"
    assert blocked["created"] is False
    assert blocked["replayed"] is False
    assert "idempotency_collision" in blocked["reasons"]
    loaded = json.loads(launch_path.read_text(encoding="utf-8"))
    assert loaded["launch_intent_id"].endswith("_tampered")


def test_launch_intent_creation_no_subprocess_or_network_calls(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _, work_order = _write_work_order(tmp_path)

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess.run must stay disabled")
        check_output_mock.side_effect = AssertionError("subprocess.check_output must stay disabled")
        popen_mock.side_effect = AssertionError("subprocess.Popen must stay disabled")
        create_connection_mock.side_effect = AssertionError("network access must stay disabled")
        result = launch_intent.create_launch_intent_dry_run(
            work_order,
            actor="human",
            approve_remote_launch=True,
        )

    assert result["created"] is True
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called
    assert (launch_dir / Path(result["launch_intent_id"]).with_suffix(".json")).exists()


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def test_run_ledger_accepts_launch_intent_stage(monkeypatch, tmp_path: Path) -> None:
    launch_dir = _patch_launch_intent_dir(monkeypatch, tmp_path)
    _patch_root(tmp_path, monkeypatch)
    _, work_order = _write_work_order(tmp_path)
    result = launch_intent.create_launch_intent_dry_run(
        work_order,
        actor="continuation_planner",
        approve_remote_launch=True,
    )
    assert result["created"] is True

    ledger = append_continuation_run_event(
        stage="launch_intent",
        status="succeeded",
        actor="continuation_planner",
        idempotency_key=result["idempotency_key"],
        run_id="p3e_ledger_stage",
        source_task_id=result["task_id"],
        task_id=result["task_id"],
        artifact_path=result["launch_intent_path"],
        reasons=result["reasons"],
        side_effects=result["side_effects"],
    )
    assert ledger["written"] is True

    events = read_continuation_run_events(run_id="p3e_ledger_stage")
    assert len(events) == 1
    assert events[0]["stage"] == "launch_intent"
    assert (launch_dir / Path(result["launch_intent_id"]).with_suffix(".json")).exists()

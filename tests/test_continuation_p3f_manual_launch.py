from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.continuation import manual_launch


MANUAL_LAUNCH_FILES = {
    "schema": Path(".solution/continuation/manual_launch_preflight_schema.yaml"),
    "policy": Path(".solution/continuation/manual_launch_preflight_policy.yaml"),
    "checklist": Path(".solution/continuation/manual_launch_preflight_checklist.yaml"),
    "doc": Path("docs/CONTINUATION_MANUAL_LAUNCH_P3F.md"),
}


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path} should contain an object"
    return payload


def test_p3f_manual_launch_contract_files_exist_and_are_local_only() -> None:
    for path in MANUAL_LAUNCH_FILES.values():
        assert path.exists(), f"missing manual launch contract artifact: {path}"

    schema = _load_yaml(MANUAL_LAUNCH_FILES["schema"])
    policy = _load_yaml(MANUAL_LAUNCH_FILES["policy"])
    checklist = _load_yaml(MANUAL_LAUNCH_FILES["checklist"])
    doc_text = MANUAL_LAUNCH_FILES["doc"].read_text(encoding="utf-8")

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

    assert policy["status"] == "local_manual_launch_boundary_only"
    assert policy["remote_launch_enabled"] is False
    assert policy["remote_execution_enabled"] is False
    assert policy["ssh_enabled"] is False
    assert policy["subprocess_enabled"] is False
    assert policy["remote_write"] is False
    assert policy["registry_mutation_enabled"] is False
    assert policy["running_state_enabled"] is False
    assert policy["scheduler_dispatch_enabled"] is False

    assert checklist["policy"] == "continuation_manual_launch_preflight_p3f"
    assert "launch_intent_load" in {item["id"] for item in checklist["checks"]}
    assert "operator_confirmation" in {item["id"] for item in checklist["checks"]}
    assert "launch_intent_side_effects_disabled" in {item["id"] for item in checklist["checks"]}

    assert "manual launch boundary" in doc_text.lower()
    assert "No SSH" in doc_text
    assert "explicit operator confirmation" in doc_text.lower()

def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _launch_intent_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "launch_intent_id": "li_rewo_req_p3d_01_abc123",
        "idempotency_key": "li_demo",
        "source_work_order_id": "rewo_req_p3d_01",
        "source_work_order_hash": "a" * 64,
        "dispatch_request_id": "req_p3d_01",
        "task_id": "task_p3d_01",
        "actor": "human",
        "approval": {
            "remote_launch_requested": True,
            "explicit_operator_approval": True,
        },
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
        "execution_mode": "bounded_remote_launch_intent",
        "launch_enabled": False,
        "status": "ready_for_launch_preflight",
        "reasons": [],
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
        "checks": [],
    }
    payload.update(overrides)
    return payload


def _write_launch_intent(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "continuation" / "launch_intents" / "li_demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or _launch_intent_payload(), sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return path


def test_p3f_manual_launch_preflight_requires_operator_confirmation() -> None:
    result = manual_launch.assess_manual_launch_preflight(
        _launch_intent_payload(),
        actor="human",
        operator_confirmed=False,
    )

    assert result["status"] == "blocked"
    assert result["ready_for_manual_remote_launch"] is False
    assert "explicit_manual_launch_confirmation_required" in result["reasons"]
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


def test_p3f_manual_launch_preflight_ready_is_still_noop() -> None:
    result = manual_launch.assess_manual_launch_preflight(
        _launch_intent_payload(),
        actor="human",
        operator_confirmed=True,
    )

    assert result["status"] == "ready_for_operator_remote_launch"
    assert result["ready_for_manual_remote_launch"] is True
    assert result["launch_enabled"] is False
    assert result["next_required_approval"] == "operator_runs_explicit_ssh_command_outside_p3f"
    assert result["host"] == "host-a"
    assert result["remote_workdir"] == "/home/demo/run/task_p3d_01"


def test_p3f_manual_launch_blocks_unready_or_side_effectful_intent() -> None:
    result = manual_launch.assess_manual_launch_preflight(
        _launch_intent_payload(
            status="blocked",
            side_effects={
                "ssh": True,
                "remote_write": False,
                "scheduler_dispatch": False,
                "registry_mutation": False,
                "running_state": False,
                "subprocess": False,
                "remote_execution": False,
            },
        ),
        actor="human",
        operator_confirmed=True,
    )

    assert result["status"] == "blocked"
    assert "launch_intent_not_ready_for_preflight" in result["reasons"]
    assert "launch_intent_side_effects_enabled" in result["reasons"]


def test_p3f_manual_launch_blocks_intent_schema_mismatch() -> None:
    result = manual_launch.assess_manual_launch_preflight(
        _launch_intent_payload(
            execution_mode="unbounded",
            schema_version=2,
        ),
        actor="human",
        operator_confirmed=True,
    )

    assert result["status"] == "blocked"
    assert "launch_intent_schema_id_mismatch" in result["reasons"]
    assert "launch_intent_schema_version_invalid" in result["reasons"]
    assert result["ready_for_manual_remote_launch"] is False
    assert result["next_required_approval"] == "resolve_manual_launch_preflight_blockers"


def test_p3f_manual_launch_record_is_idempotent_and_local_only(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)

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
        first = manual_launch.write_manual_launch_preflight(
            launch_path,
            actor="human",
            operator_confirmed=True,
        )
        second = manual_launch.write_manual_launch_preflight(
            launch_path,
            actor="human",
            operator_confirmed=True,
        )

    assert first["status"] == "ready_for_operator_remote_launch"
    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert Path(first["preflight_path"]).exists()
    assert Path(first["record_path"]).exists()
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_p3f_manual_launch_cli_writes_preflight_without_ssh(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "manual-launch-preflight",
            "--launch-intent",
            str(launch_path),
            "--actor",
            "human",
            "--operator-confirmed",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready_for_operator_remote_launch"
    assert payload["ready_for_manual_remote_launch"] is True
    assert payload["launch_enabled"] is False
    assert Path(payload["preflight_path"]).exists()
    assert Path(payload["record_path"]).exists()


def test_p3f_manual_launch_command_packet_is_printable_and_not_executed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )

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
        packet = manual_launch.write_manual_launch_command_packet(
            preflight["preflight_path"],
            detach=True,
            execution_phase="execute_run",
        )
        replay = manual_launch.write_manual_launch_command_packet(
            preflight["preflight_path"],
            detach=True,
            execution_phase="execute_run",
        )

    assert packet["status"] == "ready_for_operator_execution"
    assert packet["ready_for_operator_execution"] is True
    assert packet["created"] is True
    assert replay["replayed"] is True
    assert packet["argv"][:4] == ["python", "-m", "solution_agent.cli", "run-remote"]
    assert "task_p3d_01" in packet["argv"]
    assert "--host" in packet["argv"]
    assert "host-a" in packet["argv"]
    assert "--remote-workdir" in packet["argv"]
    assert "/home/demo/run/task_p3d_01" in packet["argv"]
    assert packet["side_effects"] == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    assert packet["intended_command_side_effects_if_operator_runs_it"]["ssh"] is True
    assert Path(packet["packet_path"]).exists()
    assert Path(packet["packet_markdown_path"]).exists()
    assert "run-remote" in Path(packet["packet_markdown_path"]).read_text(encoding="utf-8")
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_p3f_manual_launch_command_packet_blocks_unready_preflight(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=False,
    )

    packet = manual_launch.build_manual_launch_command_packet(preflight)

    assert packet["status"] == "blocked"
    assert packet["argv"] == []
    assert packet["display_command"] == ""
    assert "manual_launch_preflight_not_ready" in packet["reasons"]


def test_p3f_manual_launch_command_packet_cli_writes_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "manual-launch-command-packet",
            "--manual-launch-preflight",
            preflight["preflight_path"],
            "--phase",
            "execute_run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready_for_operator_execution"
    assert payload["ready_for_operator_execution"] is True
    assert payload["display_command"]
    assert "run-remote" in payload["display_command"]
    assert Path(payload["packet_path"]).exists()
    assert Path(payload["packet_markdown_path"]).exists()


def _ready_command_packet(tmp_path: Path, monkeypatch) -> dict:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )
    return manual_launch.write_manual_launch_command_packet(preflight["preflight_path"])


def test_p3f_live_launch_guard_dry_run_ready_but_not_executed(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

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
        guard = manual_launch.write_live_launch_executor_guard(
            packet["packet_path"],
            actor="human",
            execute_requested=False,
        )
        replay = manual_launch.write_live_launch_executor_guard(
            packet["packet_path"],
            actor="human",
            execute_requested=False,
        )

    assert guard["status"] == "dry_run_ready"
    assert guard["would_execute"] is False
    assert guard["executed"] is False
    assert guard["created"] is True
    assert replay["replayed"] is True
    assert Path(guard["guard_path"]).exists()
    assert guard["side_effects"] == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_p3f_live_launch_guard_execute_requires_exact_phrase(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=True,
        approval_phrase="",
    )

    assert guard["status"] == "blocked"
    assert guard["would_execute"] is False
    assert "explicit_live_execution_phrase_required" in guard["reasons"]


def test_p3f_live_launch_guard_with_phrase_reaches_escalation_boundary(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
    )

    assert guard["status"] == "requires_sandbox_escalation"
    assert guard["would_execute"] is True
    assert guard["executed"] is False
    assert guard["next_required_approval"] == "sandbox_escalation_and_user_approval"
    assert guard["display_command"]


def test_p3f_live_launch_guard_mission_bounded_allows_workplace_scoped_execution_boundary(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="night-researcher",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
        mission_bounded=True,
    )

    assert guard["status"] == "ready_for_mission_bounded_execution"
    assert guard["mission_bounded"] is True
    assert guard["agent_policy_allows_remote_execution"] is True
    assert guard["would_execute"] is True
    assert guard["executed"] is False
    assert guard["next_required_approval"] == "execute_command_packet_via_host_shell_with_sandbox_approval_if_needed"


def test_p3f_live_launch_guard_mission_bounded_blocks_workdir_outside_root(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(
        tmp_path,
        _launch_intent_payload(remote_root="/home/demo/project", remote_workdir="/tmp/other_project"),
    )
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )
    packet = manual_launch.write_manual_launch_command_packet(preflight["preflight_path"])

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="night-researcher",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
        mission_bounded=True,
    )

    assert guard["status"] == "blocked"
    assert guard["agent_policy_allows_remote_execution"] is False
    assert "remote_workplace_paths_incompatible" in guard["reasons"]


def test_p3f_live_launch_guard_mission_bounded_allows_output_root_under_workdir(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(
        tmp_path,
        _launch_intent_payload(
            remote_root="/home/demo/project/runs",
            remote_workdir="/home/demo/project",
        ),
    )
    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )
    packet = manual_launch.write_manual_launch_command_packet(preflight["preflight_path"])

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="night-researcher",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
        mission_bounded=True,
    )

    assert guard["status"] == "ready_for_mission_bounded_execution"
    assert guard["agent_policy_allows_remote_execution"] is True


def test_p3f_live_launch_guard_mission_bounded_blocks_destructive_command_packet(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)
    payload = json.loads(Path(packet["packet_path"]).read_text(encoding="utf-8"))
    payload["argv"] = ["rm", "-rf", "/home/demo/run/task_p3d_01"]
    payload["display_command"] = "rm -rf /home/demo/run/task_p3d_01"

    guard = manual_launch.assess_live_launch_executor_guard(
        payload,
        actor="night-researcher",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
        mission_bounded=True,
    )

    assert guard["status"] == "blocked"
    assert guard["would_execute"] is False
    assert any(reason.startswith("destructive_command_pattern:") for reason in guard["reasons"])


def test_p3f_live_launch_guard_cli_defaults_to_dry_run(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "live-launch-guard",
            "--command-packet",
            packet["packet_path"],
            "--actor",
            "human",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run_ready"
    assert payload["would_execute"] is False
    assert payload["executed"] is False
    assert Path(payload["guard_path"]).exists()


def test_p3f_live_launch_guard_cli_execute_stops_at_escalation_boundary(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "live-launch-guard",
            "--command-packet",
            packet["packet_path"],
            "--actor",
            "human",
            "--execute",
            "--approval-phrase",
            "RUN_REMOTE_MANUAL_LAUNCH",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "requires_sandbox_escalation"
    assert payload["would_execute"] is True
    assert payload["executed"] is False
    assert payload["next_required_approval"] == "sandbox_escalation_and_user_approval"


def test_p3f_live_launch_guard_execute_respects_ssh_rate_limit(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)
    log_path = tmp_path / "state" / "remote_call_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": now, "kind": "ssh", "host": "host-a", "detail": "remote status"},
        {"ts": now, "kind": "scp_to", "host": "host-a", "detail": "copy context"},
    ]
    log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
        rate_limit_window_seconds=600,
        max_transport_calls_per_window=2,
        remote_call_log_path=log_path,
    )

    assert guard["status"] == "blocked"
    assert guard["would_execute"] is False
    assert "ssh_rate_limit_active" in guard["reasons"]
    assert guard["ssh_rate_limit"]["recent_transport_call_count"] == 2
    assert guard["ssh_rate_limit"]["status"] == "blocked"


def test_p3f_live_launch_guard_dry_run_reports_rate_limit_without_blocking(tmp_path: Path, monkeypatch) -> None:
    packet = _ready_command_packet(tmp_path, monkeypatch)
    log_path = tmp_path / "state" / "remote_call_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    log_path.write_text(
        "\n".join(
            json.dumps({"ts": now, "kind": kind, "host": "host-a", "detail": kind})
            for kind in ("ssh", "scp_from")
        )
        + "\n",
        encoding="utf-8",
    )

    guard = manual_launch.assess_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=False,
        rate_limit_window_seconds=600,
        max_transport_calls_per_window=2,
        remote_call_log_path=log_path,
    )

    assert guard["status"] == "dry_run_ready"
    assert guard["would_execute"] is False
    assert guard["ssh_rate_limit"]["status"] == "blocked"
    assert "ssh_rate_limit_active" not in guard["reasons"]


def test_p3f_full_manual_launch_chain_keeps_single_auditable_artifact_path(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)

    preflight = manual_launch.write_manual_launch_preflight(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )
    packet = manual_launch.write_manual_launch_command_packet(
        preflight["preflight_path"],
        detach=True,
        execution_phase="execute_run",
    )
    dry_guard = manual_launch.write_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=False,
    )
    execution_guard = manual_launch.write_live_launch_executor_guard(
        packet["packet_path"],
        actor="human",
        execute_requested=True,
        approval_phrase="RUN_REMOTE_MANUAL_LAUNCH",
    )

    assert preflight["status"] == "ready_for_operator_remote_launch"
    assert packet["status"] == "ready_for_operator_execution"
    assert dry_guard["status"] == "dry_run_ready"
    assert execution_guard["status"] == "requires_sandbox_escalation"
    assert execution_guard["would_execute"] is True
    assert execution_guard["executed"] is False

    assert packet["manual_launch_id"] == preflight["manual_launch_id"]
    assert dry_guard["command_packet_id"] == packet["command_packet_id"]
    assert execution_guard["command_packet_id"] == packet["command_packet_id"]
    assert preflight["task_id"] == packet["task_id"] == "task_p3d_01"
    assert packet["host"] == dry_guard["ssh_rate_limit"]["host"] == "host-a"

    for artifact in (
        preflight["preflight_path"],
        preflight["record_path"],
        packet["packet_path"],
        packet["packet_markdown_path"],
        dry_guard["guard_path"],
        execution_guard["guard_path"],
    ):
        path = Path(artifact)
        assert path.exists()
        assert tmp_path in path.parents

    assert execution_guard["next_required_approval"] == "sandbox_escalation_and_user_approval"
    assert execution_guard["side_effects"] == {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }


def test_p3f_manual_launch_readiness_report_ready_and_not_reached(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    result = manual_launch.assess_manual_launch_readiness_report(
        launch_path,
        actor="human",
        operator_confirmed=True,
    )

    assert result["status"] == "ready_for_human_approved_remote_launch"
    assert result["artifact_type"] == "manual_launch_readiness_report"
    assert result["ready_for_human_approved_remote_launch"] is True
    assert result["manual_launch_ready_for_remote_launch"] is True
    assert result["preflight_status"] == "ready_for_operator_remote_launch"
    assert result["command_packet_status"] == "ready_for_operator_execution"
    assert result["live_guard_status"] == "dry_run_ready"
    assert result["remote_boundary"] == "not_reached"
    assert result["remote_boundary_reached"] is False
    assert result["preflight_ready"] is True
    assert result["command_packet_ready"] is True
    assert result["blocker_count"] == 0


def test_p3f_manual_launch_readiness_report_blocks_without_operator_confirmation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)
    result = manual_launch.assess_manual_launch_readiness_report(
        launch_path,
        actor="human",
        operator_confirmed=False,
    )

    assert result["status"] == "blocked"
    assert result["ready_for_human_approved_remote_launch"] is False
    assert result["preflight_status"] == "blocked"
    assert result["command_packet_status"] == "blocked"
    assert result["live_guard_status"] == "blocked"
    assert "explicit_manual_launch_confirmation_required" in result["reasons"]
    assert result["ready_for_human_approved_remote_launch"] is False
    assert result["blocker_count"] > 0


def test_p3f_manual_launch_readiness_report_cli_writes_artifact(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    launch_path = _write_launch_intent(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "manual-launch-readiness-report",
            "--launch-intent",
            str(launch_path),
            "--actor",
            "human",
            "--operator-confirmed",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready_for_human_approved_remote_launch"
    assert payload["artifact_type"] == "manual_launch_readiness_report"
    assert payload["created"] is True
    assert payload["replayed"] is False
    assert Path(payload["report_path"]).exists()

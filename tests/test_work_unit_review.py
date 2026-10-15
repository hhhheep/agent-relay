"""HF-017: completed ordinary commands leave a decision, not an execution slot."""
import sys
from dataclasses import replace

import pytest

from solution_agent.harness import mcp_server as server
from solution_agent.harness.remote_terminal import ProfileTerminalManager
from solution_agent.harness.task_capsule import WaitRef
from tests.test_remote_terminal import _profile


@pytest.fixture
def ordinary(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    profile = replace(profile, slots={"main": replace(profile.slots["main"], shell=sys.executable)})
    manager = ProfileTerminalManager(profile)
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "synthetic"})
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    return lambda **args: server.terminal_exec("synthetic-task", **args)


@pytest.mark.parametrize("command,state", [("print('done')", "succeeded"), ("raise SystemExit(7)", "failed")])
def test_terminal_command_waits_for_decision_and_explicit_finish_unblocks_followup(ordinary, command, state):
    first = ordinary(command=command, work_unit_id="original", timeout_seconds=10, wait_ms=5000)
    assert first["state"] == state
    assert first["work_unit"]["state"] == "review_required"
    assert first["work_unit"]["writer_leases"] == 0
    assert first["work_unit"]["external_resources"] == "unknown"
    capsule = server._capsule_store().load("synthetic-task")
    assert capsule.active_work_unit == "original"
    assert capsule.lifecycle_state == "REVIEW_REQUIRED"
    blocked = ordinary(command="print('next')", work_unit_id="following", timeout_seconds=10)
    assert blocked["ok"] is False
    assert "finish or continue" in blocked["error"]["message"]
    finished = ordinary(command_id=first["command_id"], work_unit_id="original", finish_maintenance=True, wait_ms=0)
    assert finished["work_unit"]["state"] == "finished"
    assert ordinary(command="print('next')", work_unit_id="following", timeout_seconds=10, wait_ms=5000, finish_maintenance=True)["state"] == "succeeded"


def test_continued_unit_and_persistent_tui_do_not_auto_finish(ordinary):
    # A TUI launcher may have detached work; its successful wrapper proves no resource release.
    first = ordinary(command="print('synthetic TUI launcher returned')", work_unit_id="persistent", timeout_seconds=10, wait_ms=5000)
    second = ordinary(command="print('continue same unit')", work_unit_id="persistent", timeout_seconds=10, wait_ms=5000)
    assert first["work_unit"]["state"] == second["work_unit"]["state"] == "review_required"
    assert first["command_id"] != second["command_id"]
    assert server._capsule_store().load("synthetic-task").active_work_unit == "persistent"


def test_running_continuation_keeps_writer_and_cancel_moves_to_review(ordinary):
    first = ordinary(command="print('one')", work_unit_id="sequential", timeout_seconds=10, wait_ms=5000)
    replay = ordinary(command_id=first["command_id"], work_unit_id="sequential", wait_ms=0)
    assert replay["capsule_version"] == first["capsule_version"]
    running = ordinary(command="import time; time.sleep(30)", work_unit_id="sequential", timeout_seconds=10, wait_ms=0)
    current = server._capsule_store().load("synthetic-task")
    assert current.lifecycle_state == "ACTIVE"
    assert len(current.writer_leases) == 1
    cancelled = ordinary(command_id=running["command_id"], work_unit_id="sequential", cancel=True, wait_ms=5000)
    assert cancelled["state"] == "cancelled"
    assert cancelled["work_unit"]["state"] == "review_required"
    assert cancelled["work_unit"]["writer_leases"] == 0
    closed = ordinary(command_id=running["command_id"], work_unit_id="sequential", finish_maintenance=True, wait_ms=0)
    assert closed["work_unit"]["state"] == "finished"


def test_unresolved_wait_cannot_be_finished_as_maintenance(ordinary):
    first = ordinary(command="print('done')", work_unit_id="review", timeout_seconds=10, wait_ms=5000)
    store = server._capsule_store()
    current = store.load("synthetic-task")
    store.set_wait("synthetic-task", WaitRef(wait_handle="synthetic", run_id="original", attempt_id="one", state_version=1, resume_trigger=["manual_review"], manual_resume_required=True), expected_version=current.version)
    rejected = ordinary(command_id=first["command_id"], work_unit_id="review", finish_maintenance=True, wait_ms=0)
    assert rejected["ok"] is False
    assert store.load("synthetic-task").active_work_unit == "review"

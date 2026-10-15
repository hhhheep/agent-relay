"""HF-014: explain contention without trusting stale metadata to steal locks."""
import json
import os
import subprocess
import sys

import pytest

from solution_agent.harness import remote_terminal as terminal
from tests.test_remote_terminal import FakeTmux, _profile


def test_competing_writer_reports_recorded_identity_and_read_still_works(tmp_path):
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    first = terminal.ProfileTerminalManager(profile, runner=fake)
    second = terminal.ProfileTerminalManager(profile, runner=fake)
    first.open(task_id="synthetic-owner")
    try:
        with pytest.raises(terminal.TerminalError) as caught:
            second.open(task_id="synthetic-reader")
        assert caught.value.code == "terminal_busy"
        diagnostic = json.loads(str(caught.value).split("diagnostics=", 1)[1])
        envelope = caught.value.result("terminal_open", "synthetic", "main")
        assert json.loads(envelope["error"]["message"].split("diagnostics=", 1)[1]) == diagnostic
        assert diagnostic["recorded_owner"]["pid"] == os.getpid()
        assert diagnostic["recorded_owner"]["task_id"] == "synthetic-owner"
        assert diagnostic["recorded_owner"]["connection_id"] != second.connection_id
        assert diagnostic["liveness"]["state"] == "alive"
        assert diagnostic["ownership_verified"] is False
        assert second.read(None, after_cursor=0, max_output_bytes=128)["ok"]
        assert fake.session
    finally:
        first.close(None, action="detach")
    assert second.open()["created"] is False
    second.close(None, action="detach")


def test_process_identity_detects_changed_start_marker_without_signalling():
    record = terminal._lease_process_identity()
    assert terminal._lease_process_observation(record)["state"] == "alive"
    altered = {**record, "process_start_marker": "not-the-original-start-marker"}
    assert terminal._lease_process_observation(altered)["reason"] == "pid_reused"


def test_normal_owner_exit_releases_original_lock_without_deletion_or_restart(tmp_path):
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = terminal.ProfileTerminalManager(profile, runner=fake)
    manager.open()
    manager.close(None, action="detach")
    slot = profile.slots["main"]
    lock = manager._slot_root(slot) / "writer.lock"
    identity = lock.stat().st_ino
    code = """
import json, os, sys
from pathlib import Path
from solution_agent.harness import remote_terminal as t
lease=t._Lease(Path(sys.argv[1])); lease.acquire()
path=Path(sys.argv[2]); metadata=json.loads(path.read_text(encoding='utf-8'))
metadata['lease_owner']={**t._lease_process_identity(),'connection_id':'synthetic-child','lock_type':'native'}
t._atomic_json(path,metadata)
print(json.dumps({'ready':True,'pid':os.getpid()}),flush=True)
sys.stdin.buffer.read(1)
lease.release()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(lock), str(manager._metadata_path(slot))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = json.loads(child.stdout.readline())
        assert ready["ready"] is True
        with pytest.raises(terminal.TerminalError) as caught:
            manager.open()
        diagnostic = json.loads(str(caught.value).split("diagnostics=", 1)[1])
        assert diagnostic["recorded_owner"]["pid"] == ready["pid"]
        assert diagnostic["liveness"]["state"] == "alive"
        child.communicate("x", timeout=10)
        assert child.returncode == 0
        assert manager.open()["created"] is False
        assert lock.stat().st_ino == identity
        assert sum("new-session" in call for call in fake.calls) == 1
    finally:
        if child.poll() is None:
            child.communicate("x", timeout=10)
        manager.close(None, action="detach")


@pytest.mark.parametrize("record", [None, {"pid": 1}, {"pid": True}, {"pid": -1}])
def test_legacy_or_invalid_owner_remains_unknown_and_cannot_bypass_os_lock(tmp_path, record):
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    first = terminal.ProfileTerminalManager(profile, runner=fake)
    second = terminal.ProfileTerminalManager(profile, runner=fake)
    first.open()
    path = first._metadata_path(profile.slots["main"])
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["lease_owner"] = record
    terminal._atomic_json(path, metadata)
    try:
        with pytest.raises(terminal.TerminalError) as caught:
            second.open()
        diagnostic = json.loads(str(caught.value).split("diagnostics=", 1)[1])
        assert diagnostic["liveness"]["state"] == "unknown"
        assert diagnostic["ownership_verified"] is False
        assert not second._leases
        assert fake.session
    finally:
        first.close(None, action="detach")


@pytest.mark.parametrize("reason", ["pid_reused", "host_rebooted", "process_absent", "process_evidence_unreadable"])
def test_advisory_dead_or_unknown_record_never_overrides_actual_contention(tmp_path, monkeypatch, reason):
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    first = terminal.ProfileTerminalManager(profile, runner=fake)
    second = terminal.ProfileTerminalManager(profile, runner=fake)
    first.open()
    monkeypatch.setattr(terminal, "_lease_process_observation", lambda record: {"state": "unknown" if reason.endswith("unreadable") else "exited", "reason": reason})
    try:
        with pytest.raises(terminal.TerminalError) as caught:
            second.open()
        diagnostic = json.loads(str(caught.value).split("diagnostics=", 1)[1])
        assert diagnostic["liveness"]["reason"] == reason
        assert not second._leases
        assert len(first._leases) == 1
        assert fake.session
    finally:
        first.close(None, action="detach")

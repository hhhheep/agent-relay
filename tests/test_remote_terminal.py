from __future__ import annotations

import json
import base64
import hashlib
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from solution_agent.harness.remote_terminal import (
    ProfileTerminalManager,
    TerminalError,
    TerminalProfile,
    TerminalSlot,
    load_terminal_profile,
    verify_workplace_identity,
)


class FakeTmux:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.session = False
        self.windows: set[str] = set()
        self.dead = False
        self.markers: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):  # noqa: ANN001
        args = [str(item) for item in command]
        self.calls.append(args)
        operation = args[3]
        stdout = b""
        stderr = b""
        code = 0
        if operation == "has-session":
            target = args[-1]
            if ":" in target:
                code = 0 if self.session and target.split(":", 1)[1] in self.windows else 1
            else:
                code = 0 if self.session else 1
        elif operation == "new-session":
            self.session = True
            self.dead = False
            self.windows.add(args[args.index("-n") + 1])
        elif operation == "new-window":
            self.windows.add(args[args.index("-n") + 1])
        elif operation == "list-windows":
            if not self.session:
                code = 1
            else:
                stdout = ("\n".join(sorted(self.windows)) + "\n").encode()
        elif operation == "set-option":
            if "@solution_agent_" in " ".join(args):
                self.markers[args[-2]] = args[-1]
        elif operation == "show-options":
            value = self.markers.get(args[-1])
            if value is None:
                code = 1
            else:
                stdout = (value + "\n").encode()
        elif operation == "display-message":
            stdout = f"$1\t%1\t123\t{1 if self.dead else 0}\tbash\t{self.cwd}\n".encode()
        elif operation == "kill-window":
            self.windows.discard(args[-1].split(":", 1)[1])
            self.session = bool(self.windows)
        return subprocess.CompletedProcess(args, code, stdout, stderr)


def _profile(tmp_path: Path) -> TerminalProfile:
    cwd = tmp_path / "workplace"
    cwd.mkdir()
    shell = tmp_path / "bash"
    shell.write_text("", encoding="utf-8")
    return TerminalProfile(
        profile_id="TEST",
        deployment_profile_name="TEST",
        solution_root=tmp_path,
        run_root=tmp_path / "runs",
        hub_name="solution-agent-hub",
        default_terminal="main",
        slots={"main": TerminalSlot("main", "interactive-main", cwd, str(shell))},
        segment_bytes=65536,
        retain_segments=2,
    )


def test_open_is_idempotent_and_detach_preserves_session(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    first = manager.open()
    second = manager.open()
    assert first["created"] is True
    assert second["created"] is False
    assert sum("new-session" in call for call in fake.calls) == 1
    detached = manager.close(None, action="detach")
    assert detached["session_status"] == "running"
    assert fake.session is True


def test_exec_does_not_require_interactive_terminal_attachment(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    profile = replace(
        profile,
        slots={
            "main": replace(profile.slots["main"], shell=sys.executable),
        },
    )
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)

    result = manager.exec(
        None,
        command="print('direct-job-ok')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=4096,
    )

    assert result["state"] == "succeeded", result
    assert result["stdout"]["preview"] == f"direct-job-ok{os.linesep}"
    assert result["terminal_attached_required"] is False
    assert fake.calls == []


def test_child_terminals_share_one_hub_and_one_can_terminate_in_isolation(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    main = profile.slots["main"]
    worker = TerminalSlot("worker", "interactive-worker", main.cwd, main.shell)
    profile = replace(profile, slots={"main": main, "worker": worker})
    fake = FakeTmux(main.cwd)
    manager = ProfileTerminalManager(profile, runner=fake)

    opened_main = manager.open("main")
    opened_worker = manager.open("worker")
    terminated = manager.close("main", action="terminate")

    assert opened_main["hub_id"] == opened_worker["hub_id"] == "solution-agent-hub"
    assert opened_main["window_id"] != opened_worker["window_id"]
    assert terminated["session_status"] == "terminated"
    assert fake.session is True
    assert fake.windows == {"interactive-worker"}
    assert sum("new-session" in call for call in fake.calls) == 1
    assert sum("new-window" in call for call in fake.calls) == 1
    manager.close("worker", action="detach")


def test_task_derived_child_terminal_uses_default_workplace_and_tracks_owner(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)

    opened = manager.open("TASK-FSFM-OBS", task_id="TASK-FSFM-OBS")

    assert opened["terminal_id"] == "TASK-FSFM-OBS"
    assert opened["window_id"].startswith("task-")
    assert opened["owner_task_id"] == "TASK-FSFM-OBS"
    assert opened["state"]["cwd"] == str(profile.slots["main"].cwd)
    manager.close("TASK-FSFM-OBS", action="detach")


def test_terminal_hub_enforces_child_quota_without_killing_existing_window(tmp_path: Path) -> None:
    profile = replace(_profile(tmp_path), max_children=1)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open("main")

    with pytest.raises(TerminalError) as caught:
        manager.open("TASK-SECOND", task_id="TASK-SECOND")

    assert caught.value.code == "terminal_quota_exceeded"
    assert fake.windows == {"interactive-main"}
    manager.close("main", action="detach")


def test_existing_session_refreshes_logger_without_restart(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    before = sum("new-session" in call for call in fake.calls)
    pipe_calls_before = sum("pipe-pane" in call for call in fake.calls)
    attached = manager.open()
    assert attached["created"] is False
    assert fake.markers["@solution_agent_logger_protocol"] == "2"
    assert sum("new-session" in call for call in fake.calls) == before
    assert sum("pipe-pane" in call for call in fake.calls) == pipe_calls_before + 2
    manager.close(None, action="detach")


def test_second_writer_is_busy_until_first_detaches(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    first = ProfileTerminalManager(profile, runner=fake)
    second = ProfileTerminalManager(profile, runner=fake)
    first.open()
    with pytest.raises(TerminalError, match="writer lease") as caught:
        second.open()
    assert caught.value.code == "terminal_busy"
    first.close(None, action="detach")
    assert second.open()["created"] is False
    second.close(None, action="detach")


@pytest.mark.parametrize('dead',[False,True])
def test_reattach_rejects_changed_configured_workplace_before_logger_or_restart(tmp_path: Path, dead: bool) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots['main'].cwd)
    first = ProfileTerminalManager(profile, runner=fake)
    first.open(task_id='original-task')
    first.close(None, action='detach')
    before = first._metadata_path(profile.slots['main']).read_bytes()
    other = tmp_path/'other-worktree'; other.mkdir()
    changed = replace(profile, slots={'main': replace(profile.slots['main'], cwd=other)})
    second = ProfileTerminalManager(changed, runner=fake)
    fake.dead = dead
    start = len(fake.calls)
    try:
        with pytest.raises(TerminalError) as caught:
            second.open(task_id='other-task', restart=True)
        assert caught.value.code == 'terminal_identity_mismatch'
        assert 'configured_cwd' in str(caught.value)
        assert first._metadata_path(profile.slots['main']).read_bytes() == before
        assert not any(call[3] in {'pipe-pane','kill-window','new-window','new-session'} for call in fake.calls[start:])
        assert not second._leases
    finally:
        second.release_all()


def test_reattach_accepts_same_workplace_alias_and_preserves_owner(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots['main'].cwd)
    first = ProfileTerminalManager(profile, runner=fake)
    first.open(task_id='original-task')
    first.close(None, action='detach')
    (profile.slots['main'].cwd/'alias').mkdir()
    changed = replace(profile, slots={'main': replace(profile.slots['main'], cwd=profile.slots['main'].cwd/'alias'/'..')})
    second = ProfileTerminalManager(changed, runner=fake)
    try:
        attached = second.open()
        assert attached['created'] is False
        assert attached['owner_task_id'] == 'original-task'
        assert attached['generation'] == 1
    finally:
        second.release_all()


def test_profile_label_change_reuses_persistent_session_without_rebind(tmp_path: Path) -> None:
    old_profile = replace(_profile(tmp_path), profile_id="EXAMPLE_WORKER_A_FAN", storage_profile_id="EXAMPLE_WORKER_A_FAN")
    fake = FakeTmux(old_profile.slots["main"].cwd)
    first = ProfileTerminalManager(old_profile, runner=fake)
    first.open()
    first.close(None, action="detach")

    canonical = replace(old_profile, profile_id="example_worker_a_fan")
    after_restart = ProfileTerminalManager(canonical, runner=fake)
    attached = after_restart.open()
    assert attached["created"] is False
    assert attached["profile_id"] == "example_worker_a_fan"
    assert sum("new-session" in call for call in fake.calls) == 1
    metadata = json.loads(after_restart._metadata_path(canonical.slots["main"]).read_text(encoding="utf-8"))
    assert metadata["profile_id"] == "example_worker_a_fan"
    after_restart.close(None, action="detach")

    later_restart = ProfileTerminalManager(canonical, runner=fake)
    attached = later_restart.open()
    assert attached["created"] is False
    later_restart.close(None, action="detach")


def test_workplace_identity_accepts_exact_canonical_bytes(tmp_path: Path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    identity = b"solution-agent-workplace-v1:3f5f1c58d0e84c9fb0f27210c6e30b36\n"
    path = profile.slots["main"].cwd / ".solution-agent" / "workplace.id"
    path.parent.mkdir()
    path.write_bytes(identity)
    monkeypatch.setenv("SOLUTION_AGENT_WORKPLACE_IDENTITY_B64", base64.b64encode(identity).decode("ascii"))

    result = verify_workplace_identity(profile)

    assert result == {"path": str(path), "sha256": hashlib.sha256(identity).hexdigest()}


def test_workplace_identity_rejects_mismatch(tmp_path: Path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    local = b"solution-agent-workplace-v1:3f5f1c58d0e84c9fb0f27210c6e30b36\n"
    remote = b"solution-agent-workplace-v1:4f5f1c58d0e84c9fb0f27210c6e30b36\n"
    path = profile.slots["main"].cwd / ".solution-agent" / "workplace.id"
    path.parent.mkdir()
    path.write_bytes(remote)
    monkeypatch.setenv("SOLUTION_AGENT_WORKPLACE_IDENTITY_B64", base64.b64encode(local).decode("ascii"))

    with pytest.raises(TerminalError) as conflict:
        verify_workplace_identity(profile)

    assert conflict.value.code == "workplace_identity_mismatch"


@pytest.mark.parametrize("identity", [b"\xef\xbb\xbfsolution-agent-workplace-v1:3f5f1c58d0e84c9fb0f27210c6e30b36\n", b"solution-agent-workplace-v1:3f5f1c58d0e84c9fb0f27210c6e30b36\r\n"])
def test_workplace_identity_rejects_noncanonical_encoding(tmp_path: Path, monkeypatch, identity: bytes) -> None:
    profile = _profile(tmp_path)
    path = profile.slots["main"].cwd / ".solution-agent" / "workplace.id"
    path.parent.mkdir()
    path.write_bytes(identity)
    monkeypatch.setenv("SOLUTION_AGENT_WORKPLACE_IDENTITY_B64", base64.b64encode(identity).decode("ascii"))

    with pytest.raises(TerminalError) as invalid:
        verify_workplace_identity(profile)

    assert invalid.value.code == "workplace_identity_invalid"


def test_dead_fixed_terminal_is_not_silently_recreated(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    fake.dead = True
    with pytest.raises(TerminalError) as caught:
        manager.open()
    assert caught.value.code == "terminal_exited"
    assert sum("new-session" in call for call in fake.calls) == 1
    manager.release_all()


def test_dead_fixed_terminal_is_recreated_only_with_explicit_restart(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    first = manager.open()
    fake.dead = True

    restarted = manager.open(restart=True)

    assert first["generation"] == 1
    assert restarted["created"] is True
    assert restarted["generation"] == 2
    assert fake.session is True
    assert fake.dead is False
    assert sum("kill-window" in call for call in fake.calls) == 1
    assert sum("new-session" in call for call in fake.calls) == 2
    manager.close(None, action="detach")


def test_read_rejects_non_utf8_preview_without_replacement_characters(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    slot = profile.slots["main"]
    slot_root = manager._slot_root(slot)
    payload = b"ok\n\x1b]0;unsafe\x07red\xff\n"
    segment = slot_root / "transcript-000001.bin"
    segment.write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "total_bytes": len(payload),
        "retained_from": 0,
        "segments": [
            {
                "index": 1,
                "file": segment.name,
                "start": 0,
                "end": len(payload),
                "bytes": len(payload),
                "retained": True,
            }
        ],
    }
    manager._manifest_path(slot).write_text(json.dumps(manifest), encoding="utf-8")
    result = manager.read(None, after_cursor=0, max_output_bytes=65536)
    assert result["output"] is None
    assert result["replacement_count"] == 0
    assert result["binary_artifact_required"] is True
    assert result["preview_is_sanitized"] is False
    assert result["raw_transcript_ref"]["manifest_sha256"] == result["manifest_sha256"]
    manager.release_all()


def test_read_trims_only_incomplete_utf8_suffix_at_byte_boundary(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    slot = profile.slots["main"]
    slot_root = manager._slot_root(slot)
    payload = "中文".encode("utf-8")
    segment = slot_root / "transcript-000001.bin"
    segment.write_bytes(payload)
    manager._manifest_path(slot).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "total_bytes": len(payload),
                "retained_from": 0,
                "segments": [
                    {"index": 1, "file": segment.name, "start": 0, "end": len(payload), "bytes": len(payload), "retained": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    result = manager.read(None, after_cursor=0, max_output_bytes=4)
    assert result["output"] == "中"
    assert result["decoded_bytes"] == 3
    assert result["replacement_count"] == 0
    assert result["truncated"] is True
    manager.release_all()


def test_logger_publishes_short_output_before_stdin_eof(tmp_path: Path) -> None:
    slot_root = tmp_path / "slot"
    script = Path(__file__).resolve().parents[1] / "solution_agent" / "harness" / "remote_terminal.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "logger",
            "--slot-root",
            str(slot_root),
            "--segment-bytes",
            "65536",
            "--retain-segments",
            "2",
        ],
        stdin=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(b"short-output\n")
        process.stdin.flush()
        deadline = time.monotonic() + 3
        observed = 0
        while time.monotonic() < deadline:
            manifest_path = slot_root / "stream_manifest.json"
            if manifest_path.is_file():
                try:
                    observed = int(json.loads(manifest_path.read_text(encoding="utf-8"))["total_bytes"])
                except (OSError, json.JSONDecodeError, KeyError):
                    observed = 0
                if observed == len(b"short-output\n"):
                    break
            time.sleep(0.02)
        assert observed == len(b"short-output\n")
        assert process.poll() is None
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_profile_loader_accepts_shared_workplace_with_distinct_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    payload = {
        "schema_version": 1,
        "profiles": {
            "TEST": {
                "binding_id": "test-workplace",
                "solution_agent_root": str(tmp_path),
                "remote_run_root": str(tmp_path / "runs"),
                "remote_terminals": {
                    "hub_name": "solution-agent-hub",
                    "default": "main",
                    "slots": {
                        "main": {"window_name": "main", "cwd": "/tmp/shared", "shell": "/bin/bash"},
                        "worker": {"window_name": "worker", "cwd": "/tmp/shared", "shell": "/bin/bash"},
                    },
                },
                "jobs": {"backend": "detached_process_supervisor", "max_concurrent": 2},
            }
        },
    }
    (config / "harness_remote_deploy_profiles.yaml").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SOLUTION_AGENT_PROFILE_ID", "TEST")
    profile = load_terminal_profile(tmp_path)
    assert profile.deployment_profile_name == "TEST"
    assert profile.profile_id == "test-workplace"
    assert profile.storage_profile_id == "TEST"
    assert profile.hub_name == "solution-agent-hub"
    assert profile.slots["main"].cwd == profile.slots["worker"].cwd
    assert profile.slots["main"].window_name != profile.slots["worker"].window_name


def test_nested_controller_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLUTION_AGENT_TERMINAL_NESTING", "1")
    with pytest.raises(TerminalError) as caught:
        load_terminal_profile(tmp_path)
    assert caught.value.code == "nested_terminal_controller"


def test_interactive_repl_input_stays_on_send_and_read_is_bounded_diagnostic(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    slot = profile.slots["main"]
    manager._manifest_path(slot).write_text(
        json.dumps({"schema_version": 1, "total_bytes": 0, "retained_from": 0, "segments": []}),
        encoding="utf-8",
    )
    sent = manager.send(
        None,
        chars="print('互動')",
        control="enter",
        after_cursor=None,
        wait_ms=0,
        max_output_bytes=128,
    )
    operations = [call[3] for call in fake.calls]
    assert "load-buffer" in operations
    assert "paste-buffer" in operations
    assert "send-keys" in operations
    assert sent["session_status"] == "running"

    diagnostic = manager.read(None, after_cursor=0, max_output_bytes=128)
    assert diagnostic["surface"] == "raw_diagnostic"
    assert diagnostic["deprecated_for_normal_commands"] is True
    assert diagnostic["migration_target"] == "terminal_exec"
    assert diagnostic["full_output_fallback"] is False
    manager.close(None, action="detach")


@pytest.mark.parametrize('cursor,limit',[(-1,128),(0,0),(0,65537),('invalid',128),(False,128),(0,True),(0,1.5)])
def test_send_rejects_invalid_read_window_before_interactive_input(tmp_path: Path, cursor, limit) -> None:
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots['main'].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    manager.open()
    before = len(fake.calls)
    try:
        with pytest.raises(TerminalError) as caught:
            manager.send(None, chars='effectful input', control='enter', after_cursor=cursor, wait_ms=0, max_output_bytes=limit)
        assert caught.value.code == 'invalid_read_window'
        assert not any(call[3] in {'load-buffer','paste-buffer','send-keys'} for call in fake.calls[before:])
    finally:
        manager.release_all()

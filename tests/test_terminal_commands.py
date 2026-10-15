from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solution_agent.harness.terminal_commands import TerminalCommandRunner, _process_exists, _process_marker, bounded_utf8_preview, finalize, run_execution
from solution_agent.harness.task_capsule import ScopeContract, TaskCapsule, TaskCapsuleStore, WriterLeaseRef
from solution_agent.orchestrator.core import _pid_alive as orchestrator_pid_alive
from solution_agent.small_task_worker.background_runner import _pid_alive as worker_pid_alive


def runner(tmp_path: Path, *, shell: str | None = None) -> TerminalCommandRunner:
    return TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=shell or sys.executable)


def test_empty_preview_is_text_and_does_not_request_binary_artifact(tmp_path: Path) -> None:
    path = tmp_path/'empty.bin'
    path.write_bytes(b'')
    preview = bounded_utf8_preview(path, 1)
    assert preview.text == ''
    assert preview.tail_text is None
    assert preview.raw_bytes == preview.total_bytes == 0
    assert preview.truncated is False
    assert preview.binary_artifact_required is False


def test_silent_success_returns_empty_text_for_both_streams(tmp_path: Path) -> None:
    result = runner(tmp_path).execute(command='pass',command_id=None,wait_ms=5000,max_preview_bytes=1)
    assert result['state'] == 'succeeded'
    assert result['exit_code'] == 0
    for name in ('stdout','stderr'):
        assert result[name]['preview'] == ''
        assert result[name]['binary_artifact_required'] is False
        assert result[name]['total_bytes'] == 0
        assert result[name]['sha256'] == hashlib.sha256(b'').hexdigest()


@pytest.mark.parametrize("probe", [_process_exists, orchestrator_pid_alive, worker_pid_alive])
def test_process_liveness_probe_does_not_terminate_the_child(probe) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1); raise SystemExit(19)"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert probe(child.pid)
        child.communicate(input=b"x", timeout=5)
        assert child.returncode == 19
        assert not probe(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_isolated_job_captures_exact_streams_and_nonzero_exit(tmp_path: Path) -> None:
    result = runner(tmp_path).execute(
        command="import sys\nprint('繁體中文')\nprint('bad', file=sys.stderr)\nraise SystemExit(7)",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=4096,
    )

    assert result["state"] == "failed"
    assert result["exit_code"] == 7
    assert result["stdout"]["preview"] == f"繁體中文{os.linesep}"
    assert result["stderr"]["preview"] == f"bad{os.linesep}"
    assert result["execution_surface"] == "isolated_job"
    assert Path(result["result_manifest_path"]).is_file()
    assert Path(result["completion_event_path"]).is_file()


def test_success_and_top_level_exit_are_confined_to_child(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    exited = command_runner.execute(
        command="raise SystemExit(3)",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    following = command_runner.execute(
        command="print('still-alive')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )

    assert exited["state"] == "failed"
    assert exited["exit_code"] == 3
    assert following["state"] == "succeeded"
    assert following["stdout"]["preview"] == f"still-alive{os.linesep}"


def test_async_job_can_be_resumed_by_new_runner_instance(tmp_path: Path) -> None:
    first = runner(tmp_path)
    pending = first.execute(
        command="import time\ntime.sleep(2)\nprint('done')",
        command_id=None,
        wait_ms=0,
        max_preview_bytes=128,
    )
    assert pending["state"] in {"prepared", "running"}, pending
    assert pending["wait_timed_out"] is True

    second = runner(tmp_path)
    completed = second.execute(
        command=None,
        command_id=pending["command_id"],
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert completed["state"] == "succeeded"
    assert completed["stdout"]["preview"] == f"done{os.linesep}"
    assert completed["wait_timed_out"] is False


def test_job_receipt_preserves_task_binding_and_material_event(tmp_path: Path) -> None:
    result = runner(tmp_path).execute(
        command="print('bound')",
        command_id=None,
        task_id="TASK-BOUND",
        work_unit_id="WU-1",
        wait_ms=5000,
        max_preview_bytes=128,
    )
    event = json.loads(Path(result["completion_event_path"]).read_text(encoding="utf-8"))
    assert result["task_id"] == "TASK-BOUND"
    assert result["work_unit_id"] == "WU-1"
    assert result["event_id"] == event["event_id"]
    assert event["task_id"] == "TASK-BOUND"
    assert event["work_unit_id"] == "WU-1"


def test_writer_job_releases_capsule_lease_after_terminal_receipt(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = TaskCapsuleStore(state_root)
    capsule = store.create(
        TaskCapsule(
            task_id="TASK-WRITER",
            objective="test",
            source_release_id="release-test",
            active_work_unit="WU-1",
            approval_owner="user",
            scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
        )
    )
    lease = WriterLeaseRef(
        lease_id="terminal-job-test",
        target=str(tmp_path),
        owner_work_unit="WU-1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    store.acquire_writer_lease(capsule.task_id, lease, expected_version=capsule.version)
    command_runner = TerminalCommandRunner(
        tmp_path / "jobs",
        cwd=tmp_path,
        shell=sys.executable,
        capsule_state_root=state_root,
    )

    result = command_runner.execute(
        command="print('written')",
        command_id=None,
        task_id=capsule.task_id,
        work_unit_id="WU-1",
        access_mode="writer",
        writer_lease_id=lease.lease_id,
        wait_ms=5000,
        max_preview_bytes=128,
    )

    assert result["state"] == "succeeded"
    assert result["writer_lease_release"]["released"] is True
    assert result["writer_lease_release"]["disposition"] == "released"
    assert store.load(capsule.task_id).writer_leases == []


def test_terminal_result_repairs_missing_completion_event(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    result = command_runner.execute(
        command="print('done')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    event_path = Path(result["completion_event_path"])
    event_path.unlink()

    repaired = command_runner.read_result(result["command_id"], 128)

    assert repaired["state"] == "succeeded"
    assert repaired["event_id"] == f"terminal-command:{result['command_id']}:succeeded"
    assert event_path.is_file()


@pytest.mark.parametrize("state,exit_code", [("succeeded", 0), ("failed", 7), ("cancelled", -15), ("lost", None)])
def test_terminal_publication_includes_writer_release(tmp_path, monkeypatch, state, exit_code):
    import solution_agent.harness.terminal_commands as commands

    store = TaskCapsuleStore(tmp_path / "state")
    capsule = store.create(TaskCapsule(
        task_id="TASK-PUBLICATION", objective="test", source_release_id="release-test",
        active_work_unit="WU-1", approval_owner="user",
        scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
    ))
    lease = WriterLeaseRef(lease_id="publication-lease", target=str(tmp_path), owner_work_unit="WU-1",
                           expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    store.acquire_writer_lease(capsule.task_id, lease, expected_version=capsule.version)
    command_runner = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable,
                                           capsule_state_root=tmp_path / "state")
    request = command_runner.prepare("print('unused')", task_id=capsule.task_id, work_unit_id="WU-1",
                                     access_mode="writer", writer_lease_id=lease.lease_id)
    directory = command_runner._directory(request["command_id"])
    prepared_status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    write = commands.atomic_write_json
    observations = []

    def observe_publication(path, payload):
        if Path(path).parent == directory and Path(path).name in {
            "result_manifest.json", "completion_event.json", "status.json"
        } and payload.get("state") == state:
            receipt = directory / "writer_lease_release.json"
            observations.append(json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None)
        write(path, payload)

    monkeypatch.setattr(commands, "atomic_write_json", observe_publication)
    if state == "lost":
        commands._mark_lost(directory, prepared_status, "supervisor_missing_without_terminal_receipt")
    else:
        finalize(directory, exit_code, "start", forced_state=state)
    assert len(observations) == (2 if state == "lost" else 3)
    assert all(item is not None and item["released"] and item["disposition"] == "released"
               for item in observations), observations
    assert store.load(capsule.task_id).writer_leases == []
    release_bytes = (directory / "writer_lease_release.json").read_bytes()
    # A crash after the manifest but before status publication must recover the
    # same completion and preserve the original release disposition.
    if state != "lost":
        write(directory / "status.json", prepared_status)
    recovered = command_runner.read_result(request["command_id"], 128)
    assert recovered["state"] == state
    assert recovered["writer_lease_release"]["disposition"] == "released"
    assert (directory / "writer_lease_release.json").read_bytes() == release_bytes


def test_job_timeout_has_cancelled_terminal_receipt(tmp_path: Path) -> None:
    result = runner(tmp_path).execute(
        command="import time\ntime.sleep(10)",
        command_id=None,
        timeout_seconds=1.0,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert result["state"] == "cancelled"
    assert result["failure_reason"] == "timeout_exceeded"
    assert result["finished_at"] is not None


def test_explicit_cancel_stops_only_the_bound_child_job(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    pending = command_runner.execute(
        command="import time\ntime.sleep(60)",
        command_id=None,
        wait_ms=0,
        max_preview_bytes=128,
    )
    cancelled = runner(tmp_path).execute(
        command=None,
        command_id=pending["command_id"],
        cancel=True,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    following = runner(tmp_path).execute(
        command="print('after-cancel')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert cancelled["state"] == "cancelled"
    assert cancelled["failure_reason"] == "cancel_requested"
    assert following["state"] == "succeeded", following


def test_systemd_backend_uses_one_transient_unit_and_real_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_runner = TerminalCommandRunner(
        tmp_path / "jobs",
        cwd=tmp_path,
        shell=sys.executable,
        backend="systemd_transient",
    )
    observed: list[str] = []

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(command, **kwargs):  # noqa: ANN001
        if str(command[0]).endswith("systemctl"):
            return __import__("subprocess").CompletedProcess(
                command,
                0,
                "MainPID=123\nExecMainStatus=0\nResult=success\nActiveState=active\nSubState=running\n",
                "",
            )
        observed.extend(str(item) for item in command)
        # Execute the guardian core in-process so the fake systemd query remains
        # available; real detached guardian survival is tested on Linux.
        run_execution(Path(command[-1]), os.getpid(), _process_marker(os.getpid()))
        return __import__("subprocess").CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("solution_agent.harness.terminal_commands.shutil.which", fake_which)
    monkeypatch.setattr("solution_agent.harness.terminal_commands.subprocess.run", fake_run)
    result = command_runner.execute(
        command="print('systemd-ok')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert result["state"] == "succeeded"
    assert result["backend"] == "systemd_transient"
    assert result["systemd_unit"].startswith("solution-agent-job-")
    assert "--collect" in observed
    manifest = json.loads(Path(result["result_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["systemd"]["MainPID"] == 123
    assert manifest["systemd"]["Result"] == "success"


def test_job_concurrency_limit_counts_prepared_jobs(tmp_path: Path) -> None:
    command_runner = TerminalCommandRunner(
        tmp_path / "jobs", cwd=tmp_path, shell=sys.executable, max_concurrent=1
    )
    command_runner.prepare("print('reserved')")
    with pytest.raises(RuntimeError, match="concurrency limit"):
        command_runner.prepare("print('blocked')")


def test_job_concurrency_limit_is_atomic_across_launchers(tmp_path: Path) -> None:
    first = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable, max_concurrent=1)
    second = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable, max_concurrent=1)

    def reserve(command_runner: TerminalCommandRunner) -> str:
        try:
            return str(command_runner.prepare("print('reserved')")["command_id"])
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (first, second)))

    assert sum("job concurrency limit reached" in item for item in results) == 1
    successes = [item for item in results if "job concurrency limit reached" not in item]
    assert len(successes) == 1
    assert len(successes[0]) == 32


def test_missing_supervisor_reconciles_to_lost_once(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    request = command_runner.prepare("print('never launched')")
    directory = command_runner._directory(request["command_id"])
    # This fixture represents a historical request without independent guardian.
    request['execution_guardian'] = False
    (directory / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    status_path = directory / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update(state="running", execution_guardian=False, supervisor_pid=2147483647, supervisor_process_marker="missing")
    status_path.write_text(json.dumps(status), encoding="utf-8")

    first = command_runner.read_result(request["command_id"], 128)
    second = command_runner.read_result(request["command_id"], 128)
    assert first["state"] == "lost"
    assert first["failure_reason"] == "supervisor_missing_without_terminal_receipt"
    assert second["state"] == "lost"
    assert second["finished_at"] == first["finished_at"]


def test_half_written_status_is_not_fabricated_as_lost(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    request = command_runner.prepare("print('not launched')", task_id="TASK-HALF")
    directory = command_runner._directory(request["command_id"])
    (directory / "status.json").write_text('{"state":"running"', encoding="utf-8")

    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(RuntimeError, match="receipt_corrupt"):
        command_runner.read_result(request["command_id"], 128)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("cancel", [False, True])
def test_unknown_command_lookup_does_not_write_or_launch(tmp_path, cancel):
    command_runner = runner(tmp_path)
    with pytest.raises(RuntimeError, match="not_found"):
        command_runner.execute(command=None, command_id="a" * 32, cancel=cancel,
                               wait_ms=0, max_preview_bytes=128)
    assert not (tmp_path / "jobs").exists()


def test_wrong_task_cannot_reconcile_or_cancel_command(tmp_path, monkeypatch):
    command_runner = runner(tmp_path)
    request = command_runner.prepare("print('never launched')", task_id="OWNER")
    directory = command_runner._directory(request["command_id"])
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(command_runner, "cancel", lambda *args: pytest.fail("cancel before ownership check"))
    with pytest.raises(RuntimeError, match="job_scope_conflict"):
        command_runner.execute(command=None, command_id=request["command_id"], task_id="OTHER",
                               cancel=True, wait_ms=0, max_preview_bytes=128)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_receipt_retains_execution_release_across_runner_upgrade(tmp_path):
    first = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable,
                                  runtime_release_id="execution-release")
    result = first.execute(command="print('done')", command_id=None, task_id="OWNER",
                           wait_ms=5000, max_preview_bytes=128)
    second = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable,
                                   runtime_release_id="newer-release")
    recovered = second.execute(command=None, command_id=result["command_id"], task_id="OWNER",
                               wait_ms=0, max_preview_bytes=128)
    assert recovered["state"] == "succeeded"
    assert recovered["runtime_release_id"] == "execution-release"
    for name in ("request.json", "status.json", "result_manifest.json"):
        assert json.loads((Path(result["command_dir"]) / name).read_text())["runtime_release_id"] == "execution-release"


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="requires native POSIX bash")
def test_real_bash_errexit_does_not_poison_next_job(tmp_path: Path) -> None:
    command_runner = runner(tmp_path, shell="/bin/bash")
    failed = command_runner.execute(
        command="set -e\nfalse\necho never",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    following = command_runner.execute(
        command="printf 'next-job-ok'",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert failed["state"] == "failed"
    assert failed["exit_code"] != 0
    assert following["state"] == "succeeded"
    assert following["stdout"]["preview"] == "next-job-ok"


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="requires native POSIX bash")
def test_real_bash_signal_is_preserved_in_receipt(tmp_path: Path) -> None:
    result = runner(tmp_path, shell="/bin/bash").execute(
        command="kill -TERM $$",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert result["state"] == "failed"
    assert result["exit_code"] == -15
    assert result["signal"] == 15


def test_preview_does_not_emit_replacement_characters(tmp_path: Path) -> None:
    path = tmp_path / "utf8.bin"
    path.write_bytes("中文".encode("utf-8"))
    preview = bounded_utf8_preview(path, 4)
    assert preview.text == "中"
    assert "�" not in preview.text
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\xff\xfe\xfd")
    assert bounded_utf8_preview(binary, 3).binary_artifact_required is True


def test_finalize_manifest_hashes_exact_bytes(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    request = command_runner.prepare("print('unused')")
    directory = command_runner._directory(request["command_id"])
    (directory / "stdout.bin").write_bytes(b"out")
    (directory / "stderr.bin").write_bytes(b"err")
    manifest = finalize(directory, 0, "start")
    assert manifest["state"] == "succeeded"
    assert manifest["stdout"]["bytes"] == 3
    assert json.loads((directory / "result_manifest.json").read_text())["exit_code"] == 0


def test_long_output_has_head_tail_and_binary_stderr_requires_artifact(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    request = command_runner.prepare("print('unused')")
    directory = command_runner._directory(request["command_id"])
    (directory / "stdout.bin").write_bytes(b"head-" + b"x" * 1000 + b"-tail")
    (directory / "stderr.bin").write_bytes(b"\xff\xfe\xfd")
    finalize(directory, 0, "start")
    result = command_runner.read_result(request["command_id"], 16)
    assert result["stdout"]["preview"].startswith("head-")
    assert result["stdout"]["tail_preview"].endswith("-tail")
    assert result["stdout"]["truncated"] is True
    assert result["stderr"]["preview"] is None
    assert result["stderr"]["binary_artifact_required"] is True


def test_ansi_is_sanitized_in_real_job(tmp_path: Path) -> None:
    result = runner(tmp_path).execute(
        command="print('\\033[31mred\\033[0m')",
        command_id=None,
        wait_ms=5000,
        max_preview_bytes=128,
    )
    assert result["state"] == "succeeded"
    assert result["stdout"]["preview"] == f"red{os.linesep}"
    assert result["stdout"]["control_sequences_removed"] == 2


def test_cancel_before_launch_records_request_and_never_runs_child(tmp_path: Path) -> None:
    command_runner = runner(tmp_path)
    marker = tmp_path / "must-not-exist"
    request = command_runner.prepare(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')")
    command_id = request["command_id"]
    command_runner.cancel(command_id)
    directory = command_runner._directory(command_id)
    assert json.loads((directory / "cancel_request.json").read_text())["command_id"] == command_id
    run_execution(directory)
    result = command_runner.read_result(command_id, 128)
    assert result["state"] == "cancelled"
    assert result["failure_reason"] == "cancel_requested"
    assert not marker.exists()

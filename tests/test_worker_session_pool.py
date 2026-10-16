"""The real adapter selects session identity from original jobs, not caller memory."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
import uuid

import pytest
import yaml

from solution_agent.harness import codex_worker as worker
from solution_agent.harness.task_capsule import TaskCapsule, TaskCapsuleStore, ScopeContract, WriterLeaseRef
from solution_agent.harness.terminal_commands import TerminalCommandRunner, CommandReceiptError, finalize
from solution_agent.harness.worker_sessions import WorkerAdmissionError, validate_policy, validate_dispatch
from tests.test_harness_codex_worker import packet


def policy():
    return {"version": 1, "max_running": 3,
            "workers": {"worker-a": "alpha", "worker-b": "beta", "worker-c": "gamma"},
            "scopes": {"alpha": {"task_ids": ["TASK", "TASK-A2"], "max_running": 1},
                       "beta": {"task_ids": ["TASK-B"], "max_running": 1},
                       "gamma": {"task_ids": ["TASK-C"], "max_running": 1}}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def pool(packet, monkeypatch):
    return pool_environment(packet, monkeypatch)


def pool_environment(packet, monkeypatch):
    """Reusable isolated core fixture, also consumed by the combined scenario."""
    profile = worker.load_remote_deploy_profile(None, "FAN")
    profile.worker_pool = policy()
    profile.terminal_storage_profile_id = "FAN"
    root, cwd = Path(packet["root"]), Path(packet["cwd"])
    monkeypatch.setenv("CODEX_HOME", str(root / "fixture-codex-home"))
    jobs = Path(profile.remote_run_root) / "terminal-sessions" / "FAN" / "jobs"
    runner = TerminalCommandRunner(jobs, cwd=cwd, shell=sys.executable,
                                  capsule_state_root=root / "state", runtime_release_id="fixture-release")
    store = TaskCapsuleStore(root / "state")
    calls = []

    def make(task="TASK", unit="u1"):
        return worker.worker_packet(root, "FAN", task, unit, "Do only this approved synthetic assignment", "prepare_code")

    def prepare(p):
        return runner.prepare(worker.worker_command(p), task_id=p["task_id"], work_unit_id=p["work_unit_id"],
                              access_mode="writer", writer_lease_id="lease-" + uuid.uuid4().hex, worker_packet=p)

    def assigned(request):
        script = jobs / request["command_id"] / "request.sh"
        return json.loads(base64.b64decode(shlex.split(script.read_text(encoding="utf-8"))[-1]))

    def complete(p, request, *, codex_run=None, finish_unit=True, observed_thread=None):
        directory = jobs / request["command_id"]
        try:
            capsule = store.load(p["task_id"])
        except FileNotFoundError:
            capsule = store.create(TaskCapsule(task_id=p["task_id"], objective="synthetic session acceptance",
                approval_owner="fixture", source_release_id="fixture-release",
                scope=ScopeContract(readable_paths=[str(cwd)], writable_paths=[str(cwd)])))
        if capsule.active_work_unit is None:
            capsule = store.start_work_unit(p["task_id"], p["work_unit_id"], expected_version=capsule.version)
        assert capsule.active_work_unit == p["work_unit_id"]
        if not capsule.writer_leases:
            store.acquire_writer_lease(p["task_id"], WriterLeaseRef(lease_id=request["writer_lease_id"], target=str(cwd),
                owner_work_unit=p["work_unit_id"], expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                expected_version=capsule.version)
        else:
            assert [lease.lease_id for lease in capsule.writer_leases] == [request["writer_lease_id"]]
        monkeypatch.setenv("SOLUTION_AGENT_COMMAND_ID", request["command_id"])
        monkeypatch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(directory))

        def fake_codex(argv, **kwargs):
            calls.append(argv)
            identity = observed_thread or (argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4()))
            output = Path(p["output"])
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.yaml").write_text(yaml.safe_dump({"task_id": p["task_id"], "worker": "codex",
                "status": "success", "summary_short": "synthetic result", "outputs": {"worker_notes": "worker_notes.md"}}), encoding="utf-8")
            for name in ("worker_notes.md", "stdout.log", "stderr.log"):
                (output / name).write_text("synthetic evidence", encoding="utf-8")
            (directory / "stdout.bin").write_text(json.dumps({"type": "thread.started", "thread_id": identity})
                                                    + '\n' + json.dumps({"type": "turn.completed"}) + '\n', encoding="utf-8")
            (directory / "stderr.bin").write_bytes(b'')
            return SimpleNamespace(returncode=0)

        with monkeypatch.context() as patch:
            patch.setattr(worker.subprocess, "run", codex_run or fake_codex)
            assert worker.run_worker(assigned(request)) == 0
        manifest = finalize(directory, 0, "synthetic-start")
        assert manifest["worker_session"]["state"] == "observed"
        capsule = store.load(p["task_id"])
        assert capsule.writer_leases == []
        if finish_unit:
            store.finish_work_unit(p["task_id"], p["work_unit_id"], expected_version=capsule.version)
        return manifest

    return SimpleNamespace(make=make, prepare=prepare, complete=complete, assigned=assigned,
                           runner=runner, jobs=jobs, profile=profile, store=store, calls=calls)


def test_same_session_accepts_next_assignment_with_distinct_command_and_artifacts(pool):
    first = pool.make(unit="first"); a = pool.prepare(first); receipt = pool.complete(first, a)
    second = pool.make("TASK-A2", "second"); b = pool.prepare(second)
    assert b["command_id"] != a["command_id"]
    assert b["worker_assignment"]["previous_command_id"] == a["command_id"]
    assert b["worker_assignment"]["thread_id"] == receipt["worker_session"]["thread_id"]
    pool.complete(second, b)
    assert "resume" not in pool.calls[0]
    assert pool.calls[1][-3:] == ["resume", receipt["worker_session"]["thread_id"], "-"]
    assert "--last" not in pool.calls[1]
    assert first["output"] != second["output"]


def test_lost_ack_recovers_original_reservation_without_allocating_again(pool):
    p = pool.make(); first = pool.prepare(p)
    assert worker.submission(p, jobs_root=pool.jobs) == (None, first["command_id"])
    with pytest.raises(CommandReceiptError):
        pool.prepare(p)
    with pytest.raises(WorkerAdmissionError, match="worker_pool_busy"):
        pool.prepare(pool.make("TASK-A2", "other"))
    assert len(list(pool.jobs.glob('*/request.json'))) == 1
    assert pool.calls == []


@pytest.mark.parametrize("stage", ["reserve", "dispatch"])
def test_conflicting_machine_observed_thread_cannot_be_reused_by_two_workers(pool, monkeypatch, stage):
    first = pool.make(unit="first"); a = pool.prepare(first); receipt = pool.complete(first, a)
    other = pool.make("TASK-B", "first"); b = pool.prepare(other)
    following = pool.make("TASK-A2", "next")
    pending = pool.prepare(following) if stage == "dispatch" else None
    pool.complete(other, b, observed_thread=receipt["worker_session"]["thread_id"])
    count = len(pool.calls)
    with pytest.raises(WorkerAdmissionError, match="thread_conflict"):
        if pending is None:
            pool.prepare(following)
        else:
            monkeypatch.setenv("SOLUTION_AGENT_COMMAND_ID", pending["command_id"])
            monkeypatch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(pool.jobs / pending["command_id"]))
            worker.run_worker(pool.assigned(pending))
    assert len(pool.calls) == count
    assert not Path(following["control"]).exists()
    assert len(list(pool.jobs.glob('*/request.json'))) == (2 if pending is None else 3)


def test_concurrent_managers_cannot_reserve_one_worker_twice(pool):
    packets = [pool.make("TASK" if i % 2 else "TASK-A2", "race-" + str(i)) for i in range(8)]
    def attempt(p):
        try:
            return pool.prepare(p)
        except WorkerAdmissionError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, packets))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("worker_pool_busy") == 7


def test_three_workers_ten_assignments_keep_scope_identity_and_capacity(pool):
    active = []
    for task in ("TASK", "TASK-B", "TASK-C"):
        p = pool.make(task, "first"); active.append((p, pool.prepare(p)))
    with pytest.raises(WorkerAdmissionError, match="worker_pool_busy"):
        pool.prepare(pool.make("TASK-A2", "blocked"))
    ids, commands = {}, set()
    for p, request in active:
        receipt = pool.complete(p, request)
        ids[p["task_id"]] = receipt["worker_session"]["thread_id"]
        commands.add(request["command_id"])
    for i in range(7):
        task = ("TASK", "TASK-B", "TASK-C")[i % 3]
        p = pool.make(task, "next-" + str(i)); request = pool.prepare(p)
        receipt = pool.complete(p, request)
        assert receipt["worker_session"]["thread_id"] == ids[task]
        commands.add(request["command_id"])
    assert len(commands) == 10 and len(pool.calls) == 10 and len(set(ids.values())) == 3
    assert len(list(pool.jobs.glob('*/request.json'))) == 10
    assert all(not list(pool.store.load(task).writer_leases) for task in ids)


def test_wrong_valid_thread_rejected_before_codex_even_with_changed_request_digest(pool, monkeypatch):
    first = pool.make(unit="first"); a = pool.prepare(first); pool.complete(first, a)
    other = pool.make("TASK-B", "other"); other_request = pool.prepare(other)
    other_result = pool.complete(other, other_request)
    second = pool.make(unit="second"); b = pool.prepare(second)
    bound = pool.assigned(b)
    bound["session_assignment"]["thread_id"] = other_result["worker_session"]["thread_id"]
    b["worker_assignment"] = bound["session_assignment"]
    b["worker_assignment_sha256"] = worker._digest(b["worker_assignment"])
    encoded = worker.worker_command(bound).encode()
    b["command_sha256"], b["command_bytes"] = hashlib.sha256(encoded).hexdigest(), len(encoded)
    directory = pool.jobs / b["command_id"]
    (directory / "request.sh").write_bytes(encoded + b'\n')
    write(directory / "request.json", b)
    monkeypatch.setenv("SOLUTION_AGENT_COMMAND_ID", b["command_id"])
    monkeypatch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(directory))
    count = len(pool.calls)
    with pytest.raises(WorkerAdmissionError, match="worker_pool_identity_changed"):
        worker.run_worker(bound)
    assert len(pool.calls) == count
    assert not Path(second["control"]).exists()


@pytest.mark.parametrize("fault", ["missing_manifest", "changed_stdout", "unreleased", "changed_result", "lost", "foreign_runtime"])
def test_unproven_predecessor_never_becomes_a_new_session(pool, fault):
    p = pool.make(); request = pool.prepare(p); pool.complete(p, request)
    directory = pool.jobs / request["command_id"]
    if fault == "missing_manifest": (directory / "result_manifest.json").unlink()
    elif fault == "changed_stdout": (directory / "stdout.bin").write_bytes(b'changed\n')
    elif fault == "unreleased":
        value = json.loads((directory / "writer_lease_release.json").read_text()); value["released"] = False
        write(directory / "writer_lease_release.json", value)
    elif fault == "changed_result": (Path(p["output"]) / "worker_notes.md").write_text("changed")
    elif fault == "lost":
        value = json.loads((directory / "status.json").read_text()); value["state"] = "lost"
        write(directory / "status.json", value)
    elif fault == "foreign_runtime": pool.runner.runtime_release_id = "another-release"
    with pytest.raises(WorkerAdmissionError): pool.prepare(pool.make(unit="next"))
    assert len(list(pool.jobs.glob('*/request.json'))) == 1


def test_foreign_workplace_jobs_root_and_unauthorized_task_fail_before_preparation(pool):
    with pytest.raises(WorkerAdmissionError, match="not_authorized"):
        pool.prepare(pool.make("FOREIGN", "u"))
    other = TerminalCommandRunner(pool.jobs.parent / "foreign-jobs", cwd=pool.runner.cwd, shell=sys.executable)
    p = pool.make()
    with pytest.raises(WorkerAdmissionError, match="jobs_root_mismatch"):
        other.prepare(worker.worker_command(p), task_id=p["task_id"], work_unit_id=p["work_unit_id"],
                      access_mode="writer", writer_lease_id="fixture", worker_packet=p)
    assert not list(pool.jobs.glob('*/request.json'))


def test_pool_control_state_cannot_be_put_inside_worker_writable_workplace(pool):
    pool.profile.remote_run_root = str(pool.runner.cwd / "model-writable-runs")
    with pytest.raises(WorkerAdmissionError, match="control_inside_workplace"):
        pool.make()


def test_public_mcp_capacity_refusal_releases_only_unsubmitted_work(pool, monkeypatch):
    import solution_agent.harness.mcp_server as server
    for task in ("TASK", "TASK-B", "TASK-C"):
        pool.prepare(pool.make(task, "busy"))
    def execute(*args, **kwargs):
        kwargs.pop("runtime_release_id", None)
        return pool.runner.execute(**kwargs)
    manager = SimpleNamespace(base=pool.jobs.parent,
        profile=SimpleNamespace(deployment_profile_name="FAN", default_terminal="main",
                                slots={"main": SimpleNamespace(cwd=pool.runner.cwd)}), exec=execute)
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_solution_root", lambda: Path(pool.make()["root"]))
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "fixture-release"})
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    result = server.terminal_exec("TASK-A2", work_unit_id="not-submitted", worker_task="approved assignment",
                                  timeout_seconds=30, wait_ms=0)
    assert result["ok"] is False
    assert result["submission"]["outcome"] == "rejected"
    assert result["submission"]["phase"] == "before_executor"
    assert result["worker_admission"] == {"code": "worker_pool_busy", "no_command_prepared": True}
    assert not result.get("command_id")
    capsule = pool.store.load("TASK-A2")
    assert capsule.active_work_unit is None and capsule.writer_leases == []
    assert len(list(pool.jobs.glob('*/request.json'))) == 3


def test_wrong_pinned_pool_fails_before_capsule_or_command_creation(pool, monkeypatch):
    import solution_agent.harness.mcp_server as server
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_solution_root", lambda: Path(pool.make()["root"]))
    monkeypatch.setattr(server, "_terminal_manager", lambda: SimpleNamespace(
        profile=SimpleNamespace(deployment_profile_name="FAN", default_terminal="main", slots={"main": SimpleNamespace(cwd=pool.runner.cwd)})))
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    result = server.terminal_exec("TASK", work_unit_id="u1", worker_task="approved", timeout_seconds=30,
                                  worker_pool_fingerprint="0" * 64)
    assert result["submission"]["outcome"] == "rejected"
    assert result["worker_admission"]["code"] == "worker_pool_fingerprint_mismatch"
    assert not list(pool.jobs.glob('*/request.json'))
    assert pool.calls == []


def test_public_pool_completion_collects_original_artifacts_without_research_acceptance(pool, monkeypatch):
    import solution_agent.harness.mcp_server as server
    p = pool.make(); request = pool.prepare(p)
    pool.complete(p, request, finish_unit=False)
    def execute(*args, **kwargs):
        kwargs.pop("runtime_release_id", None)
        return pool.runner.execute(**kwargs)
    manager = SimpleNamespace(base=pool.jobs.parent, profile=SimpleNamespace(
        deployment_profile_name="FAN", default_terminal="main", run_root=Path(pool.profile.remote_run_root),
        slots={"main": SimpleNamespace(cwd=pool.runner.cwd)}), exec=execute)
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_solution_root", lambda: Path(p["root"]))
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "fixture-release"})
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    result = server.terminal_exec("TASK", command_id=request["command_id"], work_unit_id=p["work_unit_id"], wait_ms=0)
    assert result.get("state") == "succeeded", result
    assert result["command_id"] == request["command_id"]
    assert result["work_unit"]["state"] == "finished"
    assert result["work_unit"]["research_acceptance"] == "not_assessed"
    assert result["writer_lease_released"] is True
    assert {"worker_result", "worker_receipt", "worker_notes"} <= result["artifacts"].keys()
    capsule = pool.store.load("TASK")
    assert capsule.active_work_unit is None and capsule.writer_leases == []
    assert capsule.authoritative_artifacts == []
    assert any(a.role == "terminal_worker_result" for a in capsule.working_artifacts)
    count = len(pool.calls)
    recovered = server.terminal_exec("TASK", command_id=request["command_id"], work_unit_id=p["work_unit_id"], wait_ms=0)
    assert recovered["artifacts"] == result["artifacts"]
    assert recovered["capsule_version"] == result["capsule_version"]
    assert len(pool.calls) == count


@pytest.mark.parametrize("fault", ["arbitrary_field", "boolean_capacity", "duplicate_task", "missing_scope", "excess_capacity"])
def test_pool_policy_does_not_silently_fall_back_to_one_shot(fault):
    value = policy()
    if fault == "arbitrary_field": value["thread_id"] = str(uuid.uuid4())
    elif fault == "boolean_capacity": value["max_running"] = True
    elif fault == "duplicate_task": value["scopes"]["beta"]["task_ids"].append("TASK")
    elif fault == "missing_scope": value["workers"]["worker-a"] = "missing"
    elif fault == "excess_capacity": value["max_running"] = 4
    with pytest.raises(ValueError): validate_policy(value)

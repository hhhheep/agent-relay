from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from solution_agent.harness import codex_worker as worker


@pytest.fixture
def packet(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    cwd = tmp_path / "workplace"
    root.mkdir()
    cwd.mkdir()
    profile = SimpleNamespace(solution_agent_root=str(root), project_workdir=str(cwd),
                              remote_run_root=str(tmp_path / "runs"), codex_path="/declared/bin/codex")
    def load_profile(path, name):
        assert name == "FAN", "worker must receive the deployment name, not the binding ID"
        return profile
    monkeypatch.setattr(worker, "load_remote_deploy_profile", load_profile)
    command_dir = tmp_path / "command"
    command_dir.mkdir()
    monkeypatch.setenv("SOLUTION_AGENT_COMMAND_ID", "a" * 32)
    monkeypatch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(command_dir))
    return worker.worker_packet(root, "FAN", "TASK", "prepare-1", "實作小測試；不要訓練。 $(unsafe) ' \"", "prepare_code")


def write_result(packet, status="success"):
    out = Path(packet["output"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.yaml").write_text(yaml.safe_dump({"task_id": "TASK", "worker": "codex",
        "status": status, "summary_short": "完成", "outputs": {"worker_notes": "worker_notes.md"}}), encoding="utf-8")
    for name in ("worker_notes.md", "stdout.log", "stderr.log"):
        (out / name).write_text("evidence", encoding="utf-8")


def test_submission_preserves_unicode_without_shell_interpretation(packet):
    command, command_id = worker.submission(packet)
    assert command_id is None
    args = shlex.split(command)
    assert json.loads(base64.b64decode(args[-1])) == packet
    assert "$(unsafe)" not in command


def test_codex_uses_declared_local_process_and_existing_prompt(packet, monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        write_result(packet)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker.run_worker(packet) == 0
    argv, kwargs = calls[0]
    assert argv[:2] == ["/declared/bin/codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "-"
    assert packet["text"] in kwargs["input"].decode()
    assert "prepare_code" in kwargs["input"].decode()
    assert kwargs["env"]["SOLUTION_AGENT_TERMINAL_NESTING"] == "1"
    assert "start_new_session" not in kwargs
    assert worker.submission(packet) == (None, "a" * 32)
    with pytest.raises(ValueError, match="already claimed"):
        worker.run_worker(packet)
    assert len(calls) == 1
    receipt = json.loads((Path(packet["control"]) / "receipt.json").read_text())
    assert receipt["contract_valid"]
    assert len(receipt["artifact_hashes"]) == 4


def test_changed_task_cannot_reuse_work_unit(packet, monkeypatch):
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=9))
    assert worker.run_worker(packet) == 9
    with pytest.raises(ValueError, match="different worker task"):
        worker.submission({**packet, "text": "new scope"})


def test_uncertain_launch_never_resends(packet):
    Path(packet["control"]).mkdir(parents=True)
    with pytest.raises(ValueError, match="uncertain"):
        worker.submission(packet)


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_process_exit_zero_is_not_task_success(packet, monkeypatch, status):
    def fake_run(*args, **kwargs):
        write_result(packet, status)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker.run_worker(packet) == 1


def test_missing_contract_is_not_success(packet, monkeypatch):
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert worker.run_worker(packet) == 1


def test_changed_profile_rejected_before_launch(packet, monkeypatch):
    monkeypatch.setattr(worker, "worker_packet", lambda *args: {**packet, "cwd": "/wrong"})
    with pytest.raises(ValueError, match="profile changed"):
        worker.run_worker(packet)
    assert not Path(packet["control"]).exists()


def test_result_path_escape_rejected(packet):
    write_result(packet)
    path = Path(packet["output"]) / "result.yaml"
    value = yaml.safe_load(path.read_text())
    value["outputs"]["secret"] = "../secret"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match="relative"):
        worker.verify_result(path.parent, "TASK")


def test_worker_must_use_supervisor(packet, monkeypatch):
    monkeypatch.delenv("SOLUTION_AGENT_COMMAND_ID")
    with pytest.raises(ValueError, match="supervised"):
        worker.run_worker(packet)


def test_mcp_worker_routes_into_existing_job_and_resume(packet, monkeypatch):
    import solution_agent.harness.mcp_server as server
    seen = []
    profile = SimpleNamespace(profile_id="fan-binding", deployment_profile_name="FAN", default_terminal="main",
                              slots={"main": SimpleNamespace(cwd=Path(packet["cwd"]))})
    def execute(*args, **kwargs):
        seen.append(kwargs)
        return {"ok": True, "state": "running", "command_id": "a" * 32, "task_id": "TASK"}
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_solution_root", lambda: Path(packet["root"]))
    monkeypatch.setattr(server, "_terminal_manager", lambda: SimpleNamespace(profile=profile, exec=execute))
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "test"})
    monkeypatch.setattr(server, "_with_server", lambda x: x)
    response = server.terminal_exec("TASK", worker_task=packet["text"], work_unit_id="prepare-1", timeout_seconds=60)
    assert response["ok"], response
    assert seen[0]["command"] and seen[0]["writer_lease_id"]
    assert seen[0]["command_id"] is None
    response = server.terminal_exec("TASK", command_id="a" * 32)
    assert response["ok"]
    assert seen[-1]["command"] is None
    assert seen[-1]["command_id"] == "a" * 32


def test_mcp_collects_worker_evidence_and_finishes_work_unit(packet, monkeypatch):
    import os
    import solution_agent.harness.mcp_server as server
    command_dir = Path(os.environ["SOLUTION_AGENT_COMMAND_DIR"])
    for stream in ("stdout", "stderr"):
        (command_dir / (stream + ".bin")).write_bytes(b"supervisor log")
    def fake_codex(*args, **kwargs):
        write_result(packet)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess, "run", fake_codex)
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["command"]:
            assert worker.run_worker(packet) == 0
        return {"ok": True, "state": "succeeded", "command_id": "a" * 32,
                "command_dir": str(command_dir), "task_id": "TASK", "work_unit_id": "prepare-1",
                "writer_lease_id": kwargs["writer_lease_id"],
                "stdout": {"path": str(command_dir / "stdout.bin")},
                "stderr": {"path": str(command_dir / "stderr.bin")}}
    profile = SimpleNamespace(profile_id="fan-binding", deployment_profile_name="FAN", default_terminal="main", run_root=Path(packet["control"]).parents[1],
                              slots={"main": SimpleNamespace(cwd=Path(packet["cwd"]))})
    monkeypatch.setattr(server, "_require_activation", lambda: None)
    monkeypatch.setattr(server, "_solution_root", lambda: Path(packet["root"]))
    monkeypatch.setattr(server, "_terminal_manager", lambda: SimpleNamespace(profile=profile, exec=execute))
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "test"})
    monkeypatch.setattr(server, "_with_server", lambda x: x)
    result = server.terminal_exec("TASK", worker_task=packet["text"], work_unit_id="prepare-1", timeout_seconds=60)
    assert result["ok"], result
    assert result["worker"]["contract_valid"]
    assert {"worker_receipt", "worker_result", "worker_notes"} <= result["artifacts"].keys()
    assert server._capsule_store().load("TASK").active_work_unit is None
    replay = server.terminal_exec("TASK", worker_task=packet["text"], work_unit_id="prepare-1", timeout_seconds=60)
    assert replay["ok"], replay
    assert calls[-1]["command"] is None
    assert replay["artifacts"] == result["artifacts"]

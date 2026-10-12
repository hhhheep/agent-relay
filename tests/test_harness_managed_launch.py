import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
import shlex
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from scripts.harness.build_profile_plugin import build
from solution_agent.harness import mcp_server
from solution_agent.harness.release_identity import build_runtime_identity
from solution_agent.harness.release_identity import canonical_json_sha256, mcp_tool_contract
from solution_agent.harness.protocol import ProtocolMismatch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remote_launcher", ROOT / "plugins/solution-agent-harness/scripts/remote_launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.mark.parametrize("recover", [True, False])
def test_atomic_receipt_windows_sharing_violation_is_bounded(tmp_path, monkeypatch, recover):
    from solution_agent.harness import task_capsule
    target = tmp_path / "status.json"
    target.write_text('{"state":"old"}')
    original = task_capsule.os.replace
    calls = []
    def sharing_conflict(source, destination):
        calls.append(1)
        if recover and len(calls) == 3:
            return original(source, destination)
        error = PermissionError("simulated Windows reader")
        error.winerror = 32
        raise error
    monkeypatch.setattr(task_capsule.os, "replace", sharing_conflict)
    monkeypatch.setattr(task_capsule.time, "sleep", lambda _: None)
    if recover:
        task_capsule.atomic_write_json(target, {"state": "new"})
        assert json.loads(target.read_text())["state"] == "new"
        assert len(calls) == 3
    else:
        with pytest.raises(PermissionError):
            task_capsule.atomic_write_json(target, {"state": "new"})
        assert json.loads(target.read_text())["state"] == "old"
        assert len(calls) == 6
    assert not list(tmp_path.glob(".tmp-*"))


def test_tool_description_indentation_is_not_a_contract_change(monkeypatch):
    tool = mcp_server.mcp._tool_manager._tools["terminal_exec"]
    monkeypatch.setattr(tool, "description", "Summary.\n    Second line.\n    ")
    indented = canonical_json_sha256(mcp_tool_contract(ROOT))
    monkeypatch.setattr(tool, "description", "Summary.\nSecond line.\n")
    assert canonical_json_sha256(mcp_tool_contract(ROOT)) == indented
    monkeypatch.setattr(tool, "description", "Summary.\nDifferent meaning.\n")
    assert canonical_json_sha256(mcp_tool_contract(ROOT)) != indented


def test_managed_main_computes_actual_identity_before_serving(monkeypatch):
    expected = build_runtime_identity(ROOT).compact()
    expected["protocol_version"] = "0.6"
    expected["release_id"] = "old-release"
    monkeypatch.setenv("SOLUTION_AGENT_PROTOCOL_REQUIRED", "1")
    monkeypatch.setenv("SOLUTION_AGENT_EXPECTED_IDENTITY_B64", base64.b64encode(json.dumps(expected).encode()).decode())
    # A caller-supplied fake observed identity must not make the gate pass.
    monkeypatch.setenv("SOLUTION_AGENT_REMOTE_IDENTITY_JSON", json.dumps(expected))
    monkeypatch.setattr(mcp_server, "_solution_root", lambda: ROOT)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **_: pytest.fail("mismatch exposed MCP"))
    with pytest.raises(ProtocolMismatch, match="release_id mismatch"):
        mcp_server.main()
    mcp_server._activation_gate.invalidate()


def test_profile_materializer_and_launcher_preserve_exact_remote_selector(tmp_path):
    source = ROOT / "plugins/solution-agent-harness"
    control = tmp_path / "control"
    profiles = control / "remote-workplaces"
    profiles.mkdir(parents=True)
    work = tmp_path / "work"
    (work / ".solution-agent").mkdir(parents=True)
    (work / ".solution-agent/workplace.id").write_bytes(b"solution-agent-workplace-v1:" + b"a" * 32 + b"\n")
    (profiles / "connection-policy.yaml").write_text(yaml.safe_dump({
        "profiles": {"test": {"profile": "remote-workplaces/test.yaml", "ssh_alias": "TEST-HOST", "lifecycle_ready": True}},
        "policy": {"stage_transport": "solution_agent_harness_only"}}))
    (profiles / "test.yaml").write_text(yaml.safe_dump({"id": "test", "ssh_alias": "TEST-HOST",
        "solution_agent_root": "/opt/runtime space", "local_workdir": str(work),
        "transport": {"mode": "single_process_stdio_ssh", "automatic_reconnect": False}}))
    output = tmp_path / "built"
    built = build(source, source, output, profile_id="TEST", control_root=control)
    alias, command = launcher.route(control, "TEST", output)
    argv = shlex.split(command)
    assert alias == "TEST-HOST"
    assert "SOLUTION_AGENT_PROFILE_ID=TEST" in argv
    assert "SOLUTION_AGENT_ROOT=/opt/runtime space" in argv
    assert "SOLUTION_AGENT_PROTOCOL_REQUIRED=1" in argv
    server = json.loads((output / ".mcp.json").read_text())["mcpServers"]["solution_agent_harness"]
    assert "profile_id" not in server
    assert server["args"][0] == str(output / "scripts/remote_launcher.py")
    assert built["release_id"] == build_runtime_identity(ROOT).release_id


def test_independent_clients_ignore_held_legacy_lock_and_shared_cooldown(tmp_path, monkeypatch):
    """An old live client and one failing SSH must not prevent another startup."""
    state_root = tmp_path / "Codex/solution-agent/transports"
    state_root.mkdir(parents=True)
    state_path = state_root / "test.json"
    state_bytes = b'{"owner_pid":123,"cooldown_until_epoch":9999999999}'
    state_path.write_bytes(state_bytes)
    lock = (state_root / "test.lock").open("w+b")
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(launcher.sys, "argv", ["launcher", "--control-root", str(tmp_path), "--profile", "TEST"])
        monkeypatch.setattr(launcher, "route", lambda *args: ("TEST-HOST", "remote-command"))
        streams = [object(), object(), object()]
        for name, stream in zip(("stdin", "stdout", "stderr"), streams):
            monkeypatch.setattr(launcher.sys, name, SimpleNamespace(buffer=stream))
        barrier = threading.Barrier(2)
        calls = []

        def ssh(argv, **kwargs):
            calls.append((argv, kwargs))
            return 255 if barrier.wait(timeout=5) == 0 else 0

        monkeypatch.setattr(launcher.subprocess, "call", ssh)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(launcher.main)
            second = pool.submit(launcher.main)
            assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [0, 255]
        assert len(calls) == 2  # Exactly one attempt per client, including failure.
        for argv, kwargs in calls:
            assert argv == ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "TEST-HOST", "remote-command"]
            assert list(kwargs.values()) == streams
        assert state_path.read_bytes() == state_bytes
        assert sorted(p.name for p in state_root.iterdir()) == ["test.json", "test.lock"]
    finally:
        lock.seek(0)
        if os.name == "nt":
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()

"""Session identity is execution evidence, never an assignment authorization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import uuid

import pytest

from solution_agent.harness.codex_worker import observe_session_identity
from solution_agent.harness.terminal_commands import TerminalCommandRunner, run_execution


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path):
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    request = {"command_id": directory.name, "task_id": "task", "work_unit_id": "unit",
               "command_sha256": "b" * 64, "worker_request_sha256": "c" * 64,
               "runtime_release_id": "fixture-release"}
    write(directory / "request.json", request)
    control = tmp_path / "control"
    write(directory / "worker.json", {"task_id": "task", "work_unit_id": "unit",
                                      "control": str(control), "output": str(tmp_path / "output")})
    write(control / "claim.json", {**{k: request[k] for k in ("command_id", "task_id", "work_unit_id")},
                                   "request_sha256": request["worker_request_sha256"]})
    identity = str(uuid.uuid4())
    data = (json.dumps({"type": "thread.started", "thread_id": identity}) + "\n"
            + json.dumps({"type": "turn.completed"}) + "\n").encode()
    manifest = {**request, "state": "succeeded"}
    (directory / "stdout.bin").write_bytes(data)
    manifest["stdout"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return directory, request, manifest, identity


def test_exact_command_bound_identity_is_observed_not_authorized(evidence):
    directory, request, manifest, identity = evidence
    result = observe_session_identity(directory, request, manifest)
    assert result["state"] == "observed"
    assert result["thread_id"] == identity
    assert result["command_id"] == request["command_id"]
    assert result["worker_request_sha256"] == request["worker_request_sha256"]
    assert result["reuse_authorized"] is False


@pytest.mark.parametrize("data", [
    b'', b'not json\n', b'{"type":"thread.started","thread_id":',
    b'{"type":"thread.started","thread_id":"a title"}\n',
    b'{"type":"thread.started","thread_id":123}\n', b'[]\n',
    b'{"type":"item.completed","item":{"text":"thread.started fake-id"}}\n',
    b'{"type":"item.completed","item":{"type":"thread.started","thread_id":"fake-id"}}\n',
])
def test_missing_corrupt_partial_or_nested_identity_is_never_accepted(evidence, data):
    directory, request, manifest, _ = evidence
    (directory / "stdout.bin").write_bytes(data)
    manifest["stdout"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    result = observe_session_identity(directory, request, manifest)
    assert result["state"] == "unverified"
    assert "thread_id" not in result


@pytest.mark.parametrize("same", [False, True])
def test_multiple_thread_events_are_not_freely_matched(evidence, same):
    directory, request, manifest, identity = evidence
    data = (directory / "stdout.bin").read_bytes()
    data += (json.dumps({"type": "thread.started", "thread_id": identity if same else str(uuid.uuid4())}) + "\n").encode()
    (directory / "stdout.bin").write_bytes(data)
    manifest["stdout"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    assert observe_session_identity(directory, request, manifest)["reason"] == "session_identity_ambiguous"


@pytest.mark.parametrize("record,key", [
    ("manifest", "command_id"), ("manifest", "task_id"), ("manifest", "work_unit_id"),
    ("manifest", "runtime_release_id"), ("claim", "command_id"),
    ("claim", "request_sha256"), ("claim", "work_unit_id"), ("worker", "task_id"),
])
def test_foreign_or_changed_command_evidence_is_rejected(evidence, record, key):
    directory, request, manifest, _ = evidence
    if record == "manifest":
        manifest[key] = "foreign"
    else:
        path = (directory.parent / "control" / "claim.json" if record == "claim"
                else directory / "worker.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        value[key] = "foreign"
        write(path, value)
    assert observe_session_identity(directory, request, manifest)["state"] == "unverified"


def test_post_capture_stream_changes_cannot_supply_a_valid_foreign_id(evidence):
    directory, request, manifest, _ = evidence
    (directory / "stdout.bin").write_text(
        json.dumps({"type": "thread.started", "thread_id": str(uuid.uuid4())}) + "\n", encoding="utf-8")
    assert observe_session_identity(directory, request, manifest)["reason"] == "session_stream_changed"


def test_finished_state_is_required(evidence):
    directory, request, manifest, _ = evidence
    manifest["state"] = "running"
    assert observe_session_identity(directory, request, manifest)["reason"] == "session_execution_not_finished"


def test_duplicate_json_fields_cannot_select_a_last_matching_identity(evidence):
    directory, request, manifest, identity = evidence
    data = ('{"type":"thread.started","thread_id":"' + identity
            + '","thread_id":"' + str(uuid.uuid4()) + '"}\n').encode()
    (directory / "stdout.bin").write_bytes(data)
    manifest["stdout"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    assert observe_session_identity(directory, request, manifest)["state"] == "unverified"


@pytest.mark.parametrize("which", ["stdout", "claim", "request", "worker"])
def test_absent_records_never_fall_back_to_another_source(evidence, which):
    directory, request, manifest, _ = evidence
    paths = {"stdout": directory / "stdout.bin", "claim": directory.parent / "control" / "claim.json",
             "request": directory / "request.json", "worker": directory / "worker.json"}
    paths[which].unlink()
    assert observe_session_identity(directory, request, manifest)["state"] == "unverified"


def test_parser_limit_does_not_accept_an_identity_from_a_prefix(evidence):
    directory, request, manifest, _ = evidence
    with (directory / "stdout.bin").open("ab") as out:
        out.write(b'x' * (2 * 1024 * 1024 + 1))
    assert observe_session_identity(directory, request, manifest)["reason"] == "session_stream_limit"


@pytest.mark.parametrize("manifest", [[], {"stdout": []}])
def test_malformed_manifest_is_unverified_not_an_exception(evidence, manifest):
    directory, request, original, _ = evidence
    changed = {**original, **manifest} if isinstance(manifest, dict) else manifest
    assert observe_session_identity(directory, request, changed)["state"] == "unverified"


def test_legacy_claim_without_request_fingerprint_is_not_inferred(evidence):
    directory, request, manifest, _ = evidence
    del request["worker_request_sha256"]
    write(directory / "request.json", request)
    assert observe_session_identity(directory, request, manifest)["state"] == "unverified"


def test_terminal_finalization_and_fresh_reader_preserve_identity(tmp_path):
    identity = str(uuid.uuid4())
    packet = {"task_id": "task", "work_unit_id": "unit", "control": str(tmp_path / "control"),
              "output": str(tmp_path / "output")}
    runner = TerminalCommandRunner(tmp_path / "jobs", cwd=tmp_path, shell=sys.executable)
    command = 'print(' + repr(json.dumps({"type": "thread.started", "thread_id": identity})) + ')'
    request = runner.prepare(command, task_id="task", work_unit_id="unit", access_mode="writer",
                             writer_lease_id="fixture-lease", worker_packet=packet)
    write(Path(packet["control"]) / "claim.json", {
        **{k: request[k] for k in ("command_id", "task_id", "work_unit_id")},
        "request_sha256": request["worker_request_sha256"]})
    directory = runner.jobs_root / request["command_id"]
    run_execution(directory)
    fresh = TerminalCommandRunner(runner.jobs_root, cwd=tmp_path, shell=sys.executable)
    result = fresh.execute(command=None, command_id=request["command_id"], task_id="task",
                           work_unit_id="unit", wait_ms=1000, max_preview_bytes=1)
    assert result["state"] == "succeeded"
    assert result["stdout"]["truncated"] is True
    assert result["worker_session"]["thread_id"] == identity
    assert result["worker_session"]["reuse_authorized"] is False
    original_stream = (directory / "stdout.bin").read_bytes()
    (directory / "stdout.bin").write_bytes(b'changed\n')
    changed = fresh.execute(command=None, command_id=request["command_id"], task_id="task",
                            work_unit_id="unit", wait_ms=1000, max_preview_bytes=1)
    assert changed["worker_session"]["state"] == "unverified"
    assert "thread_id" not in changed["worker_session"]
    (directory / "stdout.bin").write_bytes(original_stream)
    manifest_path = directory / "result_manifest.json"
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["worker_session"]["thread_id"] = str(uuid.uuid4())
    write(manifest_path, tampered)
    changed = fresh.execute(command=None, command_id=request["command_id"], task_id="task",
                            work_unit_id="unit", wait_ms=1000, max_preview_bytes=1)
    assert changed["worker_session"]["state"] == "unverified"
    assert "thread_id" not in changed["worker_session"]

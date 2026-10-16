"""Remote Codex adapter for the existing supervised terminal job (no transport)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from solution_agent.execution_port.io_utils import atomic_write_json
from solution_agent.harness.remote_profile import load_remote_deploy_profile, deployment_profiles_path
from solution_agent.orchestrator.prompt import build_worker_prompt
from solution_agent.orchestrator.schema import TaskBook, worker_result_from_dict


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def worker_packet(root: Path, profile_id: str, task_id: str, work_unit_id: str,
                  text: str, phase: str) -> dict:
    if not task_id or not work_unit_id or not text.strip():
        raise ValueError("Codex worker requires task_id, work_unit_id and worker_task text")
    if len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("worker_task exceeds 64 KiB; send a compact task with pinned artifact references")
    if phase not in {"prepare_code", "execute_run", "full"}:
        raise ValueError("unsupported worker_phase")
    profile = load_remote_deploy_profile(deployment_profiles_path(root), profile_id)
    if Path(profile.solution_agent_root).resolve() != root.resolve():
        raise ValueError("worker profile runtime root mismatch")
    key = _digest({"task_id": task_id, "work_unit_id": work_unit_id})
    cwd = Path(profile.project_workdir).resolve()
    control = Path(profile.remote_run_root).resolve() / "codex-workers" / key
    output = cwd / ".solution-agent" / "worker-output" / key
    # Reject existing links/junctions that escape either declared location.
    control.resolve().relative_to(Path(profile.remote_run_root).resolve())
    output.resolve().relative_to(cwd)
    packet = {"task_id": task_id, "work_unit_id": work_unit_id, "text": text,
            "phase": phase, "profile_id": profile_id, "root": str(root.resolve()),
            "cwd": str(cwd), "control": str(control), "output": str(output),
            "codex_path": profile.codex_path}
    if getattr(profile, "worker_pool", None) is not None:
        from solution_agent.harness.worker_sessions import pool_spec
        packet["worker_pool"] = pool_spec(profile, root)
    from solution_agent.harness.worker_help import routing_config, markdown_config
    route = routing_config(root, task_id, work_unit_id, cwd, key)
    if route is not None:
        packet["help_route"] = route
    markdown = markdown_config(root, key, cwd)
    if markdown is not None:
        packet["markdown_snapshot"] = markdown
    return packet


def prepared_submission(packet: dict, jobs_root: Path) -> str | None:
    """Find the original OS job even before the worker has written its claim.

    This is positive identity lookup, never an absence proof for legacy jobs.
    """
    if not jobs_root.exists():
        return None
    entries = list(jobs_root.iterdir())
    if len(entries) > 20000:
        raise ValueError('worker job lookup bound exceeded')
    matches = []
    for directory in entries:
        if directory.name.startswith('.') or not directory.is_dir():
            continue
        request_path = directory / 'request.json'
        if request_path.is_symlink():
            raise ValueError('linked worker job request')
        if not request_path.is_file():
            continue
        request = json.loads(request_path.read_text(encoding='utf-8'))
        if request.get('task_id') != packet['task_id'] or request.get('work_unit_id') != packet['work_unit_id']:
            continue
        if request.get('worker_request_sha256') != _digest(packet):
            raise ValueError('work unit already has a different or unverified job; recover original command')
        if request.get('command_id') != directory.name or request.get('access_mode') != 'writer':
            raise ValueError('worker job identity conflict')
        link = directory / 'worker.json'
        if link.is_symlink() or not link.is_file():
            raise ValueError('worker job link missing; do not relaunch')
        metadata = json.loads(link.read_text(encoding='utf-8'))
        if metadata != {key: packet[key] for key in ('task_id', 'work_unit_id', 'control', 'output')}:
            raise ValueError('worker job link conflict')
        matches.append(directory.name)
    if len(matches) > 1:
        raise ValueError('multiple worker jobs for one work unit; reconcile original commands')
    return matches[0] if matches else None


def submission(packet: dict, *, jobs_root: Path | None = None) -> tuple[str | None, str | None]:
    """Retry retrieves the original command; a started-but-unrecorded claim fails closed."""
    control = Path(packet["control"])
    if control.exists():
        claim_path = control / "claim.json"
        if not claim_path.is_file():
            raise ValueError(f"worker launch uncertain; inspect existing claim, do not resend: {control}")
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        if claim.get("request_sha256") != _digest(packet):
            raise ValueError("work_unit_id already belongs to different worker task content")
        command_id = claim.get("command_id")
        if not command_id:
            raise ValueError("worker claim has no command_id; maintenance inspection required")
        if jobs_root is not None:
            # Existing claims are already positive identity evidence, including
            # claims created before pre-launch links were introduced.
            if len(command_id) != 32 or any(c not in '0123456789abcdef' for c in command_id):
                raise ValueError('invalid worker claim command identity')
            request_path = jobs_root / command_id / 'request.json'
            if request_path.is_symlink():
                raise ValueError('linked worker claim job request')
            if request_path.is_file():
                request = json.loads(request_path.read_text(encoding='utf-8'))
                if any(request.get(key) != value for key, value in
                       (('command_id', command_id), ('task_id', packet['task_id']),
                        ('work_unit_id', packet['work_unit_id']))):
                    raise ValueError('worker claim and OS job identity conflict')
                if request.get('worker_request_sha256', _digest(packet)) != _digest(packet):
                    raise ValueError('worker claim and OS job content conflict')
        return None, command_id
    if jobs_root is not None:
        command_id = prepared_submission(packet, jobs_root)
        if command_id is not None:
            return None, command_id
    return worker_command(packet), None


def worker_command(packet: dict) -> str:
    # Only the packet is encoded; no task text is interpreted as shell syntax.
    encoded = base64.b64encode(json.dumps(packet, ensure_ascii=False).encode()).decode()
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "--packet", encoded])


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def observe_session_identity(command_dir: Path, request: dict, manifest: dict) -> dict:
    """Bind machine-observed CLI identity to one completed original command.

    Evidence only: it grants no assignment/session reuse permission. Never search
    account history, parse assistant prose, or choose among multiple candidates.
    """
    def reject(reason: str) -> dict:
        return {"schema": "harness-worker-session-observation/v1", "state": "unverified",
                "reason": reason, "reuse_authorized": False}

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("session_duplicate_field")
            result[key] = value
        return result

    def read_record(path: Path) -> dict:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise ValueError("session_record_invalid")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(value, dict):
            raise ValueError("session_record_invalid")
        return value

    try:
        if not isinstance(request, dict) or not isinstance(manifest, dict):
            return reject("session_record_invalid")
        if command_dir.is_symlink():
            return reject("session_record_invalid")
        command_id = request.get("command_id")
        if (not isinstance(command_id, str) or len(command_id) != 32
                or any(c not in "0123456789abcdef" for c in command_id)
                or command_dir.name != command_id):
            return reject("session_command_mismatch")
        if read_record(command_dir / "request.json") != request:
            return reject("session_request_mismatch")
        for key in ("command_id", "command_sha256", "task_id", "work_unit_id"):
            if not request.get(key) or manifest.get(key) != request[key]:
                return reject("session_command_mismatch")
        if manifest.get("state") not in {"succeeded", "failed", "cancelled", "lost"}:
            return reject("session_execution_not_finished")
        if manifest.get("runtime_release_id") != request.get("runtime_release_id"):
            return reject("session_runtime_mismatch")
        worker = read_record(command_dir / "worker.json")
        if "worker_pool" in request and request.get("worker_link_sha256") != _digest(worker):
            return reject("session_worker_mismatch")
        if any(worker.get(key) != request[key] for key in ("task_id", "work_unit_id")):
            return reject("session_worker_mismatch")
        control = Path(worker["control"])
        if control.is_symlink():
            return reject("session_record_invalid")
        claim = read_record(control / "claim.json")
        fingerprint = request.get("worker_request_sha256")
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in fingerprint)
                or claim.get("request_sha256") != fingerprint
                or any(claim.get(key) != request[key]
                       for key in ("command_id", "task_id", "work_unit_id"))):
            return reject("session_claim_mismatch")
        stream = command_dir / "stdout.bin"
        if stream.is_symlink() or not stream.is_file():
            return reject("session_stream_invalid")
        # Bound parsing without substituting a truncated preview for full evidence.
        if stream.stat().st_size > 64 * 1024 * 1024:
            return reject("session_stream_limit")
        digest = hashlib.sha256()
        observed = None
        count = 0
        with stream.open("rb") as handle:
            while line := handle.readline(2 * 1024 * 1024 + 1):
                if len(line) > 2 * 1024 * 1024:
                    return reject("session_stream_limit")
                digest.update(line)
                if not line.endswith(b"\n"):
                    return reject("session_stream_incomplete")
                if not line.strip():
                    continue
                event = json.loads(line.decode("utf-8"), object_pairs_hook=unique_fields)
                if not isinstance(event, dict):
                    return reject("session_stream_invalid")
                if event.get("type") == "thread.started":
                    count += 1
                    if count > 1:
                        return reject("session_identity_ambiguous")
                    value = event.get("thread_id")
                    if not isinstance(value, str) or str(uuid.UUID(value)) != value or not uuid.UUID(value).int:
                        return reject("session_identity_invalid")
                    observed = value
        evidence = manifest.get("stdout", {})
        if not isinstance(evidence, dict):
            return reject("session_stream_invalid")
        if (evidence.get("sha256") != digest.hexdigest()
                or evidence.get("bytes") != stream.stat().st_size):
            return reject("session_stream_changed")
        if observed is None:
            return reject("session_identity_missing")
        assignment = request.get("worker_assignment")
        if assignment is not None:
            from solution_agent.harness.worker_sessions import validate_assignment
            validate_assignment(assignment)
            if request.get("worker_assignment_sha256") != _digest(assignment):
                return reject("session_assignment_changed")
            if assignment["thread_id"] is not None and assignment["thread_id"] != observed:
                return reject("session_identity_foreign")
        result = {"schema": "harness-worker-session-observation/v1", "state": "observed",
                "thread_id": observed, "command_id": command_id,
                "task_id": request["task_id"], "work_unit_id": request["work_unit_id"],
                "worker_request_sha256": fingerprint, "stdout_sha256": digest.hexdigest(),
                "runtime_release_id": request.get("runtime_release_id"), "reuse_authorized": False}
        if assignment is not None:
            result["assignment"] = assignment
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return reject("session_evidence_invalid")


def verify_result(output: Path, task_id: str) -> dict:
    """Verify the existing worker result contract, not scientific truth."""
    result_path = output / "result.yaml"
    result_path.resolve().relative_to(output.resolve())
    if result_path.stat().st_size > 256 * 1024:
        raise ValueError("worker result exceeds 256 KiB")
    payload = yaml.safe_load(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker result must be an object")
    if not isinstance(payload.get("outputs", {}), dict):
        raise ValueError("worker outputs must be an object")
    result = worker_result_from_dict(payload)
    result.validate()
    if result.task_id != task_id or result.worker != "codex":
        raise ValueError("worker result identity mismatch")
    names = {"result.yaml", "worker_notes.md", "stdout.log", "stderr.log"}
    names.update(str(value) for value in result.outputs.values() if value)
    if len(names) > 64:
        raise ValueError("too many worker output references")
    hashes = {}
    for name in sorted(names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("worker output must be relative to its result directory")
        path = (output / relative).resolve()
        path.relative_to(output.resolve())
        if not path.is_file():
            raise ValueError(f"worker output missing: {name}")
        hashes[name] = _hash_file(path)
    return {"task_status": result.status, "summary": result.summary_short[:2000],
            "output_dir": str(output), "artifact_hashes": hashes,
            "scientific_acceptance": "requires_manager_review"}


def worker_result_action(receipt: dict) -> str:
    """Explain existing result layers without changing terminal/exit semantics."""
    code = receipt.get("codex_exit_code")
    if type(code) is int and code != 0:
        return f"Worker process exited with code {code}; inspect its logs and any partial artifacts before deciding follow-up."
    if receipt.get("contract_valid") is not True:
        return "Worker result contract is invalid or missing; task completion is unverified. Inspect the original artifacts, do not relaunch blindly."
    if receipt.get("task_status") == "partial":
        return "Worker returned a valid partial result, not final task success or proof of a process failure. Check the requested phase, phase_state and outputs for prepared work awaiting execution or another blocker."
    if receipt.get("task_status") == "failed":
        return "Worker reported task failure despite a valid result contract; inspect task evidence and acceptance checks."
    if receipt.get("unanswered_questions"):
        return "Worker has unresolved questions; retain them and review the original task before accepting completion."
    return "Review the worker artifacts against the original acceptance checks; process and contract success do not establish task or scientific acceptance."


def run_worker(packet: dict) -> int:
    # Re-resolve profile on the executing host; a changed profile cannot redirect a queued job.
    expected = worker_packet(Path(packet["root"]), packet["profile_id"], packet["task_id"],
                             packet["work_unit_id"], packet["text"], packet["phase"])
    base_packet = {key: value for key, value in packet.items() if key != "session_assignment"}
    if expected != base_packet:
        raise ValueError("worker profile changed after dispatch")
    command_id = os.environ.get("SOLUTION_AGENT_COMMAND_ID")
    if not command_id:
        raise ValueError("Codex worker must run inside a Harness supervised terminal job")
    command_dir = Path(os.environ["SOLUTION_AGENT_COMMAND_DIR"])
    thread_id = None
    if "worker_pool" in packet:
        from solution_agent.harness.worker_sessions import validate_dispatch
        thread_id = validate_dispatch(packet, command_dir, command_id)
    elif "session_assignment" in packet:
        raise ValueError("worker session assignment requires a declared pool")
    control, output = Path(packet["control"]), Path(packet["output"])
    control.parent.mkdir(parents=True, exist_ok=True)
    try:
        control.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError("worker already claimed; recover original command_id, never relaunch")
    atomic_write_json(control / "claim.json", {"command_id": command_id,
                      "request_sha256": _digest(base_packet), "task_id": packet["task_id"],
                      "work_unit_id": packet["work_unit_id"]})
    atomic_write_json(command_dir / "worker.json", {
        "task_id": packet["task_id"], "work_unit_id": packet["work_unit_id"],
        "control": str(control), "output": str(output)})
    output.mkdir(parents=True, exist_ok=False)
    task = TaskBook(task_id=packet["task_id"], title=packet["work_unit_id"])
    prompt = build_worker_prompt(task, packet["text"], output_dir=str(output), phase=packet["phase"])
    prompt += "\nOnly list output files actually created in result.yaml. Do not call Harness to delegate recursively.\n"
    markdown = packet.get("markdown_snapshot")
    if markdown:
        prompt += "\nRead these task-specific Markdown instructions before doing the task; follow the instruction hierarchy:\n"
        prompt += json.dumps([str(Path(markdown["directory"]) / name) for name in markdown["manifest"]], ensure_ascii=False)
        prompt += "\nIn worker_notes.md record the snapshot version you read: " + markdown["version"] + "\n"
    route = packet.get("help_route")
    if route:
        from solution_agent.harness.worker_help import initialize, instructions
        initialize(route)
        atomic_write_json(command_dir / "help-watch.json", {
            "command_id": command_id, "task_id": packet["task_id"],
            "work_unit_id": packet["work_unit_id"], "route": route,
            "worker_receipt": str(control / "receipt.json")})
        prompt += instructions(route)
    (control / "worker_prompt.md").write_text(prompt, encoding="utf-8")
    argv = [packet["codex_path"], "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
            "--cd", packet["cwd"], "--json", "--output-last-message", str(control / "last-message.txt"), "-"]
    if thread_id is not None:
        argv[-1:] = ["resume", thread_id, "-"]
    environment = dict(os.environ, SOLUTION_AGENT_TERMINAL_NESTING="1")
    # Inherit the supervisor's process group: timeout/cancel must also stop Codex descendants.
    if route:
        from solution_agent.harness.worker_help import run_with_watch
        code = run_with_watch(argv, prompt, packet["cwd"], environment, route)
    else:
        code = subprocess.run(argv, input=prompt.encode("utf-8"), cwd=packet["cwd"],
                              env=environment, check=False).returncode
    receipt = {"command_id": command_id, "codex_exit_code": code, "contract_valid": False}
    if markdown:
        receipt["markdown_snapshot"] = markdown
    try:
        receipt.update(verify_result(output, packet["task_id"]))
        receipt["contract_valid"] = True
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        receipt["contract_error"] = str(exc)[:1000]
    if route:
        from solution_agent.harness.help_queue import HelpQueue
        queue = HelpQueue(route["mailbox"])
        try:
            receipt["unanswered_questions"] = queue.unresolved_questions()
        finally:
            queue.close()
    atomic_write_json(control / "receipt.json", receipt)
    if route:
        from solution_agent.harness.worker_help import finished
        try:
            finished(route, code, receipt["contract_valid"] and receipt.get("task_status") == "success" and not receipt.get("unanswered_questions"))
        except Exception as exc:
            receipt["help_monitor_error"] = str(exc)[:1000]
            atomic_write_json(control / "receipt.json", receipt)
    print(json.dumps({"worker_receipt": str(control / "receipt.json"), **receipt}, ensure_ascii=False))
    return code if code else (0 if receipt["contract_valid"] and receipt["task_status"] == "success" and not receipt.get("unanswered_questions") else 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True)
    args = parser.parse_args()
    packet = json.loads(base64.b64decode(args.packet, validate=True).decode("utf-8"))
    raise SystemExit(run_worker(packet))


if __name__ == "__main__":
    # Direct script execution is used so the supervisor does not rely on its cwd/PYTHONPATH.
    main()

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
    encoded = base64.b64encode(json.dumps(packet, ensure_ascii=False).encode()).decode()
    # Only the packet is encoded; no task text is interpreted as shell syntax.
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--packet", encoded])
    return command, None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def run_worker(packet: dict) -> int:
    # Re-resolve profile on the executing host; a changed profile cannot redirect a queued job.
    expected = worker_packet(Path(packet["root"]), packet["profile_id"], packet["task_id"],
                             packet["work_unit_id"], packet["text"], packet["phase"])
    if expected != packet:
        raise ValueError("worker profile changed after dispatch")
    command_id = os.environ.get("SOLUTION_AGENT_COMMAND_ID")
    if not command_id:
        raise ValueError("Codex worker must run inside a Harness supervised terminal job")
    control, output = Path(packet["control"]), Path(packet["output"])
    control.parent.mkdir(parents=True, exist_ok=True)
    try:
        control.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError("worker already claimed; recover original command_id, never relaunch")
    atomic_write_json(control / "claim.json", {"command_id": command_id,
                      "request_sha256": _digest(packet), "task_id": packet["task_id"],
                      "work_unit_id": packet["work_unit_id"]})
    command_dir = Path(os.environ["SOLUTION_AGENT_COMMAND_DIR"])
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

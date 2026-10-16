"""Fixed worker assignment inside the existing supervised job store.

Call reserve_assignment only while holding TerminalCommandRunner's launch lock.
The request/predecessor chain is the authority; there is no parallel registry.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_COMMAND = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = {"succeeded", "failed", "cancelled"}


class WorkerAdmissionError(ValueError):
    """Refusal before a new command is prepared; never a retry authorization."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerAdmissionError("worker_pool_duplicate_field")
        result[key] = value
    return result


def read_record(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise WorkerAdmissionError("worker_pool_record_invalid")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise WorkerAdmissionError("worker_pool_record_invalid")
        return value
    except (OSError, ValueError) as exc:
        if isinstance(exc, WorkerAdmissionError):
            raise
        raise WorkerAdmissionError("worker_pool_record_invalid") from exc


def validate_policy(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > 64 * 1024:
        raise ValueError("worker_pool policy exceeds 64 KiB")
    if (not isinstance(value, dict) or set(value) != {"version", "workers", "scopes", "max_running"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("worker_pool requires version, workers, scopes and max_running")
    workers, scopes = value["workers"], value["scopes"]
    if not isinstance(workers, dict) or not 1 <= len(workers) <= 64 or not isinstance(scopes, dict) or not 1 <= len(scopes) <= 64:
        raise ValueError("worker_pool requires 1..64 workers and scopes")
    if type(value["max_running"]) is not int or not 1 <= value["max_running"] <= len(workers):
        raise ValueError("worker_pool.max_running exceeds fixed worker capacity")
    tasks = set()
    normalized = {}
    for scope, spec in scopes.items():
        if (not isinstance(scope, str) or not _ID.fullmatch(scope) or not isinstance(spec, dict)
                or set(spec) != {"task_ids", "max_running"}):
            raise ValueError("worker_pool scope must declare exact tasks and capacity")
        ids = spec["task_ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 256
                or any(not isinstance(t, str) or not t.strip() or len(t) > 128
                       or any(ord(c) < 32 for c in t) for t in ids)
                or len(set(ids)) != len(ids) or tasks.intersection(ids)):
            raise ValueError("worker_pool task membership is invalid or ambiguous")
        tasks.update(ids)
        count = sum(s == scope for s in workers.values())
        if type(spec["max_running"]) is not int or not 1 <= spec["max_running"] <= count:
            raise ValueError("worker_pool scope capacity exceeds its fixed workers")
        normalized[scope] = {"task_ids": sorted(ids), "max_running": spec["max_running"]}
    if any(not isinstance(w, str) or not _ID.fullmatch(w) or not isinstance(s, str) or s not in scopes
           for w, s in workers.items()):
        raise ValueError("worker_pool workers must name a declared scope")
    return {"version": 1, "workers": dict(sorted(workers.items())),
            "scopes": dict(sorted(normalized.items())), "max_running": value["max_running"]}


def pool_spec(profile, root):
    """Read-only description for packets and pinned manager configuration."""
    if getattr(profile, "worker_pool", None) is None:
        return None
    run_root = Path(profile.remote_run_root).resolve()
    workplace = Path(profile.project_workdir).resolve()
    if run_root == workplace or run_root.is_relative_to(workplace):
        raise WorkerAdmissionError("worker_pool_control_inside_workplace")
    jobs_root = (run_root / "terminal-sessions" / profile.terminal_storage_profile_id / "jobs").resolve()
    jobs_root.relative_to(run_root)
    return {"policy": validate_policy(profile.worker_pool), "jobs_root": str(jobs_root),
            "context": {"runtime_root": str(Path(root).resolve()),
                        "workplace": str(workplace), "codex_path": profile.codex_path,
                        "codex_home": str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve())}}


def validate_assignment(value):
    keys = {"schema", "worker_id", "scope_id", "sequence", "previous_command_id", "thread_id"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("schema") != "harness-worker-assignment/v1"
            or any(not isinstance(value[k], str) or not _ID.fullmatch(value[k]) for k in ("worker_id", "scope_id"))
            or type(value["sequence"]) is not int or not 1 <= value["sequence"] <= 1_000_000_000):
        raise WorkerAdmissionError("worker_pool_assignment_invalid")
    previous, thread = value["previous_command_id"], value["thread_id"]
    if value["sequence"] == 1:
        if previous is not None or thread is not None:
            raise WorkerAdmissionError("worker_pool_assignment_invalid")
    else:
        if not isinstance(previous, str) or not _COMMAND.fullmatch(previous) or not isinstance(thread, str):
            raise WorkerAdmissionError("worker_pool_assignment_invalid")
        try:
            if str(uuid.UUID(thread)) != thread or not uuid.UUID(thread).int:
                raise ValueError("invalid identity")
        except ValueError as exc:
            raise WorkerAdmissionError("worker_pool_assignment_invalid") from exc
    return value


def _scope(pool, task):
    matches = [name for name, spec in pool["policy"]["scopes"].items() if task in spec["task_ids"]]
    if len(matches) != 1:
        raise WorkerAdmissionError("worker_pool_task_not_authorized")
    return matches[0]


def _history(jobs_root, pool):
    if str(jobs_root.resolve()) != pool.get("jobs_root"):
        raise WorkerAdmissionError("worker_pool_jobs_root_mismatch")
    if validate_policy(pool["policy"]) != pool["policy"]:
        raise WorkerAdmissionError("worker_pool_policy_invalid")
    entries = list(jobs_root.iterdir()) if jobs_root.exists() else []
    if len(entries) > 20000:
        raise WorkerAdmissionError("worker_pool_history_limit")
    chains = {worker: [] for worker in pool["policy"]["workers"]}
    for directory in entries:
        if not _COMMAND.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != jobs_root.resolve():
            raise WorkerAdmissionError("worker_pool_record_invalid")
        request = read_record(directory / "request.json")
        status = read_record(directory / "status.json")
        if request.get("command_id") != directory.name or status.get("command_id") != directory.name:
            raise WorkerAdmissionError("worker_pool_command_mismatch")
        if "worker_pool" not in request:
            if status.get("state") not in _TERMINAL | {"prepared", "running", "lost"}:
                raise WorkerAdmissionError("worker_pool_state_unknown")
            continue
        if request["worker_pool"] != pool:
            raise WorkerAdmissionError("worker_pool_context_changed")
        assignment = request.get("worker_assignment")
        validate_assignment(assignment)
        if request.get("worker_assignment_sha256") != digest(assignment):
            raise WorkerAdmissionError("worker_pool_assignment_changed")
        worker = assignment.get("worker_id")
        if not isinstance(worker, str) or worker not in chains or assignment.get("scope_id") != _scope(pool, request.get("task_id")):
            raise WorkerAdmissionError("worker_pool_scope_mismatch")
        if pool["policy"]["workers"][worker] != assignment["scope_id"]:
            raise WorkerAdmissionError("worker_pool_scope_mismatch")
        sequence = assignment.get("sequence")
        if type(sequence) is not int or not 1 <= sequence <= 1_000_000_000:
            raise WorkerAdmissionError("worker_pool_sequence_invalid")
        chains[worker].append((directory, request, status))
    for chain in chains.values():
        chain.sort(key=lambda entry: entry[1]["worker_assignment"]["sequence"])
        for index, (_, request, _) in enumerate(chain):
            assignment = request["worker_assignment"]
            previous = chain[index - 1][1]["command_id"] if index else None
            if (assignment["sequence"] != index + 1 or assignment.get("previous_command_id") != previous
                    or (index == 0 and assignment.get("thread_id") is not None)):
                raise WorkerAdmissionError("worker_pool_chain_conflict")
    return chains


def _completed_thread(entry, expected_release):
    from solution_agent.harness.codex_worker import observe_session_identity, verify_result
    directory, request, status = entry
    if status.get("state") not in _TERMINAL:
        raise WorkerAdmissionError("worker_pool_predecessor_not_finished")
    if request.get("runtime_release_id") != expected_release:
        raise WorkerAdmissionError("worker_pool_runtime_changed")
    manifest = read_record(directory / "result_manifest.json")
    observed = observe_session_identity(directory, request, manifest)
    if observed.get("state") != "observed" or manifest.get("worker_session") != observed:
        raise WorkerAdmissionError("worker_pool_identity_unverified")
    release = read_record(directory / "writer_lease_release.json")
    if release.get("released") is not True or any(release.get(k) != request.get(k)
            for k in ("command_id", "task_id", "work_unit_id", "writer_lease_id")):
        raise WorkerAdmissionError("worker_pool_ownership_not_released")
    link = read_record(directory / "worker.json")
    receipt = read_record(Path(link["control"]) / "receipt.json")
    if (receipt.get("command_id") != request["command_id"] or receipt.get("codex_exit_code") != 0
            or receipt.get("contract_valid") is not True or receipt.get("unanswered_questions")):
        raise WorkerAdmissionError("worker_pool_result_unverified")
    try:
        verified = verify_result(Path(link["output"]), request["task_id"])
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerAdmissionError("worker_pool_result_unverified") from exc
    if verified["artifact_hashes"] != receipt.get("artifact_hashes"):
        raise WorkerAdmissionError("worker_pool_result_changed")
    return observed["thread_id"]


def _expected_thread(chain, release):
    if not chain:
        return None
    first = _completed_thread(chain[0], release)
    for _, request, _ in chain[1:]:
        if request["worker_assignment"].get("thread_id") != first:
            raise WorkerAdmissionError("worker_pool_identity_changed")
    if _completed_thread(chain[-1], release) != first:
        raise WorkerAdmissionError("worker_pool_identity_changed")
    return first


def reserve_assignment(jobs_root, packet, runtime_release):
    """Select a fixed worker under the existing launch lock, before job creation."""
    pool = packet["worker_pool"]
    scope = _scope(pool, packet["task_id"])
    chains = _history(jobs_root, pool)
    busy = []
    for worker, chain in chains.items():
        if not chain:
            continue
        state = chain[-1][2].get("state")
        if state in {"prepared", "running"}:
            busy.append(worker)
        elif state not in _TERMINAL:
            raise WorkerAdmissionError("worker_pool_state_unknown")
    policy = pool["policy"]
    if (len(busy) >= policy["max_running"]
            or sum(policy["workers"][w] == scope for w in busy) >= policy["scopes"][scope]["max_running"]):
        raise WorkerAdmissionError("worker_pool_busy")
    for worker in sorted(chains):
        if policy["workers"][worker] != scope or worker in busy:
            continue
        chain = chains[worker]
        thread = _expected_thread(chain, runtime_release)
        return {"schema": "harness-worker-assignment/v1", "worker_id": worker, "scope_id": scope,
                "sequence": len(chain) + 1, "previous_command_id": chain[-1][1]["command_id"] if chain else None,
                "thread_id": thread}
    raise WorkerAdmissionError("worker_pool_busy")


def validate_dispatch(packet, command_dir, command_id):
    """Return a machine-derived thread only after rechecking reservation/evidence."""
    from solution_agent.harness.codex_worker import _digest
    from solution_agent.harness.terminal_commands import _exclusive_job_lock
    pool = packet["worker_pool"]
    jobs_root = command_dir.parent
    with _exclusive_job_lock(jobs_root / ".launch.lock"):
        chains = _history(jobs_root, pool)
        request = read_record(command_dir / "request.json")
        base = {k: v for k, v in packet.items() if k != "session_assignment"}
        assignment = packet.get("session_assignment")
        if (request.get("command_id") != command_id or command_dir.name != command_id
                or request.get("worker_request_sha256") != _digest(base)
                or any(request.get(k) != packet.get(k) for k in ("task_id", "work_unit_id"))
                or not isinstance(assignment, dict) or request.get("worker_assignment") != assignment
                or request.get("worker_assignment_sha256") != digest(assignment)):
            raise WorkerAdmissionError("worker_pool_dispatch_mismatch")
        from solution_agent.harness.codex_worker import worker_command
        encoded = worker_command(packet).encode("utf-8")
        script = command_dir / "request.sh"
        if (script.is_symlink() or script.read_bytes() != encoded + b"\n"
                or request.get("command_sha256") != hashlib.sha256(encoded).hexdigest()
                or request.get("command_bytes") != len(encoded)):
            raise WorkerAdmissionError("worker_pool_command_changed")
        chain = chains.get(assignment["worker_id"], [])
        if not chain or chain[-1][1]["command_id"] != command_id or chain[-1][2].get("state") not in {"prepared", "running"}:
            raise WorkerAdmissionError("worker_pool_reservation_not_current")
        expected = _expected_thread(chain[:-1], request.get("runtime_release_id"))
        if assignment.get("thread_id") != expected:
            raise WorkerAdmissionError("worker_pool_identity_changed")
        return expected

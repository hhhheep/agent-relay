"""Test-only process bridge to the actual Harness owners; never an installed API.

Each call is a new Python process. Only Codex execution is synthetic. Original
requests, capsules, claims, finalization, recovery and artifact collection use
production owners against a temporary test root. No credentials are loaded.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from solution_agent.harness import codex_worker as worker, mcp_server as server
from solution_agent.harness.worker_sessions import digest, WorkerAdmissionError
from tests.test_worker_session_pool import pool_environment, write


def run(root, action):
    root = Path(root).resolve()
    runtime, workplace = root / "runtime", root / "workplace"
    runtime.mkdir(parents=True, exist_ok=True)
    workplace.mkdir(parents=True, exist_ok=True)
    profile = SimpleNamespace(solution_agent_root=str(runtime), project_workdir=str(workplace),
        remote_run_root=str(root / "runs"), codex_path="/synthetic/codex")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(worker, "load_remote_deploy_profile", lambda *_: profile)
        packet = worker.worker_packet(runtime, "FAN", "TASK", "unit", "synthetic", "prepare_code")
        pool = pool_environment(packet, patch)
        for direction, name in enumerate(("alpha", "beta", "gamma")):
            profile.worker_pool["scopes"][name]["task_ids"] = ["TASK-" + str(i) for i in range(10) if i % 3 == direction]
        def execute(*args, **kwargs):
            kwargs.pop("runtime_release_id", None)
            if kwargs.get("command") is not None:
                request = pool.runner.prepare(kwargs["command"], task_id=kwargs["task_id"],
                    work_unit_id=kwargs["work_unit_id"], access_mode=kwargs["access_mode"],
                    writer_lease_id=kwargs["writer_lease_id"], worker_packet=kwargs["worker_packet"],
                    timeout_seconds=kwargs["timeout_seconds"])
                return {**request, "state": "prepared", "command_dir": str(pool.jobs / request["command_id"])}
            directory = pool.jobs / kwargs["command_id"]
            if not (directory / "result_manifest.json").exists():
                request = json.loads((directory / "request.json").read_text())
                return {**request, "state": "prepared", "command_dir": str(directory)}
            return pool.runner.execute(**kwargs)
        manager = SimpleNamespace(base=pool.jobs.parent, profile=SimpleNamespace(
            deployment_profile_name="FAN", default_terminal="main", run_root=Path(profile.remote_run_root),
            slots={"main": SimpleNamespace(cwd=workplace)}), exec=execute)
        patch.setattr(server, "_require_activation", lambda: None)
        patch.setattr(server, "_solution_root", lambda: runtime)
        patch.setattr(server, "_terminal_manager", lambda: manager)
        patch.setattr(server, "_server_identity", lambda: {"release_id": "fixture-release"})
        patch.setattr(server, "_with_server", lambda value: value)
        kind = action["op"]
        if kind == "configuration":
            return {"fingerprint": digest(pool.make("TASK-0")["worker_pool"]), "release": "fixture-release"}
        if kind == "terminal_exec":
            return server.terminal_exec(**action["arguments"])
        if kind in {"complete", "wrong_thread"}:
            directory = pool.jobs / action["command_id"]
            request = json.loads((directory / "request.json").read_text())
            assigned = pool.assigned(request)
            packet = {k: v for k, v in assigned.items() if k != "session_assignment"}
            if kind == "complete":
                manifest = pool.complete(packet, request, finish_unit=False)
                assert len(pool.calls) == 1
                return manifest
            original = (directory / "request.sh").read_bytes()
            assigned["session_assignment"]["thread_id"] = action["foreign_thread"]
            modified = {**request, "worker_assignment": assigned["session_assignment"],
                        "worker_assignment_sha256": digest(assigned["session_assignment"])}
            encoded = worker.worker_command(assigned).encode()
            modified.update(command_sha256=hashlib.sha256(encoded).hexdigest(), command_bytes=len(encoded))
            write(directory / "request.json", modified)
            (directory / "request.sh").write_bytes(encoded + b"\n")
            patch.setenv("SOLUTION_AGENT_COMMAND_ID", request["command_id"])
            patch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(directory))
            try:
                with pytest.raises(WorkerAdmissionError, match="identity_changed"):
                    worker.run_worker(assigned)
                assert not pool.calls and not Path(packet["control"]).exists()
                return {"rejected_before_codex": True}
            finally:
                write(directory / "request.json", request)
                (directory / "request.sh").write_bytes(original)
        if kind == "inspect":
            records = []
            for file in sorted(pool.jobs.glob("*/request.json")):
                request = json.loads(file.read_text())
                link = json.loads((file.parent / "worker.json").read_text())
                manifest = file.parent / "result_manifest.json"
                capsule = pool.store.load(request["task_id"])
                records.append({"request": request, "output": link["output"], "result": json.loads(manifest.read_text()) if manifest.exists() else None,
                    "leases": len(capsule.writer_leases), "active_unit": capsule.active_work_unit,
                    "authoritative": len(capsule.authoritative_artifacts),
                    "artifacts": [a.model_dump(mode="json") for a in capsule.working_artifacts]})
            return records
        raise ValueError("unknown synthetic scenario operation")


if __name__ == "__main__":
    request = json.load(sys.stdin)
    with redirect_stdout(io.StringIO()):
        result = run(request["root"], request["action"])
    print(json.dumps(result))

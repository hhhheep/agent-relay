from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from .schema import TaskBook, now_iso
from ..utils.subprocess_utils import decode_output, hidden_subprocess_kwargs, run_subprocess, subprocess_env


def _state_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "state"


def _remote_call_log_path() -> Path:
    return _state_dir() / "remote_call_log.jsonl"


def _caller_hint() -> str:
    for frame in inspect.stack()[2:8]:
        path = Path(frame.filename)
        if path.name == "adapters.py":
            continue
        try:
            relative = path.relative_to(Path(__file__).resolve().parents[2])
            return f"{relative}:{frame.function}:{frame.lineno}"
        except ValueError:
            return f"{path.name}:{frame.function}:{frame.lineno}"
    return "unknown"


def _log_remote_call(kind: str, host: str, detail: str, returncode: int | None = None, error: str = "") -> None:
    payload = {
        "ts": now_iso(),
        "kind": kind,
        "host": host,
        "caller": _caller_hint(),
        "detail": detail[:500],
    }
    if returncode is not None:
        payload["returncode"] = returncode
        payload["ok"] = returncode == 0
    if error:
        payload["error"] = error[:500]
    path = _remote_call_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def transport_command(binary: str) -> list[str]:
    command = [binary]
    ssh_config = os.environ.get("SOLUTION_AGENT_SSH_CONFIG", "").strip()
    if not ssh_config:
        workspace_candidate = Path(__file__).resolve().parents[2] / ".solution" / "local_secrets" / "ssh_config"
        if workspace_candidate.exists():
            ssh_config = str(workspace_candidate)
    if not ssh_config:
        user_candidate = Path(os.environ.get("USERPROFILE", "")) / ".ssh" / "config"
        if str(user_candidate) not in {".ssh\\config", ".ssh/config"}:
            try:
                if user_candidate.exists():
                    ssh_config = str(user_candidate)
            except PermissionError:
                # Codex sandboxed processes can be denied stat() on the user's
                # ssh config even though ssh.exe can read it. Passing -F keeps
                # transport behavior aligned with the normal user shell.
                ssh_config = str(user_candidate)
    if ssh_config:
        command.extend(["-F", ssh_config])
    return command


def submit_to_pueue(command: str) -> Dict[str, object]:
    if not command_exists("pueue"):
        return {"submitted": False, "reason": "pueue not found on PATH", "command": command}
    completed = run_subprocess(["pueue", "add", command])
    return {
        "submitted": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }


def run_codex(prompt: str, cwd: Path) -> Dict[str, object]:
    if not command_exists("codex"):
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": "codex not found on PATH",
            "command": "codex exec <prompt>",
        }
    completed = run_subprocess(["codex", "exec", "--skip-git-repo-check", prompt], cwd=cwd)
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": "codex exec --skip-git-repo-check <prompt>",
    }


def write_status_json(path: Path, result: Dict[str, object]) -> Path:
    payload = dict(result)
    payload["finished_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


@dataclass
class WorkerOutSummary:
    result_yaml: Path
    status: str
    generated_copied: bool
    fallback_executed: bool = False


def _copy_optional_path(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    if target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    return True


def _materializer_status_to_worker_status(status: str) -> str:
    if status == "validation_failed":
        return "failed"
    return "partial"


def write_materializer_worker_out(
    task: TaskBook,
    out_dir: Path,
    mat_result: Dict[str, Any],
    *,
    mat_run_dir: Path | None = None,
    allow_codex_fallback: bool = False,
) -> WorkerOutSummary:
    """Write the legacy _worker_out contract for a materializer result.

    This is an adapter only. It does not run Codex, launch remote jobs, or mutate
    provider policy. The existing collect() path can consume the files it writes.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    write_status_json(out_dir / "materializer_status.json", mat_result)

    raw_status = str(mat_result.get("status") or "validation_failed")
    worker_status = _materializer_status_to_worker_status(raw_status)
    generated_copied = False
    result_package_copied = False
    context_pack_copied = False
    source_generated = ""
    artifact_notes: list[str] = []
    if mat_run_dir is not None:
        generated_copied = _copy_optional_path(mat_run_dir / "generated", out_dir / "generated")
        result_package_copied = _copy_optional_path(mat_run_dir / "result_package.yaml", out_dir / "result_package.yaml")
        context_pack_copied = _copy_optional_path(mat_run_dir / "context_pack", out_dir / "context_pack")
        _copy_optional_path(mat_run_dir / "raw", out_dir / "raw")
        if (mat_run_dir / "generated").exists():
            source_generated = str(mat_run_dir / "generated")
    else:
        artifact_notes.append("artifacts not copied: mat_run_dir not provided")

    if mat_run_dir is not None and raw_status == "materialized" and not (mat_run_dir / "result_package.yaml").exists():
        worker_status = "failed"
        summary_short = "remote_materializer did not produce result_package.yaml"
    else:
        summary_short = str(
            mat_result.get("summary_short")
            or mat_result.get("escalation_reason")
            or f"remote_materializer status: {raw_status}"
        )

    fallback_requested = raw_status == "escalation_required"
    fallback_allowed = bool(allow_codex_fallback)
    fallback_executed = False
    risks = list(mat_result.get("risks") or [])
    next_actions = list(mat_result.get("next_actions") or [])
    if raw_status == "materialized":
        risks.append("Generated artifacts have not been executed.")
        next_actions.append("Review generated artifacts.")
        next_actions.append("Launch executor worker if approved.")
    elif raw_status in {"ambiguous", "escalation_required"}:
        risks.append("Materializer requested escalation or could not finish the task autonomously.")
        next_actions.append("Review escalation details and decide whether to run Codex fallback.")
    elif raw_status == "validation_failed":
        risks.append("Materializer validation failed.")
        next_actions.append("Inspect validation_result.json and raw model outputs.")

    worker_notes_lines = [
        f"# Remote Materializer Result",
        "",
        f"- task_id: {task.task_id}",
        f"- materializer_status: {raw_status}",
        f"- worker_status: {worker_status}",
        f"- completion_scope: materialization_only",
        f"- artifact_state: generated_unexecuted",
        f"- generated_copied: {str(generated_copied).lower()}",
        f"- result_package_copied: {str(result_package_copied).lower()}",
        f"- context_pack_copied: {str(context_pack_copied).lower()}",
    ]
    if mat_result.get("escalation_reason"):
        worker_notes_lines.extend(["", f"Escalation reason: {mat_result.get('escalation_reason')}"])
    if mat_result.get("validation_summary"):
        worker_notes_lines.extend(["", "Validation summary:", yaml.safe_dump(mat_result.get("validation_summary"), sort_keys=False)])
    if artifact_notes:
        worker_notes_lines.extend(["", *artifact_notes])
    (out_dir / "worker_notes.md").write_text("\n".join(worker_notes_lines) + "\n", encoding="utf-8")
    (out_dir / "stdout.log").write_text(json.dumps(mat_result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "stderr.log").write_text("", encoding="utf-8")

    outputs: Dict[str, str] = {
        "worker_notes": "worker_notes.md",
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "materializer_status": "materializer_status.json",
    }
    if generated_copied:
        outputs["generated"] = "generated/"
    if result_package_copied:
        outputs["result_package"] = "result_package.yaml"
    if context_pack_copied:
        outputs["context_pack"] = "context_pack/"
    if source_generated:
        outputs["materializer_generated_source"] = source_generated

    payload = {
        "task_id": task.task_id,
        "status": worker_status,
        "worker": "remote_materializer",
        "materializer_status": raw_status,
        "completion_scope": "materialization_only",
        "artifact_state": "generated_unexecuted",
        "requires_downstream_execution": True,
        "needs_escalation": raw_status in {"ambiguous", "escalation_required"},
        "recommended_worker": "codex" if raw_status in {"ambiguous", "escalation_required"} else None,
        "started_at": mat_result.get("created_at") or now_iso(),
        "finished_at": now_iso(),
        "summary_short": summary_short,
        "changed_files": [],
        "commands_run": [],
        "outputs": outputs,
        "risks": risks,
        "next_actions": next_actions,
        "fallback": {
            "requested": fallback_requested,
            "allowed": fallback_allowed,
            "executed": fallback_executed,
            "recommended_worker": "codex" if raw_status in {"ambiguous", "escalation_required"} else None,
        },
        "provenance": {
            "source_result_package": str(mat_run_dir / "result_package.yaml") if mat_run_dir is not None else "",
            "adapter_version": "remote_materializer_worker_out_v1",
        },
    }
    result_yaml = out_dir / "result.yaml"
    result_yaml.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return WorkerOutSummary(
        result_yaml=result_yaml,
        status=worker_status,
        generated_copied=generated_copied,
        fallback_executed=fallback_executed,
    )


def is_git_repo(worktree: Path) -> bool:
    completed = run_subprocess(["git", "rev-parse", "--is-inside-work-tree"], cwd=worktree)
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _clean_git_paths(paths: Iterable[str] | None) -> list[str]:
    clean: list[str] = []
    for item in paths or []:
        text = str(item).strip().replace("\\", "/")
        if not text or text.startswith("/") or text.startswith("../") or "/../" in text:
            continue
        clean.append(text)
    return clean


def git_diff(worktree: Path, paths: Iterable[str] | None = None) -> str:
    if not is_git_repo(worktree):
        return "No git diff available: worktree is not a git repository."
    command = ["git", "diff"]
    clean_paths = _clean_git_paths(paths)
    if clean_paths:
        command.extend(["--", *clean_paths])
    completed = run_subprocess(command, cwd=worktree)
    if completed.returncode != 0:
        return completed.stderr
    return completed.stdout


def git_diff_stat(worktree: Path, paths: Iterable[str] | None = None) -> str:
    if not is_git_repo(worktree):
        return "No diff stat available: worktree is not a git repository."
    command = ["git", "diff", "--stat"]
    clean_paths = _clean_git_paths(paths)
    if clean_paths:
        command.extend(["--", *clean_paths])
    completed = run_subprocess(command, cwd=worktree)
    if completed.returncode != 0:
        return completed.stderr
    return completed.stdout


def run_ssh(host: str, remote_command: str, timeout: float | None = None) -> Dict[str, object]:
    if not command_exists("ssh"):
        _log_remote_call("ssh", host, remote_command, returncode=127, error="ssh not found on PATH")
        return {"returncode": 127, "stdout": "", "stderr": "ssh not found on PATH", "command": remote_command}
    try:
        completed = run_subprocess([*transport_command("ssh"), "-n", host, remote_command], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _log_remote_call("ssh", host, remote_command, returncode=124, error=f"ssh command timed out after {timeout} seconds")
        return {
            "returncode": 124,
            "stdout": decode_output(exc.stdout),
            "stderr": decode_output(exc.stderr) + f"\nssh command timed out after {timeout} seconds",
            "command": remote_command,
            "host": host,
            "timed_out": True,
            "timeout_seconds": timeout,
        }
    _log_remote_call("ssh", host, remote_command, returncode=completed.returncode, error=completed.stderr if completed.returncode else "")
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": remote_command,
        "host": host,
    }


def open_ssh_stream(host: str, remote_command: str) -> subprocess.Popen[bytes]:
    _log_remote_call("ssh_stream", host, remote_command)
    if not command_exists("ssh"):
        raise FileNotFoundError("ssh not found on PATH")
    return subprocess.Popen(
        [*transport_command("ssh"), "-n", host, remote_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=subprocess_env(),
        **hidden_subprocess_kwargs(),
    )


def run_remote_materializer(
    task_path: Path,
    output_root: Path,
    env_file: Path,
    policy_path: Path,
    feedback_text: str = "",
    workspace_root: Path | None = None,
) -> Dict[str, object]:
    """Invoke the embedded remote_materializer package and return its result_package dict."""
    import os
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    from ..remote_materializer.model_clients import load_policy_clients
    from ..remote_materializer.controller import run_materializer
    clients = load_policy_clients(policy_path)
    return run_materializer(
        task_path=task_path,
        output_root=output_root,
        reader_client=clients.reader,
        materializer_client=clients.materializer,
        backup_materializer_client=clients.backup_materializer,
        reader_verifier_client=clients.reader_verifier,
        feedback_text=feedback_text,
        workspace_root=workspace_root,
    )


def scp_to_remote(local_path: Path, host: str, remote_path: str) -> Dict[str, object]:
    detail = f"{local_path} -> {remote_path}"
    if not command_exists("scp"):
        _log_remote_call("scp_to", host, detail, returncode=127, error="scp not found on PATH")
        return {"returncode": 127, "stdout": "", "stderr": "scp not found on PATH"}
    completed = run_subprocess([*transport_command("scp"), str(local_path), f"{host}:{remote_path}"])
    _log_remote_call("scp_to", host, detail, returncode=completed.returncode, error=completed.stderr if completed.returncode else "")
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def scp_from_remote(host: str, remote_path: str, local_path: Path) -> Dict[str, object]:
    detail = f"{remote_path} -> {local_path}"
    if not command_exists("scp"):
        _log_remote_call("scp_from", host, detail, returncode=127, error="scp not found on PATH")
        return {"returncode": 127, "stdout": "", "stderr": "scp not found on PATH"}
    local_path.parent.mkdir(parents=True, exist_ok=True)
    completed = run_subprocess([*transport_command("scp"), "-r", f"{host}:{remote_path}", str(local_path)])
    _log_remote_call("scp_from", host, detail, returncode=completed.returncode, error=completed.stderr if completed.returncode else "")
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

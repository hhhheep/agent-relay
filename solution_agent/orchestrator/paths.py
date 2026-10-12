from __future__ import annotations

from pathlib import Path

from ..utils.paths import project_root


WORKSPACE_DIRS = (
    "tasks/inbox",
    "tasks/inbox/processed",
    "tasks/pending",
    "tasks/running",
    "tasks/launching",
    "tasks/launch_failed",
    "tasks/launch_unknown",
    "tasks/completed",
    "tasks/partial",
    "tasks/failed",
    "dispatch/sent",
    "dispatch/inbox",
    "config",
    "worktrees",
    "results",
    "reports",
    "state",
)


def root() -> Path:
    return project_root()


def ensure_orchestrator_dirs() -> None:
    for relative in WORKSPACE_DIRS:
        (root() / relative).mkdir(parents=True, exist_ok=True)


def registry_path() -> Path:
    return root() / "registry.json"


def task_dir(status: str, task_id: str) -> Path:
    return root() / "tasks" / status / task_id


def task_file(status: str, task_id: str) -> Path:
    return task_dir(status, task_id) / "task.md"


def dispatch_dir(task_id: str) -> Path:
    return root() / "dispatch" / "sent" / task_id


def worktree_dir(task_id: str) -> Path:
    return root() / "worktrees" / task_id


def worker_out_dir(task_id: str) -> Path:
    return worktree_dir(task_id) / "_worker_out"


def result_dir(task_id: str) -> Path:
    return root() / "results" / task_id


def result_latest_dir(task_id: str) -> Path:
    return result_dir(task_id) / "latest"


def result_run_dir(task_id: str, run_id: str) -> Path:
    return result_dir(task_id) / "runs" / run_id


def executions_dir() -> Path:
    return root() / "executions"


def state_dir() -> Path:
    return root() / "state"


def project_context_path() -> Path:
    return state_dir() / "project_context.md"


def last_summary_path() -> Path:
    return state_dir() / "last_summary.md"


def memory_path() -> Path:
    return state_dir() / "memory.md"


def task_history_path() -> Path:
    return state_dir() / "task_history.md"


def file_index_path() -> Path:
    return state_dir() / "file_index.md"


def codex_worker_profile_path() -> Path:
    return state_dir() / "codex_worker_profile.md"


def workflow_profile_path() -> Path:
    return state_dir() / "workflow_profile.md"


def remote_execution_profiles_path() -> Path:
    return root() / "config" / "remote_execution_profiles.yaml"

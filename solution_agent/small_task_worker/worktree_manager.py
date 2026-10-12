"""worktree_manager.py — create/remove/list git worktrees for task isolation."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..utils.subprocess_utils import hidden_subprocess_kwargs


class WorktreeError(Exception):
    pass


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        **hidden_subprocess_kwargs(),
    )


def branch_name(task_id: str) -> str:
    return f"wt/{task_id}"


def create_worktree(repo: Path, task_id: str, base_dir: Path) -> Path:
    """
    Create a git worktree at base_dir/task_id on a new branch wt/{task_id}.

    Raises WorktreeError if the branch already exists or git fails.
    Returns the worktree path.
    """
    target = base_dir / task_id
    branch = branch_name(task_id)

    result = _git(["worktree", "add", str(target), "-b", branch], cwd=repo)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed for {task_id!r}: {result.stderr.strip()}"
        )
    return target


def remove_worktree(target: Path, repo: Path) -> None:
    """
    Remove a git worktree and delete its branch.

    Uses --force to handle unclean trees.  Deletes the wt/<task_id> branch
    after worktree removal.  Silently skips if the path no longer exists.
    """
    if not target.exists():
        return

    # Remove worktree registration + directory
    result = _git(["worktree", "remove", str(target), "--force"], cwd=repo)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree remove failed for {target}: {result.stderr.strip()}"
        )

    # Delete the branch (best-effort)
    task_id = target.name
    _git(["branch", "-D", branch_name(task_id)], cwd=repo)


def list_worktrees(repo: Path) -> list[dict]:
    """
    Return a list of dicts with keys: path, head, branch.
    Parses `git worktree list --porcelain` output.
    """
    result = _git(["worktree", "list", "--porcelain"], cwd=repo)
    if result.returncode != 0:
        return []

    worktrees = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[9:], "head": "", "branch": ""}
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line == "bare":
            current["branch"] = "(bare)"
    if current:
        worktrees.append(current)
    return worktrees

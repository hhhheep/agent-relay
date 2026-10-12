"""Sprint 7 tests: worktree_manager.py — create, remove, isolate, list."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from solution_agent.small_task_worker.worktree_manager import (
    WorktreeError,
    branch_name,
    create_worktree,
    list_worktrees,
    remove_worktree,
)


# ── Fixture: minimal git repo ─────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """A bare-minimum git repo with one commit on master."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


# ── 1. create_worktree returns valid path on new branch ───────────────────────

def test_create_worktree_creates_path(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    wt = create_worktree(git_repo, "task_001", base)

    assert wt == base / "task_001"
    assert wt.exists()
    assert (wt / ".git").exists()


def test_create_worktree_branch_name(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    create_worktree(git_repo, "task_001", base)

    result = subprocess.run(
        ["git", "branch", "--list", "wt/task_001"],
        cwd=git_repo, capture_output=True, text=True,
    )
    assert "wt/task_001" in result.stdout


# ── 2. remove_worktree cleans up path and branch ─────────────────────────────

def test_remove_worktree_deletes_path(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    wt = create_worktree(git_repo, "task_002", base)
    assert wt.exists()

    remove_worktree(wt, repo=git_repo)
    assert not wt.exists()


def test_remove_worktree_deletes_branch(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    wt = create_worktree(git_repo, "task_003", base)
    remove_worktree(wt, repo=git_repo)

    result = subprocess.run(
        ["git", "branch", "--list", "wt/task_003"],
        cwd=git_repo, capture_output=True, text=True,
    )
    assert result.stdout.strip() == ""


def test_remove_nonexistent_is_noop(git_repo, tmp_path):
    # Calling remove on a path that doesn't exist should not raise
    remove_worktree(tmp_path / "never_created", repo=git_repo)


# ── 3. isolation: changes in worktree don't affect main tree ─────────────────

def test_worktree_isolation(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    wt = create_worktree(git_repo, "task_iso", base)

    # Write a new file inside the worktree
    (wt / "new_file.txt").write_text("isolated change", encoding="utf-8")

    # Main repo's working tree should not have that file
    assert not (git_repo / "new_file.txt").exists()


# ── 4. list_worktrees shows main + created worktree ──────────────────────────

def test_list_worktrees(git_repo, tmp_path):
    base = tmp_path / "研究工作樹"
    base.mkdir()
    wt = create_worktree(git_repo, "task_list", base)

    worktrees = list_worktrees(git_repo)
    # git outputs forward-slash paths on Windows; normalise before comparing
    paths = [w["path"].replace("\\", "/") for w in worktrees]

    assert git_repo.as_posix() in paths
    assert wt.as_posix() in paths


# ── 5. duplicate task_id raises WorktreeError ─────────────────────────────────

def test_duplicate_task_id_raises(git_repo, tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    create_worktree(git_repo, "task_dup", base)

    with pytest.raises(WorktreeError):
        create_worktree(git_repo, "task_dup", base)


# ── 6. branch_name helper ────────────────────────────────────────────────────

def test_branch_name():
    assert branch_name("my_task_123") == "wt/my_task_123"

"""spec_builder.py — thin wrapper over execution_port.spec_builder for TaskContract."""
from __future__ import annotations

from pathlib import Path

from ..execution_port.spec_builder import build_spec_from_execution_block
from .task_contract import TaskContract


def build_execution_spec(task: TaskContract, worktree: Path) -> dict:
    """Build execution_spec dict from TaskContract.execution block + worktree."""
    return build_spec_from_execution_block(
        execution=task.execution,
        task_id=task.task_id,
        task_type=task.task_type,
        worktree=worktree,
    )

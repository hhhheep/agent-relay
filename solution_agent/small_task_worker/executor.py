"""Apply file replacements and run allowed smoke commands."""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import List, Optional, Protocol

from .scope_planner import ProposedScope
from .task_contract import TaskContract
from ..utils.subprocess_utils import hidden_subprocess_kwargs


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass
class ExecutionResult:
    patch_applied: bool
    patch_error: str
    commands: List[CommandResult] = field(default_factory=list)

    @property
    def all_commands_passed(self) -> bool:
        return self.patch_applied and all(c.succeeded for c in self.commands)

    @property
    def first_failure(self) -> Optional[CommandResult]:
        return next((c for c in self.commands if not c.succeeded), None)


class PatchAdapter(Protocol):
    def generate_patch(
        self,
        task: TaskContract,
        scope: ProposedScope,
        context: str,
        feedback: str,
    ) -> str: ...


class FakePatchAdapter:
    """Deterministic fake — returns an empty no-op patch."""

    def __init__(self, patch: str = ""):
        self._patch = patch

    def generate_patch(
        self,
        task: TaskContract,
        scope: ProposedScope,
        context: str,
        feedback: str,
    ) -> str:
        return self._patch


_FILE_BLOCK_RE = re.compile(
    r'```[a-z]*\s+file=["\']([^"\']+)["\']\s*\n(.*?)```',
    re.DOTALL,
)


def _parse_file_blocks(patch_text: str) -> dict[str, str]:
    """Extract {path: content} from fenced file blocks."""
    return {m.group(1): m.group(2) for m in _FILE_BLOCK_RE.finditer(patch_text)}


def _apply_file_replacements(
    patch_text: str,
    worktree: Path,
    allowed_modify: List[str],
) -> tuple[bool, str, str]:
    """Write new file contents. Returns (success, error_msg, unified_diff)."""
    if not patch_text.strip():
        return True, "", ""
    blocks = _parse_file_blocks(patch_text)
    if not blocks:
        return True, "", ""

    diff_parts: list[str] = []
    for rel_path, new_content in blocks.items():
        # Safety: only write files within allowed_modify
        if allowed_modify and not any(rel_path.startswith(p) for p in allowed_modify):
            return False, f"file outside allowed_modify: {rel_path!r}", ""
        target = worktree / rel_path
        old_content = target.read_text(encoding="utf-8") if target.exists() else ""
        # Build diff for audit trail
        diff = "".join(unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        ))
        if diff:
            diff_parts.append(diff)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return False, f"write failed for {rel_path}: {exc}", ""

    return True, "", "\n".join(diff_parts)


def _run_command(cmd: str, worktree: Path, timeout: int = 120) -> CommandResult:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(worktree),
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
        duration = int((time.monotonic() - t0) * 1000)
        return CommandResult(
            command=cmd,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration,
        )
    except subprocess.TimeoutExpired:
        duration = int((time.monotonic() - t0) * 1000)
        return CommandResult(
            command=cmd,
            exit_code=124,
            stdout="",
            stderr=f"command timed out after {timeout}s",
            duration_ms=duration,
        )
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return CommandResult(
            command=cmd,
            exit_code=1,
            stdout="",
            stderr=str(exc),
            duration_ms=duration,
        )


def execute(
    task: TaskContract,
    scope: ProposedScope,
    patch_text: str,
    worktree: Path,
    out_dir: Path,
) -> ExecutionResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    applied, patch_err, actual_diff = _apply_file_replacements(
        patch_text, worktree, scope.modify_paths
    )
    # Save actual diff (computed by Python, not model-generated)
    (out_dir / "patch.diff").write_text(actual_diff, encoding="utf-8")

    result = ExecutionResult(patch_applied=applied, patch_error=patch_err)

    if not applied:
        (out_dir / "stderr.log").write_text(
            f"patch apply failed:\n{patch_err}", encoding="utf-8"
        )
        (out_dir / "stdout.log").write_text("", encoding="utf-8")
        return result

    all_stdout: list[str] = []
    all_stderr: list[str] = []

    for cmd in scope.commands_to_run:
        cmd_result = _run_command(cmd, worktree)
        result.commands.append(cmd_result)
        all_stdout.append(f"$ {cmd}\n{cmd_result.stdout}")
        all_stderr.append(f"$ {cmd}\n{cmd_result.stderr}")
        if not cmd_result.succeeded:
            break  # stop at first failure; feedback loop handles repair

    (out_dir / "stdout.log").write_text("\n".join(all_stdout), encoding="utf-8")
    (out_dir / "stderr.log").write_text("\n".join(all_stderr), encoding="utf-8")

    return result

"""Scope planner — ask model for proposed_scope before any edits."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol

from .task_contract import TaskContract


@dataclass
class ProposedScope:
    risk: str                          # "low" | "medium" | "high"
    inspect_paths: List[str]
    modify_paths: List[str]
    commands_to_run: List[str]
    reason: str
    escalation_required: bool = False
    escalation_reason: str = ""

    def is_safe(self) -> bool:
        return self.risk in ("low", "medium") and not self.escalation_required

    def to_dict(self) -> dict:
        return {
            "risk": self.risk,
            "inspect_paths": self.inspect_paths,
            "modify_paths": self.modify_paths,
            "commands_to_run": self.commands_to_run,
            "reason": self.reason,
            "escalation_required": self.escalation_required,
            "escalation_reason": self.escalation_reason,
        }


class ScopeAdapter(Protocol):
    def propose_scope(self, task: TaskContract, context: str) -> ProposedScope: ...


class FakeScopeAdapter:
    """Deterministic fake for tests — returns a minimal low-risk scope."""

    def __init__(self, response: Optional[ProposedScope] = None):
        self._response = response

    def propose_scope(self, task: TaskContract, context: str) -> ProposedScope:
        if self._response is not None:
            return self._response
        modify = task.allowed_modify[:1] if task.allowed_modify else []
        commands = task.allowed_commands[:1] if task.allowed_commands else []
        return ProposedScope(
            risk="low",
            inspect_paths=list(task.context_paths[:2]),
            modify_paths=modify,
            commands_to_run=commands,
            reason="fake scope for testing",
        )


_FORBIDDEN_COMMAND_PREFIXES = (
    "rm -rf", "sudo", "chmod 777", "curl |", "wget |",
    "pip install", "conda install", "git push", "kill -9",
    "nohup", "tmux", "> /dev/", "dd if=",
)

_SENSITIVE_PATHS = (".env", "secrets/", ".env.", "checkpoint", "dataset_split")


def _check_scope(scope: ProposedScope, task: TaskContract) -> Optional[str]:
    """Return a rejection reason string, or None if scope is acceptable."""
    for cmd in scope.commands_to_run:
        for forbidden in _FORBIDDEN_COMMAND_PREFIXES:
            if cmd.lower().startswith(forbidden):
                return f"forbidden command in scope: {cmd!r}"

    for path in scope.modify_paths:
        for sensitive in _SENSITIVE_PATHS:
            if sensitive in path.lower():
                return f"modify path touches sensitive location: {path!r}"

    if task.allowed_modify:
        for path in scope.modify_paths:
            if not any(path.startswith(allowed) for allowed in task.allowed_modify):
                return f"modify path outside allowed_modify: {path!r}"

    if task.allowed_commands:
        for cmd in scope.commands_to_run:
            allowed = any(
                cmd.startswith(prefix.rstrip("*"))
                for prefix in task.allowed_commands
            )
            if not allowed:
                return f"command not in allowed_commands: {cmd!r}"

    return None


def plan_scope(
    task: TaskContract,
    adapter: ScopeAdapter,
    context: str = "",
    scope_dir: Optional[Path] = None,
) -> ProposedScope:
    """Ask adapter for scope, validate it, write artefacts, return scope."""
    scope = adapter.propose_scope(task, context)

    rejection = _check_scope(scope, task)
    if rejection:
        scope = ProposedScope(
            risk="high",
            inspect_paths=scope.inspect_paths,
            modify_paths=[],
            commands_to_run=[],
            reason=scope.reason,
            escalation_required=True,
            escalation_reason=f"scope check failed: {rejection}",
        )

    if scope_dir is not None:
        scope_dir.mkdir(parents=True, exist_ok=True)
        (scope_dir / "proposed_scope.json").write_text(
            json.dumps(scope.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_lines = [
            f"# Proposed Scope — {task.task_id}\n",
            f"**Risk:** {scope.risk}  ",
            f"**Escalation required:** {scope.escalation_required}\n",
            f"## Reason\n{scope.reason}\n",
            "## Inspect\n" + "\n".join(f"- {p}" for p in scope.inspect_paths),
            "## Modify\n" + "\n".join(f"- {p}" for p in scope.modify_paths),
            "## Commands\n" + "\n".join(f"- `{c}`" for c in scope.commands_to_run),
        ]
        if scope.escalation_reason:
            md_lines.append(f"\n## Escalation Reason\n{scope.escalation_reason}")
        (scope_dir / "proposed_scope.md").write_text(
            "\n".join(md_lines), encoding="utf-8"
        )

    return scope

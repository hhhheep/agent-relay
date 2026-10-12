"""Admission checks for OpenCoder-style small task execution.

Inspired by local reference repos:
- OpenCode gates patch actions through explicit path permissions.
- Aider keeps edits bounded by repo context and validation commands.
- Optio separates task admission from execution/reconciliation.

This module is intentionally deterministic and local. It does not decide
whether the task is scientifically correct; it decides whether the task is
safe and specific enough for a patch worker to start.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable

from .task_contract import TaskContract


@dataclass(frozen=True)
class AdmissionIssue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class AdmissionResult:
    ok: bool
    issues: list[AdmissionIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[AdmissionIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[AdmissionIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "issues": [issue.to_dict() for issue in self.issues],
        }


_ALLOWED_WORKERS = {"small_task_worker", "opencoder_patch_worker", "opencoder"}
_BROAD_PATHS = {"", ".", "./", "*", "**", "/", "\\", "src", "tests"}
_DANGEROUS_ALLOWED_COMMANDS = (
    "rm ",
    "rm-",
    "sudo",
    "git push",
    "git reset",
    "git clean",
    "pip install",
    "conda install",
    "curl ",
    "wget ",
    "nohup",
    "tmux",
)
_LONG_RUNNING_EXPERIMENT_COMMANDS = (
    "torchrun",
    "accelerate launch",
    "deepspeed",
    "python train",
    "python scripts/train",
    "python -m torch.distributed.run",
)
_EXPECTED_PATCH_OUTPUTS = {"patch.diff", "worker_notes.md", "result.yaml"}


def _norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").strip("/")


def _is_absolute_or_parent(path: str) -> bool:
    raw = path.strip()
    if not raw:
        return False
    if PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute():
        return True
    parts = [part for part in raw.replace("\\", "/").split("/") if part]
    return any(part == ".." for part in parts)


def _issue(severity: str, code: str, message: str) -> AdmissionIssue:
    return AdmissionIssue(severity=severity, code=code, message=message)


def _any_prefix_match(path: str, prefixes: Iterable[str]) -> bool:
    normalized = _norm_path(path)
    for prefix in prefixes:
        pfx = _norm_path(prefix)
        if not pfx:
            continue
        if normalized == pfx or normalized.startswith(pfx.rstrip("/") + "/"):
            return True
    return False


def check_opencoder_admission(task: TaskContract) -> AdmissionResult:
    issues: list[AdmissionIssue] = []
    meta = task.raw_front_matter or {}
    worker = str(meta.get("worker") or "small_task_worker").strip()
    if worker and worker not in _ALLOWED_WORKERS:
        issues.append(_issue("error", "worker_not_opencoder_compatible", f"worker is not allowed for small patch execution: {worker}"))

    if task.execution_stage == "run":
        if task.allowed_modify:
            issues.append(_issue("error", "run_stage_declares_allowed_modify", "run-stage tasks must not declare source edit scope; split execution from patch work"))
        if task.run_command and not task.allow_long_running:
            issues.append(_issue("warning", "legacy_run_command_without_long_running_flag", "run_command is legacy; prefer execution.script_id for bounded execution"))
        return AdmissionResult(ok=not any(issue.severity == "error" for issue in issues), issues=issues)

    if not task.allowed_modify:
        issues.append(_issue("error", "missing_allowed_modify", "prepare/analysis patch tasks must declare ## Allowed Modify"))

    for path in task.allowed_modify:
        normalized = _norm_path(path)
        if normalized in _BROAD_PATHS:
            issues.append(_issue("error", "broad_allowed_modify", f"allowed modify path is too broad: {path!r}"))
        if _is_absolute_or_parent(path):
            issues.append(_issue("error", "unsafe_allowed_modify_path", f"allowed modify path must be repo-relative: {path!r}"))

    for path in task.context_paths:
        if _is_absolute_or_parent(path):
            issues.append(_issue("error", "unsafe_context_path", f"context path must be repo-relative: {path!r}"))

    if task.allowed_modify and task.context_paths:
        uncovered = [path for path in task.allowed_modify if not _any_prefix_match(path, task.context_paths) and not any(_any_prefix_match(ctx, [path]) for ctx in task.context_paths)]
        if uncovered:
            issues.append(
                _issue(
                    "warning",
                    "modify_paths_not_covered_by_context",
                    "allowed modify paths are not represented in ## Context Paths: " + ", ".join(uncovered),
                )
            )
    elif task.allowed_modify and not task.context_paths:
        issues.append(_issue("warning", "missing_context_paths", "OpenCoder-style patching should read relevant files before editing"))

    for cmd in task.allowed_commands:
        lowered = cmd.strip().lower()
        if any(lowered.startswith(prefix) for prefix in _DANGEROUS_ALLOWED_COMMANDS):
            issues.append(_issue("error", "dangerous_allowed_command", f"allowed command is too dangerous for patch worker: {cmd!r}"))
        if task.execution_stage != "run" and any(lowered.startswith(prefix) for prefix in _LONG_RUNNING_EXPERIMENT_COMMANDS):
            issues.append(_issue("error", "long_running_experiment_command", f"patch worker allowed command looks like a long experiment run: {cmd!r}"))

    expected_outputs = {item.strip() for item in task.expected_outputs if item.strip()}
    missing_outputs = sorted(_EXPECTED_PATCH_OUTPUTS - expected_outputs)
    if missing_outputs:
        issues.append(_issue("warning", "missing_patch_contract_outputs", "expected outputs should include: " + ", ".join(missing_outputs)))

    if not task.done_definition.strip():
        issues.append(_issue("warning", "missing_done_definition", "task should state ## Done Definition for result validation"))

    return AdmissionResult(ok=not any(issue.severity == "error" for issue in issues), issues=issues)

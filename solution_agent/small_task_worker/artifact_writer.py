"""Write _worker_out artifacts — Python owns all serialization."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml

from .executor import ExecutionResult
from .scope_planner import ProposedScope
from .task_contract import TaskContract


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _determine_status(
    scope: ProposedScope,
    exec_result: Optional[ExecutionResult],
    attempt_count: int,
) -> str:
    if scope.escalation_required:
        return "escalation_required"
    if exec_result is None:
        return "escalation_required"
    if exec_result.all_commands_passed:
        return "succeeded"
    if attempt_count >= 2:
        return "failed"
    return "validation_failed"


def write_artifacts(
    task: TaskContract,
    scope: ProposedScope,
    exec_result: Optional[ExecutionResult],
    out_dir: Path,
    attempt_count: int = 1,
    feedback_used: bool = False,
    extra_notes: str = "",
) -> dict:
    """Write all _worker_out files. Returns the result dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    status = _determine_status(scope, exec_result, attempt_count)

    commands_summary: List[dict] = []
    if exec_result:
        for c in exec_result.commands:
            commands_summary.append({
                "command": c.command,
                "exit_code": c.exit_code,
                "duration_ms": c.duration_ms,
            })

    artifacts_present: List[str] = []
    for name in ("patch.diff", "stdout.log", "stderr.log",
                 "proposed_scope.md", "proposed_scope.json", "feedback_packet.json"):
        if (out_dir / name).exists():
            artifacts_present.append(name)

    patch_applied = exec_result.patch_applied if exec_result else False
    patch_error = exec_result.patch_error if exec_result else ""
    escalation_reason = scope.escalation_reason if scope.escalation_required else ""

    result = {
        "task_id": task.task_id,
        "status": status,
        "attempt_count": attempt_count,
        "feedback_used": feedback_used,
        "scope": {
            "risk": scope.risk,
            "modify_paths": scope.modify_paths,
            "commands": [c["command"] for c in commands_summary],
        },
        "patch": {
            "applied": patch_applied,
            "error": patch_error,
        },
        "commands": commands_summary,
        "artifacts": artifacts_present,
        "escalation_required": {
            "requested": scope.escalation_required,
            "reason": escalation_reason,
        },
        "created_at": _now_iso(),
    }

    # result.yaml — always written first before any optional work
    (out_dir / "result.yaml").write_text(
        yaml.safe_dump(result, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # worker_notes.md
    notes = _build_worker_notes(task, scope, exec_result, status, attempt_count,
                                feedback_used, extra_notes)
    (out_dir / "worker_notes.md").write_text(notes, encoding="utf-8")

    if scope.escalation_required:
        action_requests = {
            "task_id": task.task_id,
            "action_requests": [
                {
                    "kind": "request_codex_review",
                    "missing_or_needed": "codex_handoff_required",
                    "why_needed": escalation_reason or "OpenCoder escalation requires Codex review before execution can continue.",
                    "next_owner": "codex",
                    "blocks": "execution",
                    "blocks_claim_not_execution": False,
                    "blocks_execution": True,
                    "suggested_next_step": "Review the task scope and decide whether Codex should implement or rewrite the handoff.",
                }
            ],
        }
        (out_dir / "action_requests.json").write_text(
            json.dumps(action_requests, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return result


def _build_worker_notes(
    task: TaskContract,
    scope: ProposedScope,
    exec_result: Optional[ExecutionResult],
    status: str,
    attempt_count: int,
    feedback_used: bool,
    extra_notes: str,
) -> str:
    lines = [
        f"# Worker Notes — {task.task_id}",
        f"",
        f"**Status:** {status}  ",
        f"**Attempts:** {attempt_count}  ",
        f"**Feedback used:** {feedback_used}  ",
        f"**Scope risk:** {scope.risk}",
        f"",
    ]

    if scope.escalation_required:
        lines += [
            "## Escalation",
            scope.escalation_reason or "(no reason recorded)",
            "",
        ]

    lines += [
        "## Scope",
        f"Modify paths: {', '.join(scope.modify_paths) or '(none)'}",
        "",
    ]

    if exec_result:
        lines.append("## Commands")
        for c in exec_result.commands:
            mark = "PASS" if c.succeeded else "FAIL"
            lines.append(f"- [{mark}] `{c.command}` (exit {c.exit_code}, {c.duration_ms}ms)")
        lines.append("")

        if not exec_result.patch_applied:
            lines += ["## Patch Error", exec_result.patch_error or "(unknown)", ""]

        failure = exec_result.first_failure
        if failure:
            excerpt = failure.stderr[:800] if failure.stderr else "(no stderr)"
            lines += [
                "## First Failure stderr",
                "```",
                excerpt,
                "```",
                "",
            ]

    if extra_notes:
        lines += ["## Notes", extra_notes, ""]

    return "\n".join(lines)

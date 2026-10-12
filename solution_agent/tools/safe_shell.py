from __future__ import annotations

from pathlib import Path
from typing import Dict

from ..agent.approval_gate import evaluate_command_approval
from ..policies.command_policy import default_timeout_sec, max_timeout_sec
from ..utils.subprocess_utils import run_subprocess


def run_safe_command(
    command: str,
    workspace: Path,
    approval_granted: bool = False,
    timeout_sec: int | None = None,
) -> Dict[str, object]:
    approval = evaluate_command_approval(command, approval_granted=approval_granted)
    if not approval["allowed"]:
        return {
            "command": command,
            "returncode": 126,
            "stdout": "",
            "stderr": approval["reason"],
            "executed": False,
            "approval": approval,
        }

    effective_timeout = default_timeout_sec() if timeout_sec is None else timeout_sec
    effective_timeout = min(effective_timeout, max_timeout_sec())
    completed = run_subprocess(
        command,
        cwd=workspace,
        shell=True,
        timeout=effective_timeout,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "executed": True,
        "approval": approval,
        "timeout_sec": effective_timeout,
    }

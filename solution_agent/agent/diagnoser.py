from __future__ import annotations

from typing import Dict


def diagnose_command_result(returncode: int, stdout: str, stderr: str) -> Dict[str, object]:
    if returncode == 0:
        return {
            "status": "ok",
            "failure_type": None,
            "likely_reason": None,
            "suggested_fix": None,
            "confidence": 0.95,
        }

    stderr_lower = stderr.lower()
    failure_type = "unknown"
    likely_reason = "Command failed."
    suggested_fix = "Inspect stderr and rerun with a narrower command."
    confidence = 0.35

    if "not found" in stderr_lower or "is not recognized" in stderr_lower:
        failure_type = "missing_command"
        likely_reason = "The command is unavailable in PATH."
        suggested_fix = "Use an available executable or install the dependency with approval."
        confidence = 0.8
    elif "requires approval" in stderr_lower:
        failure_type = "approval_required"
        likely_reason = "The command is marked risky by policy and needs explicit approval."
        suggested_fix = "Rerun with explicit approval only if the command is necessary."
        confidence = 0.95
    elif "permission" in stderr_lower or "access" in stderr_lower:
        failure_type = "permission_error"
        likely_reason = "Execution or file access was blocked by policy or OS permissions."
        suggested_fix = "Run inside allowed directories or request approval for the action."
        confidence = 0.75

    return {
        "status": "failed",
        "failure_type": failure_type,
        "likely_reason": likely_reason,
        "suggested_fix": suggested_fix,
        "confidence": confidence,
    }

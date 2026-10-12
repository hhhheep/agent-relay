from __future__ import annotations

from typing import Dict

from ..policies.command_policy import classify_command


def evaluate_command_approval(command: str, approval_granted: bool = False) -> Dict[str, object]:
    decision, reason = classify_command(command)
    if decision == "allow":
        return {
            "allowed": True,
            "requires_approval": False,
            "approval_granted": approval_granted,
            "reason": reason,
        }
    if decision == "approval":
        return {
            "allowed": bool(approval_granted),
            "requires_approval": True,
            "approval_granted": approval_granted,
            "reason": "Approval granted by caller." if approval_granted else reason,
        }
    return {
        "allowed": False,
        "requires_approval": False,
        "approval_granted": approval_granted,
        "reason": reason,
    }

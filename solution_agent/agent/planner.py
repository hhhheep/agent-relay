from __future__ import annotations

from typing import Dict, List


def make_minimal_plan(task: str, initial_command: str | None = None) -> Dict[str, object]:
    steps: List[Dict[str, str]] = [
        {"step": "review task", "status": "pending"},
        {"step": "prepare sandbox workspace", "status": "pending"},
    ]
    if initial_command:
        steps.append({"step": f"run command: {initial_command}", "status": "pending"})
    steps.append({"step": "write report", "status": "pending"})
    return {
        "task": task,
        "mode": "mvp",
        "steps": steps,
    }

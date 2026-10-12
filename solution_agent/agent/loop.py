from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from ..tools.file_editor import replace_in_file, write_text_file
from ..tools.safe_shell import run_safe_command
from ..tools.workspace_manager import ensure_workspace
from ..utils.paths import project_root
from ..utils.run_logger import RunLogger
from .diagnoser import diagnose_command_result
from .planner import make_minimal_plan
from .reporter import write_report
from .verifier import verify_run


def run_task(
    task: str,
    workspace_name: str,
    initial_command: Optional[str] = None,
    report_path: Optional[str] = None,
    approve_risky: bool = False,
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    root = project_root()
    workspace = ensure_workspace(workspace_name)
    run_dir = root / "runs" / run_id
    logger = RunLogger(run_dir)

    plan = make_minimal_plan(task, initial_command=initial_command)
    logger.write_text("task.md", task)
    logger.write_json("plan.json", plan)

    if initial_command:
        command_result = run_safe_command(
            initial_command,
            workspace=workspace,
            approval_granted=approve_risky,
        )
    else:
        command_result = {
            "command": None,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "executed": False,
        }

    logger.write_json("command_result.json", command_result)
    logger.write_text("stdout.log", str(command_result.get("stdout", "")))
    logger.write_text("stderr.log", str(command_result.get("stderr", "")))

    diagnosis = diagnose_command_result(
        returncode=int(command_result["returncode"]),
        stdout=str(command_result["stdout"]),
        stderr=str(command_result["stderr"]),
    )
    verification = verify_run(workspace, int(command_result["returncode"]))
    logger.write_json("diagnosis.json", diagnosis)
    logger.write_json("verification.json", verification)

    report_output = Path(report_path) if report_path else root / "reports" / f"{run_id}.md"
    report_payload = {
        "task": task,
        "run_id": run_id,
        "workspace": str(workspace),
        "plan": plan,
        "command_result": command_result,
        "diagnosis": diagnosis,
        "verification": verification,
    }
    report_output = write_report(report_output, report_payload)
    logger.write_text("report_path.txt", str(report_output))

    summary = "command ok" if int(command_result["returncode"]) == 0 else f"command failed: {diagnosis['failure_type']}"
    return {
        "run_id": run_id,
        "workspace": str(workspace),
        "report_path": str(report_output),
        "summary": summary,
    }


def write_workspace_file(workspace_name: str, relative_path: str, content: str) -> dict:
    workspace = ensure_workspace(workspace_name)
    target = workspace / relative_path
    return write_text_file(target, content)


def patch_workspace_file(workspace_name: str, relative_path: str, old: str, new: str) -> dict:
    workspace = ensure_workspace(workspace_name)
    target = workspace / relative_path
    return replace_in_file(target, old, new)

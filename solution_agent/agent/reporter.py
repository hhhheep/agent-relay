from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def write_report(report_path: Path, payload: Dict[str, object]) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Solution Agent Run Report",
        "",
        f"- Task: {payload['task']}",
        f"- Run ID: {payload['run_id']}",
        f"- Workspace: {payload['workspace']}",
        "",
        "## Plan",
        "```json",
        json.dumps(payload["plan"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Command Result",
        "```json",
        json.dumps(payload["command_result"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Diagnosis",
        "```json",
        json.dumps(payload["diagnosis"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Verification",
        "```json",
        json.dumps(payload["verification"], indent=2, ensure_ascii=False),
        "```",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path

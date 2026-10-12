from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from .schema import TaskBook, now_iso


DEFAULT_EXPECTED_OUTPUTS = [
    "result.yaml",
    "worker_notes.md",
    "metrics.json",
    "patch.diff",
    "stdout.log",
    "stderr.log",
]


def split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        raise ValueError("task.md must start with YAML front matter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("task.md front matter is not closed")
    payload = yaml.safe_load(parts[1]) or {}
    if not isinstance(payload, dict):
        raise ValueError("task.md front matter must be a YAML mapping")
    return payload, parts[2].lstrip()


def load_task(path: Path) -> TaskBook:
    front_matter, body = split_front_matter(path.read_text(encoding="utf-8-sig"))
    task = TaskBook(
        task_id=str(front_matter.get("task_id", "")),
        title=str(front_matter.get("title", "")),
        priority=str(front_matter.get("priority", "normal")),
        worker=str(front_matter.get("worker", "codex")),
        status=str(front_matter.get("status", "pending")),
        created_at=str(front_matter.get("created_at", now_iso())),
        objective=str(front_matter.get("objective", "")),
        success_criteria=[str(item) for item in front_matter.get("success_criteria", []) or []],
        expected_outputs=[str(item) for item in front_matter.get("expected_outputs", []) or []],
        body=body,
        raw_front_matter=front_matter,
    )
    task.validate()
    return task


def task_id_from_path(path: Path) -> str:
    return path.stem.replace(" ", "_")


def ensure_task_front_matter(path: Path, worker: str = "codex") -> str:
    text = path.read_text(encoding="utf-8-sig")
    if text.lstrip("\ufeff").startswith("---"):
        return text.lstrip("\ufeff")
    task_id = task_id_from_path(path)
    title = path.stem.replace("_", " ").replace("-", " ").strip() or task_id
    return f"""---
task_id: {task_id}
title: {title}
priority: normal
worker: {worker}
status: pending
created_at: {now_iso()}

objective: >
  Execute the Markdown task book and collect the required outputs.

success_criteria:
  - Produce result.yaml
  - Produce worker_notes.md
  - Produce stdout.log and stderr.log

expected_outputs:
  - result.yaml
  - worker_notes.md
  - metrics.json
  - patch.diff
  - stdout.log
  - stderr.log
---

{text}
"""


def render_new_task(task_id: str, title: str, worker: str = "codex") -> str:
    created_at = now_iso()
    outputs = "\n".join(f"  - {item}" for item in DEFAULT_EXPECTED_OUTPUTS)
    return f"""---
task_id: {task_id}
title: {title}
priority: normal
worker: {worker}
status: pending
created_at: {created_at}

objective: >
  Describe the experiment or implementation goal.

success_criteria:
  - Produce result.yaml
  - Produce worker_notes.md
  - Preserve existing datasets, checkpoints, and artifacts

expected_outputs:
{outputs}
---

# Task

Describe the work to run, the files to inspect, commands to execute, and what evidence should be collected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schema import now_iso


VALID_TASK_STATUSES = {"pending", "running", "completed", "partial", "failed"}


@dataclass
class TaskBook:
    task_id: str
    title: str
    priority: str = "normal"
    worker: str = "remote_materializer"
    status: str = "pending"
    created_at: str = field(default_factory=now_iso)
    body: str = ""
    raw_front_matter: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.title:
            raise ValueError("title is required")
        if self.status not in VALID_TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
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
        worker=str(front_matter.get("worker", "remote_materializer")),
        status=str(front_matter.get("status", "pending")),
        created_at=str(front_matter.get("created_at", now_iso())),
        body=body,
        raw_front_matter=front_matter,
        execution=dict(front_matter.get("execution") or {}),
    )
    task.validate()
    return task

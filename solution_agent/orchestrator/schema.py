from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


VALID_TASK_STATUSES = {
    "pending",
    "running",
    "launching",
    "launch_failed",
    "launch_unknown",
    "completed",
    "partial",
    "failed",
}
VALID_RESULT_STATUSES = {"success", "failed", "partial"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def as_readable_str(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


@dataclass
class TaskBook:
    task_id: str
    title: str
    priority: str = "normal"
    worker: str = "codex"
    status: str = "pending"
    created_at: str = field(default_factory=now_iso)
    objective: str = ""
    success_criteria: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    body: str = ""
    raw_front_matter: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.title:
            raise ValueError("title is required")
        if self.status not in VALID_TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")


@dataclass
class WorkerResult:
    task_id: str
    status: str
    worker: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    summary_short: str = ""
    changed_files: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    outputs: Dict[str, str] = field(default_factory=dict)
    risks: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id:
            raise ValueError("result task_id is required")
        if self.status not in VALID_RESULT_STATUSES:
            raise ValueError(f"invalid result status: {self.status}")
        if not self.worker:
            raise ValueError("result worker is required")


def worker_result_from_dict(payload: Dict[str, Any]) -> WorkerResult:
    result = WorkerResult(
        task_id=str(payload.get("task_id", "")),
        status=str(payload.get("status", "")),
        worker=str(payload.get("worker", "")),
        started_at=as_optional_str(payload.get("started_at")),
        finished_at=as_optional_str(payload.get("finished_at")),
        summary_short=str(payload.get("summary_short", "")),
        changed_files=[str(item) for item in payload.get("changed_files", []) or []],
        commands_run=[str(item) for item in payload.get("commands_run", []) or []],
        outputs={str(k): str(v) for k, v in (payload.get("outputs", {}) or {}).items()},
        risks=[as_readable_str(item) for item in payload.get("risks", []) or []],
        next_actions=[as_readable_str(item) for item in payload.get("next_actions", []) or []],
        raw=payload,
    )
    result.validate()
    return result

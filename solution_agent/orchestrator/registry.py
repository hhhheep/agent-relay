from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .paths import registry_path, state_dir
from .schema import TaskBook, WorkerResult, now_iso
from .state_machine import InvalidTransitionError, validate_transition


_THREAD_LOCK = threading.RLock()


def _registry_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def registry_journal_path(path: Optional[Path] = None) -> Path:
    target = path or registry_path()
    try:
        if target.resolve() == registry_path().resolve():
            return state_dir() / "registry_events.jsonl"
    except OSError:
        pass
    return target.with_suffix(target.suffix + ".events.jsonl")


def _task_change_events(before: Dict[str, Any], after: Dict[str, Any]) -> list[Dict[str, Any]]:
    before_tasks = before.get("tasks") if isinstance(before.get("tasks"), dict) else {}
    after_tasks = after.get("tasks") if isinstance(after.get("tasks"), dict) else {}
    task_ids = sorted(set(before_tasks.keys()) | set(after_tasks.keys()))
    events: list[Dict[str, Any]] = []
    watched_keys = (
        "status",
        "task_status",
        "remote_health",
        "run_phase",
        "remote_state",
        "latest_run_id",
        "result_path",
        "report_path",
    )
    for task_id in task_ids:
        before_record = before_tasks.get(task_id) if isinstance(before_tasks.get(task_id), dict) else {}
        after_record = after_tasks.get(task_id) if isinstance(after_tasks.get(task_id), dict) else {}
        changed = [key for key in watched_keys if before_record.get(key) != after_record.get(key)]
        if not changed:
            continue
        event = {
            "ts": now_iso(),
            "event": "task_record_changed",
            "task_id": task_id,
            "changed": changed,
        }
        if "status" in changed or "task_status" in changed:
            event["source"] = after_record.get("status_transition_source") or after_record.get("last_status_transition_source") or "unknown"
            event["reason"] = after_record.get("status_transition_reason") or after_record.get("last_status_transition_reason")
            event["actor"] = after_record.get("status_transition_actor") or after_record.get("last_status_transition_actor")
        for key in changed:
            event[f"before_{key}"] = before_record.get(key)
            event[f"after_{key}"] = after_record.get(key)
        events.append(event)
    return events


def _append_registry_events(events: list[Dict[str, Any]], path: Path) -> None:
    if not events:
        return
    journal = registry_journal_path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _append_registry_event_best_effort(event: Dict[str, Any], path: Path) -> None:
    try:
        _append_registry_events([event], path)
    except Exception as exc:
        print(f"registry event write failed: {exc}; original_event={event}", flush=True)


@contextmanager
def _registry_write_lock(path: Path):
    lock_path = _registry_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry_unlocked(path: Optional[Path] = None) -> Dict[str, Any]:
    target = path or registry_path()
    if not target.exists():
        return {"tasks": {}}
    return json.loads(target.read_text(encoding="utf-8-sig"))


def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    return _load_registry_unlocked(path)


def _save_registry_unlocked(payload: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    last_error: Optional[OSError] = None
    for attempt in range(24):
        try:
            tmp.replace(target)
            return target
        except PermissionError as exc:
            last_error = exc
            delay = min(0.05 * (2**attempt), 1.5) + random.uniform(0, 0.05)
            time.sleep(delay)
    if last_error:
        failed_path = target.with_name(f"{target.name}.failed-{int(time.time())}-{uuid.uuid4().hex}.json")
        try:
            tmp.replace(failed_path)
        except Exception:
            failed_path = tmp
        _append_registry_event_best_effort(
            {
                "ts": now_iso(),
                "event": "registry_save_failed",
                "path": str(target),
                "tmp_path": str(tmp),
                "failed_path": str(failed_path),
                "error_type": type(last_error).__name__,
                "winerror": getattr(last_error, "winerror", None),
                "errno": getattr(last_error, "errno", None),
                "error": str(last_error),
                "will_retry_next_loop": True,
            },
            target,
        )
        raise last_error
    return target


def save_registry(payload: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = path or registry_path()
    with _registry_write_lock(target):
        return _save_registry_unlocked(payload, target)


def mutate_registry(mutator: Callable[[Dict[str, Any]], Any], path: Optional[Path] = None) -> Any:
    target = path or registry_path()
    with _registry_write_lock(target):
        payload = _load_registry_unlocked(target)
        payload.setdefault("tasks", {})
        before = json.loads(json.dumps(payload, ensure_ascii=False))
        result = mutator(payload)
        _save_registry_unlocked(payload, target)
        _append_registry_events(_task_change_events(before, payload), target)
        return result


def ensure_registry() -> Path:
    payload = load_registry()
    payload.setdefault("tasks", {})
    return save_registry(payload)


def upsert_task(task: TaskBook, status: Optional[str] = None, **updates: Any) -> Dict[str, Any]:
    def _mutate(payload: Dict[str, Any]) -> Dict[str, Any]:
        tasks = payload.setdefault("tasks", {})
        existing = tasks.get(task.task_id, {})
        next_status = status or existing.get("status") or task.status
        from_status = existing.get("status")
        if from_status and from_status != next_status:
            source = updates.get("status_transition_source", "unknown")
            validate_transition(from_status, next_status, source=source)
        worker = updates.pop("worker", existing.get("worker", task.worker))
        record = {
            "task_id": task.task_id,
            "title": task.title,
            "status": next_status,
            "task_status": updates.pop("task_status", existing.get("task_status", next_status)),
            "worker": worker,
            "created_at": existing.get("created_at", task.created_at),
            "started_at": existing.get("started_at"),
            "finished_at": existing.get("finished_at"),
            "result_path": existing.get("result_path"),
            "report_path": existing.get("report_path"),
            "summary_short": existing.get("summary_short", ""),
        }
        if next_status == "pending":
            record.update({"started_at": None, "finished_at": None, "result_path": None, "report_path": None, "summary_short": ""})
        elif next_status == "running":
            record.update({"finished_at": None, "result_path": None, "report_path": None, "summary_short": ""})
        record.update(updates)
        tasks[task.task_id] = record
        return record

    return mutate_registry(_mutate)


def update_task_record(task_id: str, **updates: Any) -> Dict[str, Any]:
    def _mutate(payload: Dict[str, Any]) -> Dict[str, Any]:
        tasks = payload.setdefault("tasks", {})
        record = tasks.setdefault(task_id, {"task_id": task_id})
        if "status" in updates:
            from_status = record.get("status")
            to_status = updates["status"]
            if from_status and from_status != to_status:
                source = updates.get("status_transition_source", "unknown")
                validate_transition(from_status, to_status, source=source)
        record.update(updates)
        return record

    return mutate_registry(_mutate)


def mark_from_result(task: TaskBook, result: WorkerResult, result_path: Path, report_path: Path) -> Dict[str, Any]:
    if result.status == "success":
        status = "completed"
    elif result.status == "partial":
        status = "partial"
    else:
        status = "failed"
    return update_task_record(
        task.task_id,
        title=task.title,
        status=status,
        task_status=status,
        worker=result.worker,
        finished_at=result.finished_at or now_iso(),
        result_path=str(result_path),
        report_path=str(report_path),
        summary_short=result.summary_short,
        remote_health="reachable",
        run_phase="collected",
        remote_state="collected",
        status_transition_source="collect",
        status_transition_reason=f"worker_result_status:{result.status}",
        status_transition_actor="scheduler",
    )

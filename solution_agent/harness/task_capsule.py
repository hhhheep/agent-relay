from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Iterator, Literal

from pydantic import Field, model_validator

from solution_agent.harness.contracts import StrictModel


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROCESS_LOCK = RLock()


class CapsuleConflict(RuntimeError):
    code = "CAPSULE_CONFLICT"


class ScopeContract(StrictModel):
    readable_paths: list[str] = Field(default_factory=list)
    writable_paths: list[str] = Field(default_factory=list)
    remote_profile_id: str | None = None


class ModelBudget(StrictModel):
    max_requests: int = Field(default=1, gt=0)
    max_context_tokens_estimate: int = Field(default=24000, gt=0)
    max_context_bytes: int = Field(default=65536, gt=0)
    max_output_bytes: int = Field(default=16384, gt=0)


class ArtifactRef(StrictModel):
    artifact_id: str
    sha256: str
    role: str
    selector_receipt_ids: list[str] = Field(default_factory=list)


class WriterLeaseRef(StrictModel):
    lease_id: str
    target: str
    base_sha256: str | None = None
    owner_work_unit: str
    expires_at: datetime


class WaitRef(StrictModel):
    wait_handle: str
    run_id: str
    attempt_id: str
    state_version: int = Field(ge=0)
    resume_trigger: list[str] = Field(default_factory=list)
    manual_resume_required: bool


class TaskCapsule(StrictModel):
    # This is the supported task-state contract, independent of a source build.
    # Incompatible state/ownership semantics require a new schema and migration.
    schema_version: Literal[1] = 1
    task_id: str
    objective: str
    lifecycle_state: str = "READY"
    version: int = Field(default=0, ge=0)
    source_release_id: str
    active_work_unit: str | None = None
    authoritative_artifacts: list[ArtifactRef] = Field(default_factory=list)
    working_artifacts: list[ArtifactRef] = Field(default_factory=list)
    read_receipt_ids: list[str] = Field(default_factory=list)
    writer_leases: list[WriterLeaseRef] = Field(default_factory=list)
    wait: WaitRef | None = None
    next_material_event: str | None = None
    queued_events: list[str] = Field(default_factory=list)
    approval_owner: str
    scope: ScopeContract
    model_budget: ModelBudget = Field(default_factory=ModelBudget)

    @model_validator(mode="after")
    def validate_identity_and_leases(self) -> "TaskCapsule":
        if not _TASK_ID.fullmatch(self.task_id):
            raise ValueError("task_id contains unsupported characters")
        targets = [str(Path(item.target).resolve()).casefold() for item in self.writer_leases]
        if len(targets) != len(set(targets)):
            raise ValueError("writer lease target conflict")
        return self


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers can briefly deny atomic replacement. Never
                # retry the job or relax permissions; retry only this publish.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK, path.open("a+b") as handle:
        handle.seek(0)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TaskCapsuleStore:
    def __init__(self, state_root: Path):
        self.root = state_root.resolve() / "task_capsules"

    def _directory(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("task_id contains unsupported characters")
        return self.root / task_id

    def _capsule_path(self, task_id: str) -> Path:
        return self._directory(task_id) / "capsule.json"

    def create(self, capsule: TaskCapsule) -> TaskCapsule:
        directory = self._directory(capsule.task_id)
        with _file_lock(directory / "capsule.lock"):
            path = directory / "capsule.json"
            if path.exists():
                raise CapsuleConflict(f"capsule already exists: {capsule.task_id}")
            atomic_write_json(path, capsule.model_dump(mode="json"))
        return capsule

    def load(self, task_id: str) -> TaskCapsule:
        path = self._capsule_path(task_id)
        if not path.is_file():
            raise FileNotFoundError(f"unknown task capsule: {task_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise CapsuleConflict("unsupported capsule schema; preserve state and use a reviewed migration before dispatch")
        return TaskCapsule.model_validate(payload)

    def compare_and_swap(
        self,
        task_id: str,
        expected_version: int,
        update: Callable[[TaskCapsule], TaskCapsule],
    ) -> TaskCapsule:
        directory = self._directory(task_id)
        with _file_lock(directory / "capsule.lock"):
            current = self.load(task_id)
            if current.version != expected_version:
                raise CapsuleConflict(
                    f"expected capsule version {expected_version}, observed {current.version}"
                )
            proposed = update(current.model_copy(deep=True))
            if proposed.task_id != task_id:
                raise ValueError("capsule update cannot change task_id")
            proposed = proposed.model_copy(update={"version": current.version + 1})
            proposed = TaskCapsule.model_validate(proposed.model_dump(mode="python"))
            atomic_write_json(directory / "capsule.json", proposed.model_dump(mode="json"))
            return proposed

    def start_work_unit(self, task_id: str, work_unit: str, *, expected_version: int) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            if capsule.active_work_unit is not None:
                raise CapsuleConflict(f"active work unit already exists: {capsule.active_work_unit}")
            return capsule.model_copy(update={"active_work_unit": work_unit, "lifecycle_state": "ACTIVE"})

        return self.compare_and_swap(task_id, expected_version, update)

    def finish_work_unit(self, task_id: str, work_unit: str, *, expected_version: int) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            if capsule.active_work_unit != work_unit:
                raise CapsuleConflict("work unit is not active")
            return capsule.model_copy(update={"active_work_unit": None, "lifecycle_state": "READY"})

        return self.compare_and_swap(task_id, expected_version, update)

    def review_work_unit(self, task_id: str, work_unit: str, *, expected_version: int) -> TaskCapsule:
        """Retain ownership for an explicit finish/continue decision, not execution."""
        def update(capsule: TaskCapsule) -> TaskCapsule:
            if capsule.active_work_unit != work_unit or capsule.writer_leases:
                raise CapsuleConflict("work unit ownership or writer conflict during review transition")
            return capsule.model_copy(update={"lifecycle_state": "REVIEW_REQUIRED"})

        return self.compare_and_swap(task_id, expected_version, update)

    def attach_read_receipt(self, task_id: str, receipt_id: str, *, expected_version: int) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            if receipt_id in capsule.read_receipt_ids:
                return capsule
            return capsule.model_copy(update={"read_receipt_ids": [*capsule.read_receipt_ids, receipt_id]})

        return self.compare_and_swap(task_id, expected_version, update)

    def attach_artifact(
        self,
        task_id: str,
        artifact: ArtifactRef,
        *,
        authoritative: bool,
        expected_version: int,
    ) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            field = "authoritative_artifacts" if authoritative else "working_artifacts"
            current = getattr(capsule, field)
            if any(item.artifact_id == artifact.artifact_id and item.role == artifact.role for item in current):
                return capsule
            return capsule.model_copy(update={field: [*current, artifact]})

        return self.compare_and_swap(task_id, expected_version, update)

    def extend_read_scope(self, task_id: str, paths: list[str], *, expected_version: int) -> TaskCapsule:
        normalized = [str(Path(item).resolve()) for item in paths]

        def update(capsule: TaskCapsule) -> TaskCapsule:
            missing = [item for item in normalized if item not in capsule.scope.readable_paths]
            if not missing:
                return capsule
            scope = capsule.scope.model_copy(
                update={"readable_paths": [*capsule.scope.readable_paths, *missing]}
            )
            return capsule.model_copy(update={"scope": scope})

        return self.compare_and_swap(task_id, expected_version, update)

    def set_wait(self, task_id: str, wait: WaitRef | None, *, expected_version: int) -> TaskCapsule:
        return self.compare_and_swap(
            task_id,
            expected_version,
            lambda capsule: capsule.model_copy(
                update={"wait": wait, "next_material_event": None}
            ),
        )

    def queue_event(self, task_id: str, event_id: str, *, expected_version: int) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            if event_id in capsule.queued_events:
                return capsule
            return capsule.model_copy(
                update={
                    "queued_events": [*capsule.queued_events, event_id],
                    "next_material_event": event_id,
                }
            )

        return self.compare_and_swap(task_id, expected_version, update)

    def acquire_writer_lease(
        self,
        task_id: str,
        lease: WriterLeaseRef,
        *,
        expected_version: int,
    ) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            now = datetime.now(timezone.utc)
            active = [item for item in capsule.writer_leases if item.expires_at > now]
            if active:
                raise CapsuleConflict(f"task already has an active writer lease: {active[0].lease_id}")
            changes = {"writer_leases": [*active, lease]}
            if capsule.active_work_unit == lease.owner_work_unit:
                changes["lifecycle_state"] = "ACTIVE"
            return capsule.model_copy(update=changes)

        return self.compare_and_swap(task_id, expected_version, update)

    def release_writer_lease(self, task_id: str, lease_id: str, *, expected_version: int) -> TaskCapsule:
        def update(capsule: TaskCapsule) -> TaskCapsule:
            leases = [item for item in capsule.writer_leases if item.lease_id != lease_id]
            if len(leases) == len(capsule.writer_leases):
                raise CapsuleConflict(f"unknown writer lease: {lease_id}")
            return capsule.model_copy(update={"writer_leases": leases})

        return self.compare_and_swap(task_id, expected_version, update)

    def record_event(self, task_id: str, event_id: str, payload: dict) -> Path:
        if not _TASK_ID.fullmatch(event_id):
            raise ValueError("event_id contains unsupported characters")
        path = self._directory(task_id) / "events" / f"{event_id}.json"
        if not path.exists():
            atomic_write_json(path, payload)
        return path

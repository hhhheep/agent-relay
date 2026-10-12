from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from solution_agent.harness.artifact_runtime import ArtifactStore, Selector
from solution_agent.harness.contracts import StrictModel
from solution_agent.harness.task_capsule import ArtifactRef, TaskCapsule, TaskCapsuleStore, atomic_write_json
from solution_agent.tools.patch_tools import inspect_patch_files


class ApplyError(RuntimeError):
    code = "APPLY_ERROR"


class WriteConflict(ApplyError):
    code = "WRITE_CONFLICT"


_LITERAL_TEMPLATES = {
    "report-divider-v1": "\n\n---\n\n",
    "newline-v1": "\n",
}


class ApplyPlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: str
    mode: Literal["materialize", "compose", "patch"]
    task_id: str
    capsule_version: int = Field(ge=0)
    target: str
    target_before_sha256: str | None
    input_artifact_ids: list[str]
    candidate_sha256: str
    candidate_bytes: int = Field(ge=0)
    diff_artifact_id: str
    validation_commands: list[str] = Field(default_factory=list)
    expires_at: datetime
    committed: bool = False


class ApplyReceipt(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: str
    task_id: str
    target: str
    before_sha256: str | None
    after_sha256: str
    artifact_id: str
    writer_lease_id: str
    committed_at: datetime
    rollback_path: str | None = None
    rollback_policy: Literal["disabled_no_backup"] = "disabled_no_backup"
    recovered_after_replace: bool = False


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_path(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, roots: list[str]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class ArtifactApplyEngine:
    def __init__(self, state_root: Path, artifact_store: ArtifactStore, capsule_store: TaskCapsuleStore):
        self.root = state_root.resolve() / "artifact_apply"
        self.artifacts = artifact_store
        self.capsules = capsule_store

    def _target(self, task: TaskCapsule, value: str) -> Path:
        target = Path(value).expanduser().resolve()
        if not _inside(target, task.scope.writable_paths):
            raise PermissionError("apply target is outside task writable scope")
        return target

    def _plan_dir(self, task_id: str, plan_id: str) -> Path:
        if not all(char in "0123456789abcdef" for char in plan_id) or len(plan_id) != 64:
            raise ValueError("invalid plan_id")
        return self.root / "plans" / task_id / plan_id

    def _create_plan(
        self,
        task: TaskCapsule,
        mode: Literal["materialize", "compose", "patch"],
        target: Path,
        candidate: bytes,
        input_artifact_ids: list[str],
        expected_target_sha256: str | None,
        validation_commands: list[str],
    ) -> ApplyPlan:
        before = _hash_path(target)
        if before != expected_target_sha256:
            raise WriteConflict(f"stale target hash: expected {expected_target_sha256}, observed {before}")
        candidate_sha = _hash_bytes(candidate)
        diff = self._diff(target, candidate)
        diff_manifest = self.artifacts.put_bytes(
            task,
            io.BytesIO(diff.encode("utf-8")),
            media_type="text/x-diff",
            source_kind="generated",
            source_ref=f"apply:{mode}:{target}",
            provenance={"target": str(target), "candidate_sha256": candidate_sha},
        )
        material = {
            "mode": mode,
            "task_id": task.task_id,
            "capsule_version": task.version,
            "target": str(target),
            "before": before,
            "inputs": input_artifact_ids,
            "candidate": candidate_sha,
        }
        plan_id = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        directory = self._plan_dir(task.task_id, plan_id)
        plan_path = directory / "plan.json"
        if plan_path.exists():
            return ApplyPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        plan = ApplyPlan(
            plan_id=plan_id,
            mode=mode,
            task_id=task.task_id,
            capsule_version=task.version,
            target=str(target),
            target_before_sha256=before,
            input_artifact_ids=input_artifact_ids,
            candidate_sha256=candidate_sha,
            candidate_bytes=len(candidate),
            diff_artifact_id=diff_manifest.artifact_id,
            validation_commands=validation_commands,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(directory / "candidate.bin", candidate)
        atomic_write_json(plan_path, plan.model_dump(mode="json"))
        return plan

    def plan_materialize(
        self,
        task: TaskCapsule,
        artifact_id: str,
        target: str,
        expected_target_sha256: str | None,
    ) -> ApplyPlan:
        resolved = self._target(task, target)
        with self.artifacts.open_content(task, artifact_id) as handle:
            candidate = handle.read()
        manifest = self.artifacts.resolve(task, artifact_id)
        return self._create_plan(
            task,
            "materialize",
            resolved,
            candidate,
            [manifest.artifact_id],
            expected_target_sha256,
            self._validations(resolved),
        )

    def plan_compose(
        self,
        task: TaskCapsule,
        recipe: dict[str, Any],
        target: str,
        expected_target_sha256: str | None,
    ) -> ApplyPlan:
        parts = recipe.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("compose recipe requires parts")
        chunks: list[str] = []
        inputs: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or set(part) - {
                "artifact_id",
                "selector",
                "literal_template_id",
                "separator",
                "markdown_heading",
            }:
                raise ValueError("compose accepts only artifact selectors, registered literals, and Markdown wrappers")
            if part.get("literal_template_id"):
                template_id = str(part["literal_template_id"])
                if template_id not in _LITERAL_TEMPLATES:
                    raise ValueError(f"unknown literal_template_id: {template_id}")
                content = _LITERAL_TEMPLATES[template_id]
            else:
                artifact_id = str(part.get("artifact_id") or "")
                selector = Selector.model_validate(part.get("selector") or {"type": "whole", "value": None})
                selected = self.artifacts.read(task, artifact_id, selector, replay_content=True)
                content = str(selected.get("content") or "")
                inputs.append(self.artifacts.resolve(task, artifact_id).artifact_id)
            heading = str(part.get("markdown_heading") or "").strip()
            chunks.append(f"## {heading}\n\n{content}" if heading else content)
            separator = part.get("separator", "none")
            if separator not in {"none", "newline", "blank_line"}:
                raise ValueError("unsupported compose separator")
            chunks.append({"none": "", "newline": "\n", "blank_line": "\n\n"}[separator])
        candidate = "".join(chunks).encode("utf-8")
        resolved = self._target(task, target)
        return self._create_plan(
            task,
            "compose",
            resolved,
            candidate,
            inputs,
            expected_target_sha256,
            self._validations(resolved),
        )

    def plan_patch(
        self,
        task: TaskCapsule,
        patch_artifact_id: str,
        target: str,
        base_sha256: str | None,
    ) -> ApplyPlan:
        resolved = self._target(task, target)
        with self.artifacts.open_content(task, patch_artifact_id) as handle:
            patch_text = handle.read().decode("utf-8", errors="strict")
        changed = inspect_patch_files(patch_text)
        if len(changed) != 1 or Path(changed[0]).name != resolved.name:
            raise ValueError("patch plan requires exactly one changed file matching target basename")
        candidate = self._apply_patch_in_temporary_tree(resolved, changed[0], patch_text)
        manifest = self.artifacts.resolve(task, patch_artifact_id)
        return self._create_plan(
            task,
            "patch",
            resolved,
            candidate,
            [manifest.artifact_id],
            base_sha256,
            ["git apply --check", *self._validations(resolved)],
        )

    @staticmethod
    def _apply_patch_in_temporary_tree(target: Path, changed_path: str, patch_text: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="harness-apply-") as temporary:
            root = Path(temporary)
            staged = root / changed_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                staged.write_bytes(target.read_bytes())
            result = subprocess.run(
                ["git", "apply", "--unsafe-paths", "-"],
                cwd=root,
                input=patch_text.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(result.stderr.decode("utf-8", errors="replace")[:1200] or "git apply failed")
            if not staged.is_file():
                raise ValueError("patch deleted target; deletion is outside artifact_apply scope")
            return staged.read_bytes()

    def commit(self, task: TaskCapsule, plan_id: str, writer_lease_id: str) -> ApplyReceipt:
        directory = self._plan_dir(task.task_id, plan_id)
        plan_path = directory / "plan.json"
        if not plan_path.is_file():
            raise FileNotFoundError(f"unknown apply plan: {plan_id}")
        plan = ApplyPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        receipt_path = directory / "receipt.json"
        if receipt_path.exists():
            return ApplyReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise ApplyError("apply plan expired")
        current = self.capsules.load(task.task_id)
        lease = next((item for item in current.writer_leases if item.lease_id == writer_lease_id), None)
        if lease is None or lease.expires_at <= datetime.now(timezone.utc):
            raise PermissionError("writer lease is missing or expired")
        target = self._target(current, plan.target)
        if str(Path(lease.target).resolve()).casefold() != str(target).casefold():
            raise PermissionError("writer lease target does not match apply plan")
        candidate = (directory / "candidate.bin").read_bytes()
        if _hash_bytes(candidate) != plan.candidate_sha256:
            raise RuntimeError("apply candidate hash mismatch")
        try:
            self._validate_candidate(target, candidate)
        except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            atomic_write_json(
                directory / "failed_validation.json",
                {
                    "plan_id": plan.plan_id,
                    "candidate_sha256": plan.candidate_sha256,
                    "candidate_path": str(directory / "candidate.bin"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "canonical_target_published": False,
                },
            )
            raise ApplyError("candidate validation failed; canonical target was not published") from exc
        observed = _hash_path(target)
        recovered_after_replace = observed == plan.candidate_sha256
        if not recovered_after_replace:
            if observed != plan.target_before_sha256 or lease.base_sha256 != observed:
                raise WriteConflict("target changed after plan or writer lease acquisition")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{plan.plan_id[:12]}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(candidate)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        after = _hash_path(target)
        if after != plan.candidate_sha256:
            raise RuntimeError("target hash does not match planned candidate after replace")
        manifest = self.artifacts.put_path(
            current,
            target,
            source_kind="patch" if plan.mode == "patch" else "compose" if plan.mode == "compose" else "generated",
            provenance={"plan_id": plan.plan_id, "before_sha256": plan.target_before_sha256},
        )
        if not any(
            item.artifact_id == manifest.artifact_id and item.role == "applied_target"
            for item in current.working_artifacts
        ):
            current = self.capsules.attach_artifact(
                task.task_id,
                ArtifactRef(
                    artifact_id=manifest.artifact_id,
                    sha256=manifest.sha256,
                    role="applied_target",
                ),
                authoritative=False,
                expected_version=current.version,
            )
        receipt = ApplyReceipt(
            plan_id=plan.plan_id,
            task_id=task.task_id,
            target=str(target),
            before_sha256=plan.target_before_sha256,
            after_sha256=after,
            artifact_id=manifest.artifact_id,
            writer_lease_id=writer_lease_id,
            committed_at=datetime.now(timezone.utc),
            recovered_after_replace=recovered_after_replace,
        )
        atomic_write_json(receipt_path, receipt.model_dump(mode="json"))
        atomic_write_json(plan_path, plan.model_copy(update={"committed": True}).model_dump(mode="json"))
        self.capsules.release_writer_lease(task.task_id, writer_lease_id, expected_version=current.version)
        return receipt

    @staticmethod
    def _validations(target: Path) -> list[str]:
        if target.suffix.lower() == ".json":
            return ["json parse"]
        if target.suffix.lower() in {".yaml", ".yml"}:
            return ["yaml parse"]
        return []

    @staticmethod
    def _validate_candidate(target: Path, candidate: bytes) -> None:
        if target.suffix.lower() == ".json":
            json.loads(candidate.decode("utf-8", errors="strict"))
        elif target.suffix.lower() in {".yaml", ".yml"}:
            yaml.safe_load(candidate.decode("utf-8", errors="strict"))

    @staticmethod
    def _diff(target: Path, candidate: bytes) -> str:
        before = target.read_bytes() if target.exists() else b""
        try:
            before_text = before.decode("utf-8", errors="strict").splitlines(keepends=True)
            after_text = candidate.decode("utf-8", errors="strict").splitlines(keepends=True)
        except UnicodeDecodeError:
            return f"Binary artifact change: {len(before)} -> {len(candidate)} bytes\n"
        return "".join(
            difflib.unified_diff(before_text, after_text, fromfile=f"a/{target.name}", tofile=f"b/{target.name}")
        )

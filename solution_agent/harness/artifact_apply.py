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
from pydantic import Field, model_validator

from solution_agent.harness.artifact_runtime import ArtifactStore, Selector
from solution_agent.harness.contracts import StrictModel
from solution_agent.harness.task_capsule import ArtifactRef, TaskCapsule, TaskCapsuleStore, WriterLeaseRef, atomic_write_json, _file_lock
from solution_agent.harness.md_sync import safe_relative_path, resolve_under_root, parse_markdown_snapshot
from solution_agent.tools.patch_tools import inspect_patch_files


class ApplyError(RuntimeError):
    code = "APPLY_ERROR"


class WriteConflict(ApplyError):
    code = "WRITE_CONFLICT"


class PublicationDeclaration(StrictModel):
    schema_version: Literal["managed-markdown/v1"]
    task_id: str
    direction: Literal["draft_to_authority"]
    destination_kind: Literal["local_file"]
    draft_path: str
    destination_path: str
    draft_owner: str = Field(min_length=1)
    review_owner: str = Field(min_length=1)
    publish_owner: str = Field(min_length=1)
    scope: Literal["full_document", "sections"]
    sections: list[str] = Field(default_factory=list)
    accepted_destination_sha256: str | None = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def check_scope(self):
        if self.accepted_destination_sha256 is not None:
            self.accepted_destination_sha256 = self.accepted_destination_sha256.lower()
        if any(not value.strip() for value in (self.draft_owner, self.review_owner, self.publish_owner)):
            raise ValueError("publication owners must be explicit")
        for value in (self.draft_path, self.destination_path):
            if safe_relative_path(value) != value or any(c in value for c in "*?[]") or not value.endswith('.md'):
                raise ValueError("publication requires exact relative Markdown paths")
        if self.draft_path == self.destination_path:
            raise ValueError("draft and authority must be separate")
        if self.scope == 'sections':
            if not self.sections or len(set(self.sections)) != len(self.sections) or any(not s.startswith('managed:') for s in self.sections):
                raise ValueError("section scope requires unique managed: block IDs")
        elif self.sections:
            raise ValueError("full_document cannot declare sections")
        return self


class PublicationPacket(StrictModel):
    # Existing run_research_report_writer.py v1 output, not another packet format.
    schema_version: Literal["1.0"]
    kind: Literal["local_file"]
    commit_required: Literal[True]
    commit_protocol: Literal["local_compare_and_swap"]
    draft_path: str
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_path: str
    destination_record_id: None
    expected_destination_sha256: str | None = Field(pattern=r"^[0-9a-fA-F]{64}$")
    reason: str = Field(min_length=1)
    writer_must_not_commit: Literal[True]

    @model_validator(mode="after")
    def canonical_base(self):
        if self.expected_destination_sha256 is not None:
            self.expected_destination_sha256 = self.expected_destination_sha256.lower()
        return self


class PublicationReview(StrictModel):
    schema_version: Literal["managed-markdown-review/v1"]
    plan_id: str
    diff_artifact_id: str
    candidate_sha256: str
    declaration_sha256: str
    review_owner: str
    publish_owner: str
    decision: Literal["approved", "rejected"]
    evidence_refs: list[str] = Field(min_length=1)


_LITERAL_TEMPLATES = {
    "report-divider-v1": "\n\n---\n\n",
    "newline-v1": "\n",
}


class ApplyPlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: str
    mode: Literal["materialize", "compose", "patch", "publication"]
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
        mode: Literal["materialize", "compose", "patch", "publication"],
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

    def _json_artifact(self, task: TaskCapsule, artifact_id: str) -> dict:
        with self.artifacts.open_content(task, artifact_id) as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("publication metadata exceeds 65536 bytes")
        return json.loads(raw)

    def plan_publication(self, task: TaskCapsule, root: Path, declaration: dict, packet_artifact_id: str) -> ApplyPlan:
        declared = PublicationDeclaration.model_validate(declaration)
        if declared.task_id != task.task_id:
            raise PermissionError("publication task mismatch")
        packet = PublicationPacket.model_validate(self._json_artifact(task, packet_artifact_id))
        draft = resolve_under_root(root, declared.draft_path)
        target = self._target(task, str(resolve_under_root(root, declared.destination_path)))
        if draft == target:
            raise ValueError("draft and authority resolve to the same file")
        def packet_path(value):
            path = Path(value)
            return (path if path.is_absolute() else root / path).resolve()
        if (packet_path(packet.draft_path) != draft or packet_path(packet.destination_path) != target
                or packet.expected_destination_sha256 != declared.accepted_destination_sha256):
            raise ValueError("publication packet/declaration destination or base mismatch")
        manifest = self.artifacts.put_path(task, draft, source_kind="generated")
        if manifest.sha256 != packet.draft_sha256:
            raise ValueError("publication draft hash mismatch")
        with self.artifacts.open_content(task, manifest.artifact_id) as handle:
            candidate = handle.read(524289)
        if len(candidate) > 524288:
            raise ValueError("managed Markdown exceeds 524288 bytes")
        candidate.decode('utf-8', errors='strict')
        if declared.scope == 'sections':
            self._check_publication_sections(target, candidate, declared.sections)
        declaration_manifest = self.artifacts.put_bytes(task,
            io.BytesIO(declared.model_dump_json().encode()), media_type="application/json",
            source_kind="generated", source_ref="managed_markdown declaration")
        packet_id = self.artifacts.resolve(task, packet_artifact_id).artifact_id
        return self._create_plan(task, "publication", target, candidate,
            [packet_id, manifest.artifact_id, declaration_manifest.artifact_id],
            declared.accepted_destination_sha256, ["UTF-8", "managed declaration", "review binding", "read-back SHA-256"])

    @staticmethod
    def _check_publication_sections(target: Path, candidate: bytes, sections: list[str]) -> None:
        # Reuse the existing SYNC parser; do not create another Markdown parser.
        with tempfile.TemporaryDirectory(prefix='harness-sections-') as temporary:
            draft = Path(temporary) / 'draft.md'
            draft.write_bytes(candidate)
            def outside(path):
                snapshot = parse_markdown_snapshot(path, entity_id='publication', endpoint='local', source_path=path.name)
                blocks = sorted((snapshot.block(name) for name in sections), key=lambda b: b.start_line)
                lines = path.read_bytes().splitlines(keepends=True)
                result, cursor = [], 0
                for block in blocks:
                    if block.kind != 'managed' or block.start_line < cursor:
                        raise ValueError("invalid managed section scope")
                    result.extend(lines[cursor:block.start_line])  # Preserve START marker.
                    result.append(('\x00' + block.block_id + '\x00').encode())
                    cursor = block.end_line - 1  # Preserve END marker.
                result.extend(lines[cursor:])
                return b''.join(result)
            if outside(target) != outside(draft):
                raise PermissionError("publication changes bytes outside declared sections")

    def _publication_context(self, task: TaskCapsule, plan: ApplyPlan):
        if plan.mode != 'publication' or len(plan.input_artifact_ids) != 3:
            raise ValueError("not a managed publication plan")
        declaration_id = plan.input_artifact_ids[2]
        declared = PublicationDeclaration.model_validate(self._json_artifact(task, declaration_id))
        if declared.task_id != task.task_id:
            raise PermissionError("publication task mismatch")
        return declared, self.artifacts.resolve(task, declaration_id).sha256

    def _check_publication_review(self, task: TaskCapsule, plan: ApplyPlan, review_artifact_id: str, publisher: str):
        declared, declaration_sha = self._publication_context(task, plan)
        review = PublicationReview.model_validate(self._json_artifact(task, review_artifact_id))
        if (review.decision != 'approved' or review.plan_id != plan.plan_id
                or review.diff_artifact_id != plan.diff_artifact_id or review.candidate_sha256 != plan.candidate_sha256
                or review.declaration_sha256 != declaration_sha or review.review_owner != declared.review_owner
                or review.publish_owner != declared.publish_owner or publisher != declared.publish_owner
                or not all(isinstance(ref, str) and ref.strip() for ref in review.evidence_refs)):
            raise PermissionError("publication review does not approve this exact plan/owner")
        return review

    def commit_publication(self, task: TaskCapsule, plan_id: str, review_artifact_id: str, publisher: str) -> dict:
        directory = self._plan_dir(task.task_id, plan_id)
        with _file_lock(directory / 'publication.lock'):
            return self._commit_publication(task, plan_id, review_artifact_id, publisher)

    def review_publication(self, task: TaskCapsule, plan_id: str, body: dict, publisher: str) -> dict:
        directory = self._plan_dir(task.task_id, plan_id)
        with _file_lock(directory / 'publication.lock'):
            plan = ApplyPlan.model_validate_json((directory / 'plan.json').read_text(encoding='utf-8'))
            review = PublicationReview.model_validate(body)
            raw = review.model_dump_json().encode()
            if len(raw) > 65536:
                raise ValueError("publication metadata exceeds 65536 bytes")
            manifest = self.artifacts.put_bytes(task, io.BytesIO(raw),
                media_type='application/json', source_kind='generated', source_ref='managed Markdown review')
            self._record_publication_review(task, plan, manifest.artifact_id, publisher)
            return self.inspect_publication(task, plan_id)

    def inspect_publication(self, task: TaskCapsule, plan_id: str) -> dict:
        directory = self._plan_dir(task.task_id, plan_id)
        plan = ApplyPlan.model_validate_json((directory / 'plan.json').read_text(encoding='utf-8'))
        self._publication_context(task, plan)
        current = self.capsules.load(task.task_id)
        observed = _hash_path(self._target(current, plan.target))
        result = {'status': 'draft_ready', 'plan_id': plan_id, 'observed_target_sha256': observed}
        review_path, receipt_path = directory / 'publication_review.json', directory / 'receipt.json'
        if review_path.is_file():
            binding = json.loads(review_path.read_text(encoding='utf-8'))
            self._check_publication_review(task, plan, binding['review_artifact_id'], binding['publish_owner'])
            result.update(binding, status='reviewed')
        if receipt_path.is_file():
            if not review_path.is_file():
                raise ApplyError("publication review evidence is missing; receipt retained")
            receipt = ApplyReceipt.model_validate_json(receipt_path.read_text(encoding='utf-8'))
            result.update(receipt=receipt.model_dump(mode='json'),
                          status='verified' if observed == receipt.after_sha256 else 'conflict')
        elif observed not in {plan.target_before_sha256, plan.candidate_sha256}:
            result['status'] = 'conflict'
        elif review_path.is_file() and any(lease.expires_at > datetime.now(timezone.utc)
                and lease.lease_id != 'publication-' + plan_id[:24] for lease in current.writer_leases):
            result['status'] = 'reviewed_waiting_for_writer'
        return result

    def _record_publication_review(self, task, plan, review_artifact_id, publisher):
        directory = self._plan_dir(task.task_id, plan.plan_id)
        self._check_publication_review(task, plan, review_artifact_id, publisher)
        review_id = self.artifacts.resolve(task, review_artifact_id).artifact_id
        binding = {'review_artifact_id': review_id, 'publish_owner': publisher}
        review_path = directory / 'publication_review.json'
        if review_path.exists():
            if json.loads(review_path.read_text(encoding='utf-8')) != binding:
                raise PermissionError("publication review rebinding is forbidden")
        else:
            # Same existing apply-plan directory; no publication queue or database.
            _atomic_bytes(review_path, json.dumps(binding, sort_keys=True).encode())
        return binding

    def _commit_publication(self, task: TaskCapsule, plan_id: str, review_artifact_id: str, publisher: str) -> dict:
        directory = self._plan_dir(task.task_id, plan_id)
        plan = ApplyPlan.model_validate_json((directory / 'plan.json').read_text(encoding='utf-8'))
        binding = self._record_publication_review(task, plan, review_artifact_id, publisher)
        lease_id = 'publication-' + plan_id[:24]
        current = self.capsules.load(task.task_id)
        if not (directory / 'receipt.json').is_file():
            if datetime.now(timezone.utc) >= plan.expires_at:
                raise ApplyError("apply plan expired")
            observed = _hash_path(self._target(current, plan.target))
            if observed not in {plan.target_before_sha256, plan.candidate_sha256}:
                raise WriteConflict("target changed after publication review; draft and review retained")
            own = next((lease for lease in current.writer_leases if lease.lease_id == lease_id), None)
            if own is None or own.expires_at <= datetime.now(timezone.utc):
                current = self.capsules.acquire_writer_lease(task.task_id, WriterLeaseRef(
                    lease_id=lease_id, target=plan.target, base_sha256=plan.target_before_sha256,
                    owner_work_unit=current.active_work_unit or 'artifact_apply',
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1)), expected_version=current.version)
        try:
            receipt = self.commit(current, plan_id, lease_id, _publication=True)
        except (ApplyError, PermissionError):
            latest = self.capsules.load(task.task_id)
            if any(lease.lease_id == lease_id for lease in latest.writer_leases):
                self.capsules.release_writer_lease(task.task_id, lease_id, expected_version=latest.version)
            raise
        # A crash after receipt persistence may have left only our own lease.
        current = self.capsules.load(task.task_id)
        if any(lease.lease_id == lease_id for lease in current.writer_leases):
            self.capsules.release_writer_lease(task.task_id, lease_id, expected_version=current.version)
        observed = _hash_path(self._target(current, receipt.target))
        if observed != receipt.after_sha256:
            raise WriteConflict("published target changed on read-back; original receipt retained, do not republish")
        return {'status': 'verified', 'receipt': receipt.model_dump(mode='json'),
                'read_back_sha256': observed, **binding}

    def commit(self, task: TaskCapsule, plan_id: str, writer_lease_id: str, *, _publication: bool = False) -> ApplyReceipt:
        directory = self._plan_dir(task.task_id, plan_id)
        plan_path = directory / "plan.json"
        if not plan_path.is_file():
            raise FileNotFoundError(f"unknown apply plan: {plan_id}")
        plan = ApplyPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        if plan.mode == 'publication':
            if not _publication:
                raise PermissionError("managed plan requires publication commit and read-back")
            binding = json.loads((directory / 'publication_review.json').read_text(encoding='utf-8'))
            self._check_publication_review(task, plan, binding['review_artifact_id'], binding['publish_owner'])
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

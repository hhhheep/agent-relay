from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import Field
import yaml

from solution_agent.harness.contracts import StrictModel
from solution_agent.harness.task_capsule import CapsuleConflict, TaskCapsule, atomic_write_json


_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_BYTES = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*['\"]?[A-Za-z0-9_./+-]{12,}"),
    re.compile(rb"\b(?:sk|ghp|github_pat)-[A-Za-z0-9_-]{12,}\b"),
)
_CREDENTIAL_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}


class ArtifactAccessError(PermissionError):
    pass


class ArtifactManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_id: str
    sha256: str
    byte_size: int = Field(ge=0)
    media_type: str
    encoding: str | None = None
    source_kind: Literal["path", "stage", "terminal", "generated", "compose", "patch"]
    source_ref: str
    task_scope: str
    sensitivity: Literal["normal", "restricted", "secret_rejected"] = "normal"
    created_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)
    outline_ref: str | None = None


class ArtifactAlias(StrictModel):
    task_id: str
    alias: str
    artifact_id: str
    previous_artifact_id: str | None = None
    version: int = Field(ge=0)


class Selector(StrictModel):
    type: Literal[
        "whole",
        "line_range",
        "markdown_heading",
        "json_pointer",
        "yaml_path",
        "table_slice",
        "search",
        "terminal_stream",
    ]
    value: Any = None


class ReadReceipt(StrictModel):
    read_receipt_id: str
    task_id: str
    artifact_id: str
    artifact_sha256: str
    selector: Selector
    selector_sha256: str
    returned_sha256: str
    returned_bytes: int = Field(ge=0)
    truncated: bool
    created_at: datetime


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _inside_any(path: Path, roots: list[str]) -> bool:
    resolved = path.resolve()
    for item in roots:
        try:
            resolved.relative_to(Path(item).resolve())
            return True
        except ValueError:
            continue
    return False


def _strict_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("selector requires UTF-8 text; inspect or materialize the binary artifact instead") from exc


class ArtifactStore:
    def __init__(
        self,
        state_root: Path,
        *,
        object_max_bytes: int = 128 * 1024 * 1024,
        task_max_bytes: int = 512 * 1024 * 1024,
        global_soft_max_bytes: int = 2 * 1024 * 1024 * 1024,
    ):
        self.root = state_root.resolve() / "artifact_runtime"
        self.object_max_bytes = object_max_bytes
        self.task_max_bytes = task_max_bytes
        self.global_soft_max_bytes = global_soft_max_bytes

    def _object_path(self, digest: str) -> Path:
        return self.root / "objects" / "sha256" / digest[:2] / digest

    def _manifest_path(self, task_id: str, digest: str) -> Path:
        return self.root / "manifests" / task_id / f"{digest}.json"

    def _alias_path(self, task_id: str, alias: str) -> Path:
        if not _ALIAS.fullmatch(alias):
            raise ValueError("artifact alias contains unsupported characters")
        return self.root / "aliases" / task_id / f"{alias}.json"

    def _task_bytes(self, task_id: str) -> int:
        return sum(
            ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8")).byte_size
            for path in (self.root / "manifests" / task_id).glob("*.json")
        )

    def _global_bytes(self) -> int:
        return sum(path.stat().st_size for path in (self.root / "objects" / "sha256").glob("*/*") if path.is_file())

    def put_path(
        self,
        task: TaskCapsule,
        path: Path,
        *,
        alias: str | None = None,
        source_kind: Literal["path", "stage", "terminal", "generated", "compose", "patch"] = "path",
        expected_alias_version: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactManifest:
        candidate = path.expanduser()
        if candidate.is_symlink():
            raise ArtifactAccessError("symlinked artifact sources are rejected")
        resolved = candidate.resolve()
        if not _inside_any(resolved, task.scope.readable_paths):
            raise ArtifactAccessError("artifact source is outside task readable scope")
        stat = resolved.stat()
        if not resolved.is_file():
            raise ValueError("artifact source must be a regular file")
        if resolved.name.casefold() in _CREDENTIAL_NAMES:
            raise ArtifactAccessError("credential file paths are rejected")
        media_type, _ = mimetypes.guess_type(resolved.name)
        with resolved.open("rb") as handle:
            return self.put_bytes(
                task,
                handle,
                media_type=media_type or "application/octet-stream",
                source_kind=source_kind,
                source_ref=str(resolved),
                alias=alias,
                expected_alias_version=expected_alias_version,
                provenance={"source_size": stat.st_size, **(provenance or {})},
            )

    def put_bytes(
        self,
        task: TaskCapsule,
        data: BinaryIO,
        *,
        media_type: str,
        source_kind: Literal["path", "stage", "terminal", "generated", "compose", "patch"],
        source_ref: str,
        alias: str | None = None,
        expected_alias_version: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactManifest:
        temporary_dir = self.root / "objects" / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / f"{os.getpid()}-{secrets.token_hex(8)}.tmp"
        digest = hashlib.sha256()
        size = 0
        carry = b""
        try:
            with temporary.open("xb") as target:
                while chunk := data.read(1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact stream must return bytes")
                    size += len(chunk)
                    if size > self.object_max_bytes:
                        raise ValueError("artifact exceeds object_max_bytes")
                    probe = carry + chunk
                    if any(pattern.search(probe) for pattern in _SECRET_BYTES):
                        raise ArtifactAccessError("high-confidence secret pattern rejected before CAS commit")
                    carry = probe[-256:]
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            hexdigest = digest.hexdigest()
            object_path = self._object_path(hexdigest)
            manifest_path = self._manifest_path(task.task_id, hexdigest)
            if not manifest_path.exists() and self._task_bytes(task.task_id) + size > self.task_max_bytes:
                raise ValueError("artifact task quota exceeded")
            if not object_path.exists() and self._global_bytes() + size > self.global_soft_max_bytes:
                raise ValueError("artifact global soft quota exceeded")
            object_path.parent.mkdir(parents=True, exist_ok=True)
            if object_path.exists():
                if object_path.stat().st_size != size or self._hash_file(object_path) != hexdigest:
                    raise RuntimeError("existing CAS object failed size/hash verification")
                temporary.unlink()
            else:
                if self._hash_file(temporary) != hexdigest:
                    raise RuntimeError("temporary CAS object hash changed during copy")
                os.replace(temporary, object_path)
            outline_ref, encoding = self._write_outline(task.task_id, hexdigest, object_path, media_type)
            manifest = ArtifactManifest(
                artifact_id=f"sha256:{hexdigest}",
                sha256=hexdigest,
                byte_size=size,
                media_type=media_type,
                encoding=encoding,
                source_kind=source_kind,
                source_ref=source_ref,
                task_scope=task.task_id,
                created_at=datetime.now(timezone.utc),
                provenance=provenance or {},
                outline_ref=outline_ref,
            )
            if not manifest_path.exists():
                atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
            else:
                manifest = ArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            if alias is not None:
                self.update_alias(task, alias, manifest.artifact_id, expected_alias_version)
            return manifest
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_outline(self, task_id: str, digest: str, path: Path, media_type: str) -> tuple[str | None, str | None]:
        if path.stat().st_size > 4 * 1024 * 1024 and media_type not in {"text/markdown", "text/plain"}:
            return None, None
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None, None
        headings = [
            {"line": number, "level": len(match.group(1)), "title": match.group(2).strip()}
            for number, line in enumerate(text.splitlines(), start=1)
            if (match := re.match(r"^(#{1,6})\s+(.+?)\s*$", line))
        ]
        if not headings:
            return None, "utf-8"
        outline = self.root / "outlines" / task_id / f"{digest}.json"
        atomic_write_json(outline, {"artifact_id": f"sha256:{digest}", "headings": headings})
        return str(outline.relative_to(self.root).as_posix()), "utf-8"

    def resolve(self, task: TaskCapsule, artifact_id_or_alias: str) -> ArtifactManifest:
        value = artifact_id_or_alias
        if not value.startswith("sha256:"):
            alias_path = self._alias_path(task.task_id, value)
            if not alias_path.is_file():
                raise FileNotFoundError(f"unknown artifact alias: {value}")
            value = ArtifactAlias.model_validate_json(alias_path.read_text(encoding="utf-8")).artifact_id
        digest = value.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid artifact id")
        path = self._manifest_path(task.task_id, digest)
        if not path.is_file():
            raise ArtifactAccessError("artifact is not visible in this task scope")
        return ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def describe(self, task: TaskCapsule, artifact_id_or_alias: str) -> dict[str, Any]:
        manifest = self.resolve(task, artifact_id_or_alias)
        outline = None
        if manifest.outline_ref:
            outline = json.loads((self.root / manifest.outline_ref).read_text(encoding="utf-8"))
        selectors = ["line_range", "search"]
        if manifest.encoding == "utf-8":
            selectors.extend(["whole", "markdown_heading"])
        if manifest.media_type in {"application/json", "text/json"} or manifest.source_ref.lower().endswith(".json"):
            selectors.append("json_pointer")
        if manifest.source_ref.lower().endswith((".yaml", ".yml")):
            selectors.append("yaml_path")
        if manifest.source_ref.lower().endswith((".csv", ".tsv")):
            selectors.append("table_slice")
        return {
            "manifest": manifest.model_dump(mode="json"),
            "outline": outline,
            "available_selectors": sorted(set(selectors)),
        }

    def open_content(self, task: TaskCapsule, artifact_id_or_alias: str) -> BinaryIO:
        manifest = self.resolve(task, artifact_id_or_alias)
        return self._object_path(manifest.sha256).open("rb")

    def update_alias(
        self,
        task: TaskCapsule,
        alias: str,
        artifact_id: str,
        expected_version: int | None,
    ) -> ArtifactAlias:
        self.resolve(task, artifact_id)
        path = self._alias_path(task.task_id, alias)
        previous = ArtifactAlias.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
        observed_version = previous.version if previous else -1
        required = -1 if expected_version is None else expected_version
        if observed_version != required:
            raise CapsuleConflict(f"alias expected version {required}, observed {observed_version}")
        updated = ArtifactAlias(
            task_id=task.task_id,
            alias=alias,
            artifact_id=artifact_id,
            previous_artifact_id=previous.artifact_id if previous else None,
            version=observed_version + 1,
        )
        atomic_write_json(path, updated.model_dump(mode="json"))
        return updated

    def read(
        self,
        task: TaskCapsule,
        artifact_id_or_alias: str,
        selector: Selector,
        *,
        max_output_bytes: int | None = None,
        replay_content: bool = False,
    ) -> dict[str, Any]:
        manifest = self.resolve(task, artifact_id_or_alias)
        if selector.type == "whole" and manifest.byte_size > 65536:
            value = selector.value if isinstance(selector.value, dict) else {}
            if value.get("override") is not True or not str(value.get("reason") or "").strip():
                raise ValueError("whole selector for artifacts over 65536 bytes requires override=true and a reason")
        selector_hash = hashlib.sha256(_canonical_bytes(selector.model_dump(mode="json"))).hexdigest()
        receipt_id = hashlib.sha256(
            f"{task.task_id}\0{manifest.sha256}\0{selector_hash}\0reader-v1".encode("utf-8")
        ).hexdigest()
        receipt_path = self.root / "receipts" / task.task_id / f"{receipt_id}.json"
        if receipt_path.is_file() and not replay_content:
            receipt = ReadReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
            return {
                "unchanged": True,
                "read_receipt_id": receipt.read_receipt_id,
                "content_replayed": False,
                "returned_bytes": 0,
                "truncated": receipt.truncated,
            }
        with self.open_content(task, manifest.artifact_id) as handle:
            raw = handle.read()
        selected = self._select(raw, selector)
        limit = min(max_output_bytes or task.model_budget.max_output_bytes, task.model_budget.max_output_bytes)
        encoded = selected.encode("utf-8")
        truncated = len(encoded) > limit
        if truncated:
            encoded = encoded[:limit]
            while encoded:
                try:
                    selected = encoded.decode("utf-8", errors="strict")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
        receipt = ReadReceipt(
            read_receipt_id=receipt_id,
            task_id=task.task_id,
            artifact_id=manifest.artifact_id,
            artifact_sha256=manifest.sha256,
            selector=selector,
            selector_sha256=selector_hash,
            returned_sha256=hashlib.sha256(encoded).hexdigest(),
            returned_bytes=len(encoded),
            truncated=truncated,
            created_at=datetime.now(timezone.utc),
        )
        atomic_write_json(receipt_path, receipt.model_dump(mode="json"))
        return {
            "unchanged": False,
            "read_receipt_id": receipt_id,
            "content_replayed": replay_content,
            "returned_bytes": len(encoded),
            "truncated": truncated,
            "content": selected,
            "receipt": receipt.model_dump(mode="json"),
        }

    @staticmethod
    def _select(raw: bytes, selector: Selector) -> str:
        text = _strict_utf8(raw)
        if selector.type == "whole":
            return text
        if selector.type in {"line_range", "terminal_stream"}:
            value = selector.value if isinstance(selector.value, dict) else {}
            start = int(value.get("start", 1))
            end = int(value.get("end", start))
            if start < 1 or end < start:
                raise ValueError("invalid line range")
            return "\n".join(text.splitlines()[start - 1 : end])
        if selector.type == "markdown_heading":
            heading = str(selector.value or "").strip().casefold()
            lines = text.splitlines()
            for index, line in enumerate(lines):
                match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
                if not match or match.group(2).strip().casefold() != heading:
                    continue
                level = len(match.group(1))
                stop = len(lines)
                for cursor in range(index + 1, len(lines)):
                    following = re.match(r"^(#{1,6})\s+", lines[cursor])
                    if following and len(following.group(1)) <= level:
                        stop = cursor
                        break
                return "\n".join(lines[index:stop])
            raise ValueError(f"markdown heading not found: {selector.value}")
        if selector.type == "json_pointer":
            current: Any = json.loads(text)
            pointer = str(selector.value or "")
            if pointer:
                if not pointer.startswith("/"):
                    raise ValueError("JSON pointer must be empty or start with /")
                for token in pointer[1:].split("/"):
                    key = token.replace("~1", "/").replace("~0", "~")
                    current = current[int(key)] if isinstance(current, list) else current[key]
            return json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True)
        if selector.type == "yaml_path":
            current = yaml.safe_load(text)
            path = selector.value if isinstance(selector.value, list) else str(selector.value or "").split(".")
            for token in (item for item in path if str(item) != ""):
                current = current[int(token)] if isinstance(current, list) else current[str(token)]
            return yaml.safe_dump(current, allow_unicode=True, sort_keys=True).rstrip()
        if selector.type == "table_slice":
            value = selector.value if isinstance(selector.value, dict) else {}
            delimiter = "\t" if value.get("delimiter") == "tab" else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            columns = value.get("columns") or reader.fieldnames or []
            if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
                raise ValueError("table_slice columns must be a string list")
            unknown = sorted(set(columns) - set(reader.fieldnames or []))
            if unknown:
                raise ValueError(f"table_slice unknown columns: {unknown}")
            start = int(value.get("start", 1))
            end = int(value.get("end", start + 99))
            if start < 1 or end < start or end - start + 1 > 1000:
                raise ValueError("table_slice row range must contain 1..1000 rows")
            exact_filter = value.get("filter") or {}
            if not isinstance(exact_filter, dict) or any(key not in (reader.fieldnames or []) for key in exact_filter):
                raise ValueError("table_slice filter must use declared columns")
            rows = []
            for number, row in enumerate(reader, start=1):
                if number < start or number > end:
                    continue
                if all(str(row.get(key) or "") == str(expected) for key, expected in exact_filter.items()):
                    rows.append({column: row.get(column) for column in columns})
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns, delimiter=delimiter, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            return output.getvalue().rstrip()
        if selector.type == "search":
            value = selector.value if isinstance(selector.value, dict) else {"query": selector.value}
            query = str(value.get("query") or "")
            if not query:
                raise ValueError("search selector requires query")
            max_matches = min(int(value.get("max_matches", 20)), 100)
            mode = str(value.get("mode") or "exact")
            if mode == "exact":
                predicate = lambda line: query.casefold() in line.casefold()
            elif mode == "regex":
                if len(query) > 512:
                    raise ValueError("regex search query exceeds 512 characters")
                pattern = re.compile(query)
                predicate = lambda line: pattern.search(line) is not None
            else:
                raise ValueError("search mode must be exact or regex")
            matches = [f"{number}:{line}" for number, line in enumerate(text.splitlines(), start=1) if predicate(line)]
            return "\n".join(matches[:max_matches])
        raise ValueError(f"unsupported selector: {selector.type}")

    def gc_proposal(self, *, retention_before: datetime) -> dict[str, Any]:
        referenced = {
            manifest.sha256
            for path in (self.root / "manifests").glob("*/*.json")
            for manifest in [ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))]
        }
        candidates = []
        for path in (self.root / "objects" / "sha256").glob("*/*"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if path.name not in referenced and modified < retention_before:
                candidates.append({"sha256": path.name, "bytes": path.stat().st_size})
        return {"delete_performed": False, "candidates": candidates}

    def quota_status(self, task_id: str) -> dict[str, int]:
        return {
            "task_bytes": self._task_bytes(task_id),
            "task_max_bytes": self.task_max_bytes,
            "global_bytes": self._global_bytes(),
            "global_soft_max_bytes": self.global_soft_max_bytes,
            "object_max_bytes": self.object_max_bytes,
        }

    def missing_hashes(self, sha256_values: list[str]) -> list[str]:
        """Return only hashes absent locally so a remote peer transfers each object at most once."""

        missing = []
        for digest in sha256_values:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid SHA-256 in negotiation request")
            if not self._object_path(digest).is_file():
                missing.append(digest)
        return missing

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import subprocess
import shlex
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .orchestrator.adapters import transport_command
from .model_execution_evidence import extract_model_execution_evidence, normalize_model_execution_evidence


SCHEMA_VERSION = 1
DEFAULT_CONTEXT_DIR = Path(".solution") / "context"
DEFAULT_DOCS_NAME = "context_documents.jsonl"
DEFAULT_INDEX_NAME = "context_index.sqlite"
DEFAULT_MAX_DOCS = 5000
DEFAULT_MAX_DOC_CHARS = 4000
DEFAULT_QUERY_LIMIT = 8

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]{2,}", re.UNICODE)


@dataclass(frozen=True)
class ContextDocument:
    doc_id: str
    source_type: str
    title: str
    text: str
    tags: list[str]
    paths: list[str]
    trust_level: str = "system_generated"
    allowed_for_api: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "doc_id": self.doc_id,
            "source_type": self.source_type,
            "title": self.title,
            "text": self.text,
            "tags": self.tags,
            "paths": self.paths,
            "trust_level": self.trust_level,
            "allowed_for_api": self.allowed_for_api,
        }


def context_dir(repo_root: Path) -> Path:
    return repo_root / DEFAULT_CONTEXT_DIR


def documents_path(repo_root: Path) -> Path:
    return context_dir(repo_root) / DEFAULT_DOCS_NAME


def index_path(repo_root: Path) -> Path:
    return context_dir(repo_root) / DEFAULT_INDEX_NAME


def _safe_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_json_lines(path: Path, limit: int, *, prefer_tail: bool = False) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    count = 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if prefer_tail:
        lines = list(reversed(lines))
    for line in lines:
        if count >= limit:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            count += 1
            yield item


def _clip(text: Any, max_chars: int) -> str:
    if text is None:
        return ""
    if isinstance(text, str):
        value = text
    else:
        value = yaml.safe_dump(text, sort_keys=False, allow_unicode=True)
    value = value.strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n[truncated by context index]\n"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        paths.extend(_strings(value.get("path") or value.get("uri")))
        for nested_key in ("items", "artifacts", "checkpoints", "outputs"):
            paths.extend(_artifact_paths(value.get(nested_key)))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_artifact_paths(item))
    elif value:
        paths.extend(_strings(value))
    return paths


def _artifact_tags(value: Any) -> list[str]:
    tags: list[str] = []
    if isinstance(value, dict):
        for key in ("id", "name", "type", "role", "metric", "stage"):
            tags.extend(_strings(value.get(key)))
        for nested_key in ("items", "artifacts", "checkpoints", "outputs"):
            tags.extend(_artifact_tags(value.get(nested_key)))
    elif isinstance(value, list):
        for item in value:
            tags.extend(_artifact_tags(item))
    elif value:
        tags.extend(_strings(value))
    return tags


def _project_map_path(repo_root: Path) -> Path | None:
    for rel in (".solution/context/project_map.yaml", "config/project_map.yaml"):
        path = repo_root / rel
        if path.exists():
            return path
    return None


def _script_registry_path(repo_root: Path) -> Path | None:
    for rel in (
        "solution_agent/execution_port/script_registry.yaml",
        ".solution/context/script_registry.yaml",
        "config/script_registry.yaml",
    ):
        path = repo_root / rel
        if path.exists():
            return path
    return None


def _data_inventory_path(repo_root: Path) -> Path | None:
    for rel in (".solution/context/data_inventory.yaml", "config/data_inventory.yaml"):
        path = repo_root / rel
        if path.exists():
            return path
    return None


def _capability_matrix_path(repo_root: Path) -> Path | None:
    for rel in (".solution/context/capability_matrix.yaml", "config/capability_matrix.yaml"):
        path = repo_root / rel
        if path.exists():
            return path
    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _doc(
    *,
    doc_id: str,
    source_type: str,
    title: str,
    text: Any,
    tags: Iterable[str] = (),
    paths: Iterable[str] = (),
    max_doc_chars: int,
    trust_level: str = "system_generated",
) -> ContextDocument:
    return ContextDocument(
        doc_id=doc_id,
        source_type=source_type,
        title=title,
        text=_clip(text, max_doc_chars),
        tags=sorted({str(tag) for tag in tags if str(tag)}),
        paths=sorted({str(path) for path in paths if str(path)}),
        trust_level=trust_level,
    )


def build_context_documents(
    repo_root: Path,
    *,
    max_docs: int = DEFAULT_MAX_DOCS,
    max_doc_chars: int = DEFAULT_MAX_DOC_CHARS,
) -> list[dict[str, Any]]:
    """Build bounded retrieval documents from durable summaries only.

    This intentionally avoids raw logs, checkpoints, caches, and arbitrary repo
    scans. It is an orientation layer for context_pack generation, not a source
    code indexer.
    """
    repo_root = repo_root.resolve()
    docs: list[ContextDocument] = []

    project_path = _project_map_path(repo_root)
    if project_path:
        project = _safe_yaml(project_path)
        trust = "human_reviewed" if project.get("confidence", {}).get("overall") == "reviewed" else "draft_or_system_generated"
        docs.append(
            _doc(
                doc_id="project_map:overview",
                source_type="project_map",
                title=str(project.get("project_name") or project.get("project_id") or "Project map overview"),
                text={key: project.get(key) for key in ("project_id", "project_name", "confidence", "source")},
                tags=["project_map", "overview"],
                paths=[_rel(project_path, repo_root)],
                max_doc_chars=max_doc_chars,
                trust_level=trust,
            )
        )
        for section in ("datasets", "checkpoints", "scripts", "outputs", "metrics", "workflows", "routing_hints", "open_questions"):
            for idx, item in enumerate(_as_list(project.get(section))):
                if not isinstance(item, dict):
                    item = {"value": item}
                item_id = item.get("id") or item.get("script_id") or item.get("name") or item.get("prefer_script_id") or idx
                paths = _strings(item.get("path") or item.get("paths"))
                tags = ["project_map", section, str(item_id)]
                if item.get("script_id"):
                    tags.append(str(item.get("script_id")))
                docs.append(
                    _doc(
                        doc_id=f"project_map:{section}:{item_id}",
                        source_type=f"project_map.{section}",
                        title=f"{section}: {item_id}",
                        text=item,
                        tags=tags,
                        paths=paths + [_rel(project_path, repo_root)],
                        max_doc_chars=max_doc_chars,
                        trust_level=trust,
                    )
                )
            if len(docs) >= max_docs:
                return [doc.to_json() for doc in docs]

    data_inventory_path = _data_inventory_path(repo_root)
    if data_inventory_path:
        inventory = _safe_yaml(data_inventory_path)
        trust = "human_reviewed" if inventory.get("confidence", {}).get("overall") == "reviewed" else "draft_or_system_generated"
        docs.append(
            _doc(
                doc_id="data_inventory:overview",
                source_type="data_inventory",
                title=str(inventory.get("project_id") or "Data inventory overview"),
                text={key: inventory.get(key) for key in ("schema_version", "project_id", "confidence", "source")},
                tags=["data_inventory", "overview"],
                paths=[_rel(data_inventory_path, repo_root)],
                max_doc_chars=max_doc_chars,
                trust_level=trust,
            )
        )
        for section in (
            "datasets",
            "feature_caches",
            "mask_sources",
            "splits",
            "checkpoints",
            "artifact_roots",
            "remote_roots",
            "open_questions",
        ):
            for idx, item in enumerate(_as_list(inventory.get(section))):
                if not isinstance(item, dict):
                    item = {"value": item}
                item_id = item.get("id") or item.get("name") or item.get("path") or idx
                paths = _strings(item.get("path") or item.get("paths") or item.get("root"))
                tags = ["data_inventory", section, str(item_id)]
                tags.extend(_strings(item.get("kind") or item.get("type") or item.get("format") or item.get("status")))
                tags.extend(_strings(item.get("reusable_for") or item.get("required_for") or item.get("produced_by")))
                docs.append(
                    _doc(
                        doc_id=f"data_inventory:{section}:{item_id}",
                        source_type=f"data_inventory.{section}",
                        title=f"{section}: {item_id}",
                        text=item,
                        tags=tags,
                        paths=paths + [_rel(data_inventory_path, repo_root)],
                        max_doc_chars=max_doc_chars,
                        trust_level=trust,
                    )
                )
                if len(docs) >= max_docs:
                    return [doc.to_json() for doc in docs]

    capability_matrix_path = _capability_matrix_path(repo_root)
    if capability_matrix_path:
        matrix = _safe_yaml(capability_matrix_path)
        trust = "human_reviewed" if matrix.get("confidence", {}).get("overall") == "reviewed" else "draft_or_system_generated"
        docs.append(
            _doc(
                doc_id="capability_matrix:overview",
                source_type="capability_matrix",
                title=str(matrix.get("project_id") or "Capability matrix overview"),
                text={key: matrix.get(key) for key in ("schema_version", "project_id", "confidence", "source")},
                tags=["capability_matrix", "overview"],
                paths=[_rel(capability_matrix_path, repo_root)],
                max_doc_chars=max_doc_chars,
                trust_level=trust,
            )
        )
        for section in ("capabilities", "experiment_primitives", "agent_roles", "open_questions"):
            for idx, item in enumerate(_as_list(matrix.get(section))):
                if not isinstance(item, dict):
                    item = {"value": item}
                item_id = item.get("id") or item.get("script_id") or item.get("name") or idx
                tags = ["capability_matrix", section, str(item_id)]
                tags.extend(_strings(item.get("can_produce") or item.get("outputs") or item.get("expected_outputs")))
                tags.extend(_strings(item.get("cannot_do") or item.get("limitations")))
                tags.extend(_strings(item.get("requires_codex") or item.get("requires_approval")))
                paths = _strings(item.get("path") or item.get("paths") or item.get("script_path"))
                docs.append(
                    _doc(
                        doc_id=f"capability_matrix:{section}:{item_id}",
                        source_type=f"capability_matrix.{section}",
                        title=f"{section}: {item_id}",
                        text=item,
                        tags=tags,
                        paths=paths + [_rel(capability_matrix_path, repo_root)],
                        max_doc_chars=max_doc_chars,
                        trust_level=trust,
                    )
                )
                if len(docs) >= max_docs:
                    return [doc.to_json() for doc in docs]

    registry_path = _script_registry_path(repo_root)
    if registry_path:
        registry = _safe_yaml(registry_path)
        scripts = registry.get("scripts") if isinstance(registry.get("scripts"), dict) else {}
        for script_id, spec in scripts.items():
            if not isinstance(spec, dict):
                spec = {"value": spec}
            entrypoint = spec.get("entrypoint")
            paths = [item for item in _strings(entrypoint) if "/" in item or "\\" in item or item.endswith(".py")]
            docs.append(
                _doc(
                    doc_id=f"script_registry:{script_id}",
                    source_type="script_registry",
                    title=f"script_id: {script_id}",
                    text={"script_id": script_id, **spec},
                    tags=["script_registry", str(script_id)],
                    paths=paths + [_rel(registry_path, repo_root)],
                    max_doc_chars=max_doc_chars,
                    trust_level="canonical_execution_allowlist",
                )
            )
            if len(docs) >= max_docs:
                return [doc.to_json() for doc in docs]

    artifact_path = context_dir(repo_root) / "artifact_index.jsonl"
    for item in _safe_json_lines(artifact_path, max(0, max_docs - len(docs)), prefer_tail=True):
        task_id = item.get("task_id") or item.get("run_id") or len(docs)
        text = {
            "task_id": item.get("task_id"),
            "status": item.get("status"),
            "worker": item.get("worker"),
            "experiment_kind": item.get("experiment_kind") or item.get("kind") or item.get("phase"),
            "summary": item.get("summary") or item.get("log_summary"),
            "conclusion": item.get("conclusion") or item.get("result_conclusion"),
            "claim_scope": item.get("claim_scope"),
            "decision": item.get("decision"),
            "next_actions": item.get("next_actions") or item.get("recommended_next_actions"),
            "metrics": item.get("metrics"),
            "script_ids": item.get("script_ids") or item.get("script_id"),
            "parent_task_ids": item.get("parent_task_ids") or item.get("parents") or item.get("depends_on"),
            "input_task_ids": item.get("input_task_ids") or item.get("consumes_from"),
            "derived_from": item.get("derived_from"),
            "lineage": item.get("lineage"),
            "best_checkpoint": item.get("best_checkpoint") or item.get("best_ckpt"),
            "checkpoints": item.get("checkpoints"),
            "selected_artifacts": item.get("selected_artifacts"),
            "produced_artifacts": item.get("produced_artifacts") or item.get("artifacts_produced"),
            "consumed_artifacts": item.get("consumed_artifacts") or item.get("inputs_used"),
            "artifact_roles": item.get("artifact_roles"),
            "config_paths": item.get("config_paths") or item.get("configs"),
            "code_paths": item.get("code_paths") or item.get("changed_files"),
            "important_paths": item.get("important_paths") or item.get("paths"),
            "result_path": item.get("result_path"),
            "model_execution_evidence": item.get("model_execution_evidence"),
        }
        tags = ["artifact_index", str(task_id), str(item.get("status") or "")]
        tags.extend(_strings(item.get("claim_scope")))
        model_evidence = item.get("model_execution_evidence") if isinstance(item.get("model_execution_evidence"), dict) else {}
        tags.extend(_strings(model_evidence.get("requested_model")))
        tags.extend(_strings(model_evidence.get("executed_model")))
        tags.extend(_strings(model_evidence.get("evidence_scope")))
        tags.extend(_strings(model_evidence.get("proxy_only")))
        tags.extend(_strings(model_evidence.get("formal_evidence")))
        tags.extend(_strings(item.get("tags")))
        tags.extend(_strings(item.get("experiment_kind") or item.get("kind") or item.get("phase")))
        tags.extend(_strings(item.get("script_ids") or item.get("script_id")))
        tags.extend(_strings(item.get("parent_task_ids") or item.get("parents") or item.get("depends_on")))
        tags.extend(_strings(item.get("input_task_ids") or item.get("consumes_from")))
        best_ckpt = item.get("best_checkpoint") or item.get("best_ckpt")
        if isinstance(best_ckpt, dict):
            tags.extend(_strings(best_ckpt.get("id") or best_ckpt.get("path")))
        elif best_ckpt:
            tags.extend(_strings(best_ckpt))
        for artifact_key in ("selected_artifacts", "produced_artifacts", "artifacts_produced", "consumed_artifacts", "inputs_used", "artifact_roles", "checkpoints"):
            tags.extend(_artifact_tags(item.get(artifact_key)))
        paths = []
        paths.extend(_strings(item.get("result_path")))
        paths.extend(_strings(item.get("important_paths") or item.get("paths")))
        paths.extend(_strings(item.get("config_paths") or item.get("configs")))
        paths.extend(_strings(item.get("code_paths") or item.get("changed_files")))
        if isinstance(best_ckpt, dict):
            paths.extend(_strings(best_ckpt.get("path")))
        elif best_ckpt:
            paths.extend(_strings(best_ckpt))
        for artifact_key in ("selected_artifacts", "produced_artifacts", "artifacts_produced", "consumed_artifacts", "inputs_used", "artifact_roles", "checkpoints"):
            paths.extend(_artifact_paths(item.get(artifact_key)))
        docs.append(
            _doc(
                doc_id=f"artifact_index:{task_id}",
                source_type="artifact_index",
                title=f"artifact summary: {task_id}",
                text=text,
                tags=tags,
                paths=paths + [_rel(artifact_path, repo_root)],
                max_doc_chars=max_doc_chars,
                trust_level="collected_summary",
            )
        )
        if len(docs) >= max_docs:
            break

    state_root = repo_root / "state" / "workplaces"
    if state_root.exists():
        for path in sorted(state_root.glob("*/agent_context.yaml")):
            data = _safe_yaml(path)
            if not data:
                continue
            text = {
                "profile_id": data.get("profile_id"),
                "host": data.get("host"),
                "remote_workdir": data.get("remote_workdir"),
                "remote_root": data.get("remote_root"),
                "configured_conda_env": data.get("configured_conda_env"),
                "configured_conda_env_ready": data.get("configured_conda_env_ready"),
                "remote_materializer": data.get("remote_materializer"),
                "readiness_checks": data.get("readiness_checks"),
            }
            docs.append(
                _doc(
                    doc_id=f"agent_context:{data.get('profile_id') or path.parent.name}",
                    source_type="agent_context",
                    title=f"workplace: {data.get('profile_id') or path.parent.name}",
                    text=text,
                    tags=["agent_context", str(data.get("host") or ""), str(data.get("configured_conda_env") or "")],
                    paths=[_rel(path, repo_root)],
                    max_doc_chars=max_doc_chars,
                    trust_level="workplace_init_summary",
                )
            )
            if len(docs) >= max_docs:
                break

    dialogue_root = repo_root / "continuation" / "project_dialogue" / "sessions"
    if dialogue_root.exists() and len(docs) < max_docs:
        for session_path in sorted(dialogue_root.glob("*/project_understanding_answer.json")):
            data = _safe_json(session_path)
            if not data:
                continue
            session_id = session_path.parent.name
            binding = data.get("project_knowledge_binding") if isinstance(data.get("project_knowledge_binding"), dict) else {}
            text = {
                "session_id": session_id,
                "project_id": data.get("project_id"),
                "question": data.get("question"),
                "state_summary": data.get("state_summary"),
                "active_workstreams": data.get("active_workstreams"),
                "current_mainline": data.get("current_mainline"),
                "completed_or_superseded": data.get("completed_or_superseded"),
                "bottlenecks": data.get("bottlenecks"),
                "claim_limits": data.get("claim_limits"),
                "next_safe_action": data.get("next_safe_action"),
                "evidence_refs": data.get("evidence_refs"),
                "project_knowledge_binding": binding,
            }
            tags = [
                "project_dialogue",
                str(data.get("project_id") or ""),
                str(data.get("current_mainline") or ""),
                str(binding.get("workplace_profile") or ""),
            ]
            tags.extend(_strings(data.get("question")))
            paths = [_rel(session_path, repo_root)]
            transcript = session_path.parent / "transcript.md"
            if transcript.exists():
                paths.append(_rel(transcript, repo_root))
            docs.append(
                _doc(
                    doc_id=f"project_dialogue:{session_id}",
                    source_type="project_dialogue",
                    title=f"project dialogue summary: {data.get('project_id') or session_id}",
                    text=text,
                    tags=tags,
                    paths=paths,
                    max_doc_chars=max_doc_chars,
                    trust_level="project_dialogue_summary",
                )
            )
            if len(docs) >= max_docs:
                break

    unique_docs: list[ContextDocument] = []
    seen_doc_ids: set[str] = set()
    for doc in docs:
        if doc.doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc.doc_id)
        unique_docs.append(doc)
        if len(unique_docs) >= max_docs:
            break
    return [doc.to_json() for doc in unique_docs]


def write_context_documents(
    repo_root: Path,
    *,
    output_path: Path | None = None,
    max_docs: int = DEFAULT_MAX_DOCS,
    max_doc_chars: int = DEFAULT_MAX_DOC_CHARS,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output_path = output_path or documents_path(repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    docs = build_context_documents(repo_root, max_docs=max_docs, max_doc_chars=max_doc_chars)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for doc in docs:
            handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "path": str(output_path), "doc_count": len(docs), "schema_version": SCHEMA_VERSION}


def _remote_profile(repo_root: Path, host: str, remote_workdir: str | None = None) -> dict[str, Any]:
    path = repo_root / "config" / "remote_execution_profiles.yaml"
    data = _safe_yaml(path)
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("host") or name) != host and str(name) != host:
            continue
        if remote_workdir and str(profile.get("remote_workdir") or "") != remote_workdir:
            continue
        result = dict(profile)
        result.setdefault("profile_name", str(name))
        return result
    return {"profile_name": host, "host": host, "remote_workdir": remote_workdir}


def _artifact_index_path(repo_root: Path) -> Path:
    return context_dir(repo_root) / "artifact_index.jsonl"


def _upsert_artifact_index_entry(repo_root: Path, entry: dict[str, Any]) -> Path:
    path = _artifact_index_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    task_id = str(entry.get("task_id") or "")
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(item.get("task_id") or "") == task_id:
                continue
            rows.append(item)
    rows.append(entry)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in rows:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_file_entries(result_path: Path, limit: int = 80) -> list[dict[str, Any]]:
    artifacts = result_path / "artifacts"
    if not artifacts.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(item for item in artifacts.rglob("*") if item.is_file()):
        try:
            rel = path.relative_to(result_path).as_posix()
        except ValueError:
            rel = path.as_posix()
        rows.append(
            {
                "path": rel,
                "name": path.name,
                "suffix": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def index_collected_result(repo_root: Path, *, task_id: str, result_path: Path) -> dict[str, Any]:
    """Record a compact MLflow/OpenLineage-style memory entry for a collected run."""
    repo_root = repo_root.resolve()
    result_path = result_path.resolve()
    result_yaml = result_path / "result.yaml"
    payload = _safe_yaml(result_yaml)
    decision = _safe_json(result_path / "artifacts" / "decision_summary.json")
    action_requests = _safe_json(result_path / "artifacts" / "action_requests.json")
    metrics = _safe_json(result_path / "metrics.json")
    artifact_files = _artifact_file_entries(result_path)
    if payload.get("run_id"):
        run_id = str(payload.get("run_id"))
    elif result_path.name != "latest":
        run_id = result_path.parent.name
    else:
        run_id = task_id
    remote = payload.get("remote") if isinstance(payload.get("remote"), dict) else {}
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    judge = payload.get("judge") if isinstance(payload.get("judge"), dict) else {}
    model_execution_evidence = (
        extract_model_execution_evidence(payload)
        or extract_model_execution_evidence(decision)
        or extract_model_execution_evidence(metrics)
    )
    if model_execution_evidence is not None:
        model_execution_evidence = normalize_model_execution_evidence(model_execution_evidence)
    claim_scope = None
    if isinstance(decision, dict):
        claim_scope = decision.get("claim_allowed") or decision.get("claim_scope")
    entry = {
        "schema_version": SCHEMA_VERSION,
        "kind": "collected_run",
        "task_id": task_id,
        "run_id": run_id,
        "status": payload.get("status") or payload.get("execution_status") or "unknown",
        "worker": payload.get("worker"),
        "script_id": payload.get("script_id") or execution.get("script_id"),
        "summary": payload.get("summary_short"),
        "result_path": _rel(result_path, repo_root),
        "result_yaml": _rel(result_yaml, repo_root) if result_yaml.exists() else None,
        "remote": {
            "host": remote.get("host"),
            "remote_workdir": remote.get("remote_workdir"),
            "remote_root": remote.get("remote_root"),
            "remote_task_dir": remote.get("remote_task_dir"),
        },
        "job": {
            "name": payload.get("script_id") or execution.get("script_id") or payload.get("worker"),
            "namespace": "solution-agent/materialized_execution",
        },
        "run": {
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "exit_code": payload.get("exit_code") or execution.get("exit_code"),
            "execution_success": judge.get("execution_success") if judge else payload.get("execution_success"),
            "failure_type": payload.get("failure_type") or judge.get("failure_type"),
            "warning_type": payload.get("warning_type") or judge.get("warning_type"),
        },
        "artifacts": artifact_files,
        "produced_artifacts": [item["path"] for item in artifact_files],
        "metrics": metrics if isinstance(metrics, dict) else None,
        "decision": decision if isinstance(decision, dict) else None,
        "model_execution_evidence": model_execution_evidence,
        "claim_scope": claim_scope,
        "caveats": payload.get("caveats") or payload.get("risks") or [],
        "action_requests": action_requests if isinstance(action_requests, dict) else None,
        "tags": [
            str(item)
            for item in (
                task_id,
                payload.get("worker"),
                payload.get("status"),
                payload.get("script_id") or execution.get("script_id"),
                claim_scope,
                model_execution_evidence.get("requested_model") if model_execution_evidence else None,
                model_execution_evidence.get("executed_model") if model_execution_evidence else None,
                model_execution_evidence.get("evidence_scope") if model_execution_evidence else None,
            )
            if item
        ],
        "reusable_for": [
            "grounded_context",
            "artifact_lineage",
            "claim_boundary_reference",
        ],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    output_path = _upsert_artifact_index_entry(repo_root, entry)
    return {"ok": True, "path": str(output_path), "task_id": task_id, "run_id": run_id}


def snapshot_remote_workplace(
    repo_root: Path,
    *,
    host: str,
    remote_root: str | None = None,
    remote_workdir: str | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    """Capture a bounded remote workplace asset snapshot for context retrieval.

    This is intentionally not a daemon and not a full scan. It only searches
    configured project asset roots plus the selected remote_root for checkpoint
    files and recent worker result contracts.
    """
    repo_root = repo_root.resolve()
    profile = _remote_profile(repo_root, host, remote_workdir)
    assets = profile.get("project_assets") if isinstance(profile.get("project_assets"), dict) else {}
    roots: list[str] = []
    roots.extend(_strings(assets.get("checkpoint_roots")))
    if remote_root:
        roots.append(remote_root)
    elif profile.get("remote_output_root"):
        roots.append(str(profile.get("remote_output_root")))
    roots = list(dict.fromkeys(item for item in roots if item))
    if not roots:
        return {"ok": False, "reason": "no_snapshot_roots", "host": host}
    quoted_roots = " ".join(shlex.quote(item) for item in roots)
    script = (
        "set -u; "
        "echo __CHECKPOINTS__; "
        f"for root in {quoted_roots}; do "
        "[ -d \"$root\" ] || continue; "
        "find \"$root\" -maxdepth 6 -type f \\( -name '*.ckpt' -o -name '*.pt' -o -name '*.pth' -o -name '*.safetensors' \\) "
        "-printf '%T@\\t%s\\t%p\\n' 2>/dev/null; "
        "done | sort -nr | head -n "
        f"{int(limit)}; "
        "echo __RESULTS__; "
        f"for root in {quoted_roots}; do "
        "[ -d \"$root\" ] || continue; "
        "find \"$root\" -maxdepth 4 -path '*/_worker_out/result.yaml' -type f -printf '%T@\\t%s\\t%p\\n' 2>/dev/null; "
        "done | sort -nr | head -n "
        f"{int(limit)}"
    )
    proc = subprocess.run(
        [*transport_command("ssh"), host, script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    checkpoints: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    section = ""
    for line in proc.stdout.splitlines():
        if line == "__CHECKPOINTS__":
            section = "checkpoints"
            continue
        if line == "__RESULTS__":
            section = "results"
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        mtime, size, path = parts
        item = {"path": path, "size_bytes": int(float(size)), "mtime": float(mtime)}
        if section == "checkpoints":
            checkpoints.append(item)
        elif section == "results":
            results.append(item)
    known_checkpoints = assets.get("known_checkpoints") if isinstance(assets.get("known_checkpoints"), dict) else {}
    checkpoint_roles: list[dict[str, Any]] = []
    known_paths = {str(path): str(role) for role, path in known_checkpoints.items()}
    for item in checkpoints:
        path = str(item.get("path") or "")
        if path in known_paths:
            role = known_paths[path]
            item["role"] = role
            item["selection_hint"] = "profile_known_checkpoint"
            checkpoint_roles.append({"role": role, "path": path, "source": "remote_execution_profiles.project_assets.known_checkpoints"})
    for role, path in known_checkpoints.items():
        path_text = str(path)
        if not any(str(item.get("path") or "") == path_text for item in checkpoints):
            checkpoint_roles.append({"role": str(role), "path": path_text, "source": "remote_execution_profiles.project_assets.known_checkpoints", "observed": False})
    ok = proc.returncode == 0
    profile_name = str(profile.get("profile_name") or host)
    entry = {
        "schema_version": SCHEMA_VERSION,
        "task_id": f"workplace_snapshot:{profile_name}",
        "kind": "workplace_snapshot",
        "status": "completed" if ok else "failed",
        "host": host,
        "remote_workdir": remote_workdir or profile.get("remote_workdir"),
        "remote_root": remote_root or profile.get("remote_output_root"),
        "summary": f"Remote workplace snapshot for {profile_name}: {len(checkpoints)} checkpoints, {len(results)} result contracts.",
        "known_checkpoint_roles": checkpoint_roles,
        "checkpoint_selection_policy": [
            "Prefer an explicit task checkpoint path when provided.",
            "Otherwise prefer a matching project_assets.known_checkpoints role.",
            "Otherwise use parent artifact summaries or metrics-bearing checkpoint names.",
            "Do not choose a checkpoint by recency alone when multiple candidates are plausible.",
            "If the intended role is ambiguous, request missing context instead of guessing.",
        ],
        "checkpoints": checkpoints,
        "important_paths": [item["path"] for item in checkpoints[:20]] + [item["path"] for item in results[:20]],
        "produced_artifacts": [item["path"] for item in results[:20]],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    output_path = _upsert_artifact_index_entry(repo_root, entry)
    return {
        "ok": ok,
        "host": host,
        "profile": profile_name,
        "roots": roots,
        "checkpoint_count": len(checkpoints),
        "result_count": len(results),
        "artifact_index": str(output_path),
        "stderr": proc.stderr.strip()[:1000],
    }


def _read_documents(path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if not path.exists():
        return docs
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and doc.get("allowed_for_api", True):
            docs.append(doc)
    return docs


def build_context_index(
    repo_root: Path,
    *,
    docs_path: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    docs_path = docs_path or documents_path(repo_root)
    db_path = db_path or index_path(repo_root)
    docs = _read_documents(docs_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    build_path = db_path.with_name(f"{db_path.name}.tmp-{os.getpid()}")
    if build_path.exists():
        build_path.unlink()
    fts_enabled = True
    with closing(sqlite3.connect(str(build_path))) as conn:
        conn.execute(
            "CREATE TABLE documents (doc_id TEXT PRIMARY KEY, source_type TEXT, title TEXT, text TEXT, tags TEXT, paths TEXT, trust_level TEXT)"
        )
        try:
            conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(doc_id UNINDEXED, title, text, tags)")
        except sqlite3.OperationalError:
            fts_enabled = False
        for doc in docs:
            row = (
                str(doc.get("doc_id") or ""),
                str(doc.get("source_type") or ""),
                str(doc.get("title") or ""),
                str(doc.get("text") or ""),
                " ".join(_strings(doc.get("tags"))),
                json.dumps(_strings(doc.get("paths")), ensure_ascii=False),
                str(doc.get("trust_level") or ""),
            )
            conn.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)", row)
            if fts_enabled:
                conn.execute("INSERT INTO documents_fts (doc_id, title, text, tags) VALUES (?, ?, ?, ?)", (row[0], row[2], row[3], row[4]))
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO metadata VALUES (?, ?)", ("schema_version", str(SCHEMA_VERSION)))
        conn.execute("INSERT INTO metadata VALUES (?, ?)", ("fts_enabled", "true" if fts_enabled else "false"))
        conn.commit()
    for attempt in range(6):
        try:
            os.replace(build_path, db_path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))
    return {"ok": True, "path": str(db_path), "doc_count": len(docs), "fts_enabled": fts_enabled, "schema_version": SCHEMA_VERSION}


def _query_terms(query: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(query or "")][:24]


def _fts_query(query: str) -> str:
    terms = _query_terms(query)
    return " OR ".join(terms) if terms else ""


def _row_to_result(row: sqlite3.Row, score: float | None = None) -> dict[str, Any]:
    paths: list[str]
    try:
        paths = json.loads(row["paths"] or "[]")
    except Exception:
        paths = []
    result = {
        "doc_id": row["doc_id"],
        "source_type": row["source_type"],
        "title": row["title"],
        "text": row["text"],
        "tags": [item for item in str(row["tags"] or "").split() if item],
        "paths": paths,
        "trust_level": row["trust_level"],
    }
    if score is not None:
        result["score"] = score
    return result


def _rerank_results(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return results
    reranked: list[tuple[float, dict[str, Any]]] = []
    for index, item in enumerate(results):
        metadata = " ".join(
            [
                str(item.get("doc_id") or ""),
                str(item.get("source_type") or ""),
                str(item.get("title") or ""),
                " ".join(_strings(item.get("tags"))),
                " ".join(_strings(item.get("paths"))),
            ]
        ).lower()
        exact_fields = {
            str(item.get("doc_id") or "").lower(),
            str(item.get("title") or "").lower().replace("script_id: ", ""),
            *[str(tag).lower() for tag in _strings(item.get("tags"))],
        }
        doc_id = str(item.get("doc_id") or "").lower()
        text = str(item.get("text") or "").lower()
        identity_bonus = sum(20.0 for term in terms if term in exact_fields or doc_id.endswith(f":{term}"))
        exact_bonus = sum(4.0 for term in terms if term in metadata)
        body_bonus = sum(1.0 for term in terms if term in text)
        canonical_bonus = 25.0 if item.get("source_type") == "script_registry" and identity_bonus else 0.0
        base = float(item.get("score") or 0.0)
        # FTS bm25 returns smaller values for stronger matches. Convert it to
        # a positive-ish signal, then add exact metadata boosts.
        fts_signal = -base if base < 0 else base
        score = identity_bonus + canonical_bonus + exact_bonus + body_bonus + fts_signal - (index * 0.001)
        updated = dict(item)
        updated["rerank_score"] = score
        reranked.append((score, updated))
    reranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("doc_id") or "")))
    return [item for _, item in reranked]


def query_context_index(repo_root: Path, query: str, *, limit: int = DEFAULT_QUERY_LIMIT, db_path: Path | None = None) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    db_path = db_path or index_path(repo_root)
    if not db_path.exists():
        return {"ok": False, "reason": "index_missing", "path": str(db_path), "query": query, "results": []}
    terms = _query_terms(query)
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        fts_enabled = False
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key='fts_enabled'").fetchone()
            fts_enabled = bool(row and row["value"] == "true")
        except sqlite3.Error:
            fts_enabled = False
        if fts_enabled and terms:
            try:
                rows = conn.execute(
                    """
                    SELECT d.*, bm25(documents_fts) AS rank
                    FROM documents_fts
                    JOIN documents d ON d.doc_id = documents_fts.doc_id
                    WHERE documents_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (_fts_query(query), max(int(limit) * 4, int(limit))),
                ).fetchall()
                results = _rerank_results([_row_to_result(row, float(row["rank"])) for row in rows], query)[: int(limit)]
                return {
                    "ok": True,
                    "path": str(db_path),
                    "query": query,
                    "mode": "fts5",
                    "results": results,
                }
            except sqlite3.Error:
                pass
        all_rows = conn.execute("SELECT * FROM documents").fetchall()
    lowered_terms = terms or [query.lower()]
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in all_rows:
        haystack = f"{row['title']} {row['text']} {row['tags']}".lower()
        score = sum(1 for term in lowered_terms if term and term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1]["doc_id"])))
    results = _rerank_results([_row_to_result(row, float(score)) for score, row in scored], query)[: int(limit)]
    return {
        "ok": True,
        "path": str(db_path),
        "query": query,
        "mode": "like_fallback",
        "results": results,
    }


def context_index_status(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    docs_path = documents_path(repo_root)
    db_path = index_path(repo_root)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "context_dir": str(context_dir(repo_root)),
        "documents_path": str(docs_path),
        "index_path": str(db_path),
        "documents_exists": docs_path.exists(),
        "index_exists": db_path.exists(),
    }
    source_paths = []
    for candidate in (
        _project_map_path(repo_root),
        _script_registry_path(repo_root),
        _data_inventory_path(repo_root),
        _capability_matrix_path(repo_root),
        context_dir(repo_root) / "artifact_index.jsonl",
    ):
        if candidate and candidate.exists():
            source_paths.append(candidate)
    state_root = repo_root / "state" / "workplaces"
    if state_root.exists():
        source_paths.extend(sorted(state_root.glob("*/agent_context.yaml")))
    dialogue_root = repo_root / "continuation" / "project_dialogue" / "sessions"
    if dialogue_root.exists():
        source_paths.extend(sorted(dialogue_root.glob("*/project_understanding_answer.json")))
    newest_source_mtime = max((path.stat().st_mtime for path in source_paths if path.exists()), default=0.0)
    docs_mtime = docs_path.stat().st_mtime if docs_path.exists() else 0.0
    index_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
    payload["sources"] = {
        "count": len(source_paths),
        "newest_mtime": newest_source_mtime,
        "newest_path": str(max(source_paths, key=lambda path: path.stat().st_mtime)) if source_paths else None,
    }
    payload["stale"] = {
        "documents_stale": bool(source_paths and (not docs_path.exists() or docs_mtime < newest_source_mtime)),
        "index_stale": bool(docs_path.exists() and (not db_path.exists() or index_mtime < docs_mtime)),
        "dirty": bool(
            (source_paths and (not docs_path.exists() or docs_mtime < newest_source_mtime))
            or (docs_path.exists() and (not db_path.exists() or index_mtime < docs_mtime))
        ),
        "policy": "manual_rebuild_required",
    }
    if docs_path.exists():
        payload["document_lines"] = len([line for line in docs_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
    if db_path.exists():
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                payload["indexed_docs"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                row = conn.execute("SELECT value FROM metadata WHERE key='fts_enabled'").fetchone()
                payload["fts_enabled"] = bool(row and row[0] == "true")
        except sqlite3.Error as exc:
            payload["index_error"] = str(exc)
    return payload

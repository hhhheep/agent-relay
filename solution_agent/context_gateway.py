from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .context_index import query_context_index
from .harness.artifact_runtime import ArtifactStore, Selector
from .harness.release_identity import build_runtime_identity
from .harness.task_capsule import ArtifactRef, ModelBudget, ScopeContract, TaskCapsule, TaskCapsuleStore
from .orchestrator.schema import now_iso


SCHEMA_VERSION = 1
DEFAULT_MAX_TOKENS = 24000
DEFAULT_IGNORE_PATTERNS = [
    ".git/**",
    ".pytest_cache/**",
    ".tmp/**",
    ".test_tmp/**",
    "__pycache__/**",
    "state/**",
    "logs/**",
    "runs/**",
    "results/**",
    "reports/**",
    "exports/**",
    "worktrees/**",
    "workspaces/**",
    "registry.json",
    "registry.json.lock",
    "**/*.log",
    "**/*.pt",
    "**/*.pth",
    "**/*.ckpt",
    "**/*.safetensors",
    "**/*.onnx",
    "**/*.bin",
    ".env",
    "**/.env",
    "**/*.key",
    "**/*.pem",
    "**/id_rsa",
]
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]{2,}", re.UNICODE)


def _rel(path: Path, root: Path) -> str:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        return resolved.relative_to(root_resolved).as_posix()
    except ValueError:
        return f"external:{resolved.as_posix()}"


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


def _infer_workplace_output_root(run_dir: Path) -> Path:
    """Return the root that contains sibling task run directories.

    Normal remote runs use:
      remote_root/task__timestamp/_worker_out

    The remote materializer retry loop builds a second context pack under:
      remote_root/task__timestamp/materializer/attempts/attempt_001/task_id

    In both cases dependency lookup must search remote_root, not the nested
    materializer attempt directory.
    """
    resolved = run_dir.resolve()
    for parent in [resolved, *resolved.parents]:
        if parent.name == "materializer" and parent.parent.parent != parent.parent:
            return parent.parent.parent
    return resolved.parent


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _load_ignore_patterns(repo_root: Path) -> list[str]:
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    ignore_path = repo_root / ".solutionignore"
    if ignore_path.exists():
        for line in ignore_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                patterns.append(stripped)
    return patterns


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _script_registry_entries(registry_path: Path) -> list[str]:
    if not registry_path.exists():
        return []
    try:
        payload = yaml.safe_load(registry_path.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(payload, dict):
        return []
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return []
    return sorted(str(key) for key in scripts.keys())


def _script_registry_source(repo_root: Path) -> Path:
    workspace_registry = repo_root / "solution_agent" / "execution_port" / "script_registry.yaml"
    if workspace_registry.exists():
        return workspace_registry
    return Path(__file__).resolve().parent / "execution_port" / "script_registry.yaml"


def _project_map_source(repo_root: Path) -> Path | None:
    for relative in (".solution/context/project_map.yaml", "config/project_map.yaml"):
        path = repo_root / relative
        if path.exists():
            return path
    return None


def _bounded_project_map_excerpt(repo_root: Path) -> tuple[str, dict[str, Any]]:
    path = _project_map_source(repo_root)
    if not path:
        empty = {
            "schema_version": SCHEMA_VERSION,
            "status": "missing",
            "message": "No project_map.yaml found. Use docs/PROJECT_MAP_BOOTSTRAP.md to create one from a disposable project copy.",
        }
        return yaml.safe_dump(empty, sort_keys=False, allow_unicode=True), {"path": None, "status": "missing"}
    payload = _safe_yaml_load(path)
    allowed_top = {
        "schema_version",
        "project_id",
        "project_name",
        "source",
        "generated_by",
        "created_at",
        "confidence",
        "datasets",
        "checkpoints",
        "scripts",
        "outputs",
        "metrics",
        "workflows",
        "routing_hints",
        "forbidden_paths",
        "open_questions",
        "update_log",
    }
    excerpt = {key: value for key, value in payload.items() if key in allowed_top}
    excerpt.setdefault("schema_version", SCHEMA_VERSION)
    excerpt["status"] = "present"
    excerpt["source_path"] = _rel(path, repo_root)
    text = yaml.safe_dump(excerpt, sort_keys=False, allow_unicode=True)
    if _estimate_tokens(text) > 5000:
        compact = {
            "schema_version": SCHEMA_VERSION,
            "status": "present_truncated",
            "source_path": _rel(path, repo_root),
            "project_id": excerpt.get("project_id"),
            "project_name": excerpt.get("project_name"),
            "datasets": excerpt.get("datasets", [])[:20] if isinstance(excerpt.get("datasets"), list) else excerpt.get("datasets"),
            "checkpoints": excerpt.get("checkpoints", [])[:20] if isinstance(excerpt.get("checkpoints"), list) else excerpt.get("checkpoints"),
            "scripts": excerpt.get("scripts", [])[:30] if isinstance(excerpt.get("scripts"), list) else excerpt.get("scripts"),
            "workflows": excerpt.get("workflows", [])[:20] if isinstance(excerpt.get("workflows"), list) else excerpt.get("workflows"),
            "routing_hints": excerpt.get("routing_hints", [])[:20] if isinstance(excerpt.get("routing_hints"), list) else excerpt.get("routing_hints"),
            "open_questions": excerpt.get("open_questions", [])[:20] if isinstance(excerpt.get("open_questions"), list) else excerpt.get("open_questions"),
        }
        text = yaml.safe_dump(compact, sort_keys=False, allow_unicode=True)
    return text, {"path": _rel(path, repo_root), "status": "present"}


def _safe_yaml_load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _task_front_matter(task_text: str) -> dict[str, Any]:
    text = task_text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _dependency_result_candidates(repo_root: Path, workplace_output_root: Path, dep_id: str) -> list[Path]:
    candidates: list[Path] = []
    local_latest = repo_root / "results" / dep_id / "latest" / "result.yaml"
    if local_latest.exists():
        candidates.append(local_latest)
    if workplace_output_root.exists():
        remote_results = sorted(
            workplace_output_root.glob(f"{dep_id}__*/_worker_out/result.yaml"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        candidates.extend(path for path in remote_results if path not in candidates)
    return candidates


def _artifact_summary_from_dependencies(
    repo_root: Path,
    workplace_output_root: Path,
    task_text: str,
    limit_per_task: int = 30,
) -> tuple[str, list[dict[str, Any]]]:
    front_matter = _task_front_matter(task_text)
    dependency_ids = _as_string_list(front_matter.get("requires_artifacts_from"))
    if not dependency_ids:
        return (
            "# Artifact Summary\n\n"
            "No upstream artifact dependencies were declared for this task.\n",
            [],
        )

    selected: list[dict[str, Any]] = []
    lines = [
        "# Artifact Summary",
        "",
        "Use these exact artifact pointers for upstream dependencies. Do not invent run ids, timestamps, or remote paths.",
        "If a required upstream artifact is absent here, stop as partial and request missing context instead of guessing.",
        "",
    ]
    for dep_id in dependency_ids:
        candidates = _dependency_result_candidates(repo_root, workplace_output_root, dep_id)
        result_path = candidates[0] if candidates else repo_root / "results" / dep_id / "latest" / "result.yaml"
        lines.append(f"## {dep_id}")
        lines.append("")
        if not candidates:
            lines.extend(
                [
                    "- status: missing_local_result",
                    f"- expected_local_result_yaml: {_rel(result_path, repo_root)}",
                    f"- searched_remote_root: {_posix(workplace_output_root)}",
                    "",
                ]
            )
            selected.append(
                {
                    "task_id": dep_id,
                    "status": "missing_result",
                    "expected_local_result_yaml": _rel(result_path, repo_root),
                    "searched_remote_root": _posix(workplace_output_root),
                    "artifacts": [],
                }
            )
            continue
        payload = _safe_yaml_load(result_path)
        artifacts = _as_string_list(payload.get("artifacts_found"))[:limit_per_task]
        missing = _as_string_list(payload.get("artifacts_missing"))[:limit_per_task]
        summary = str(payload.get("summary_short") or "").strip()
        status = str(payload.get("status") or payload.get("execution_status") or "unknown")
        lines.extend(
            [
                f"- status: {status}",
                f"- result_yaml: {_rel(result_path, repo_root)}",
            ]
        )
        if summary:
            lines.append(f"- summary: {summary}")
        if artifacts:
            lines.append("- artifacts_found:")
            for artifact in artifacts:
                lines.append(f"  - {artifact}")
        else:
            lines.append("- artifacts_found: []")
        if missing:
            lines.append("- artifacts_missing:")
            for artifact in missing:
                lines.append(f"  - {artifact}")
        lines.append("")
        selected.append(
            {
                "task_id": dep_id,
                "status": status,
                "summary": summary,
                "result_yaml": _rel(result_path, repo_root),
                "artifacts_found": artifacts,
                "artifacts_missing": missing,
            }
        )
    return "\n".join(lines), selected


def _remote_execution_profile_excerpt(repo_root: Path, workspace_profile: str) -> dict[str, Any]:
    path = repo_root / "config" / "remote_execution_profiles.yaml"
    data = _safe_yaml_load(path)
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    selected: dict[str, Any] = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(name) == workspace_profile or str(profile.get("profile") or "") == workspace_profile or str(profile.get("host") or "") == workspace_profile:
            conda = profile.get("conda") if isinstance(profile.get("conda"), dict) else {}
            selected = {
                "profile": str(name),
                "host": profile.get("host") or name,
                "remote_workdir": profile.get("remote_workdir"),
                "conda_env": conda.get("env") or profile.get("conda_env"),
                "isolation": profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {},
                "project_assets": profile.get("project_assets") if isinstance(profile.get("project_assets"), dict) else {},
            }
            break
    return {
        "path": "config/remote_execution_profiles.yaml" if path.exists() else None,
        "selected": selected,
    }


def _workplace_agent_context_excerpt(repo_root: Path, workspace_profile: str) -> dict[str, Any]:
    local_context = repo_root / ".codex-orchestrator" / "agent_context.yaml"
    if local_context.exists():
        data = _safe_yaml_load(local_context)
        if data:
            return {
                "selected": {
                    "source": _rel(local_context, repo_root),
                    "profile_id": data.get("profile_id"),
                    "host": data.get("host"),
                    "remote_workdir": data.get("remote_workdir"),
                    "remote_root": data.get("remote_root"),
                    "ok": bool(data.get("ok")),
                    "configured_conda_env": data.get("configured_conda_env"),
                    "configured_conda_prefix": data.get("configured_conda_prefix"),
                    "configured_conda_env_ready": data.get("configured_conda_env_ready"),
                    "project_assets": data.get("project_assets") if isinstance(data.get("project_assets"), dict) else {},
                    "remote_materializer": data.get("remote_materializer") if isinstance(data.get("remote_materializer"), dict) else {},
                    "readiness_checks": data.get("readiness_checks") if isinstance(data.get("readiness_checks"), list) else [],
                },
                "available_count": 1,
            }
    state_root = repo_root / "state" / "workplaces"
    matches: list[dict[str, Any]] = []
    if not state_root.exists():
        return {"selected": None, "available_count": 0}
    for path in sorted(state_root.glob("*/agent_context.yaml")):
        data = _safe_yaml_load(path)
        if not data:
            continue
        profile_id = str(data.get("profile_id") or "")
        host = str(data.get("host") or "")
        if workspace_profile and workspace_profile not in {profile_id, host} and not profile_id.startswith(workspace_profile):
            continue
        matches.append(
            {
                "source": _rel(path, repo_root),
                "profile_id": data.get("profile_id"),
                "host": data.get("host"),
                "remote_workdir": data.get("remote_workdir"),
                "remote_root": data.get("remote_root"),
                "ok": bool(data.get("ok")),
                "configured_conda_env": data.get("configured_conda_env"),
                "configured_conda_prefix": data.get("configured_conda_prefix"),
                "configured_conda_env_ready": data.get("configured_conda_env_ready"),
                "project_assets": data.get("project_assets") if isinstance(data.get("project_assets"), dict) else {},
                "remote_materializer": data.get("remote_materializer") if isinstance(data.get("remote_materializer"), dict) else {},
                "readiness_checks": data.get("readiness_checks") if isinstance(data.get("readiness_checks"), list) else [],
            }
        )
    return {
        "selected": matches[0] if matches else None,
        "available_count": len(matches),
    }


def _artifact_read_file(
    repo_root: Path,
    path: Path,
    *,
    task_scope: str,
    max_output_bytes: int,
    allow_truncated_excerpt: bool = False,
) -> tuple[str, dict[str, Any]]:
    artifact_task_id = task_scope
    capsules = TaskCapsuleStore(repo_root / "state")
    try:
        task = capsules.load(artifact_task_id)
    except FileNotFoundError:
        try:
            release_id = build_runtime_identity(repo_root).release_id
        except (OSError, ValueError, json.JSONDecodeError):
            release_id = "context-gateway-v1"
        task = capsules.create(
            TaskCapsule(
                task_id=artifact_task_id,
                objective="Select exact admitted evidence without repository-wide scans.",
                source_release_id=release_id,
                approval_owner="context_gateway",
                scope=ScopeContract(readable_paths=[str(repo_root), str(path.parent)], writable_paths=[]),
                model_budget=ModelBudget(max_context_bytes=max_output_bytes, max_output_bytes=max_output_bytes),
            )
        )
    if str(path.parent.resolve()) not in task.scope.readable_paths:
        task = capsules.extend_read_scope(
            task.task_id,
            [str(path.parent)],
            expected_version=task.version,
        )
    store = ArtifactStore(repo_root / "state")
    manifest = store.put_path(task, path)
    if not any(item.artifact_id == manifest.artifact_id and item.role == "context_required" for item in task.authoritative_artifacts):
        task = capsules.attach_artifact(
            task.task_id,
            ArtifactRef(
                artifact_id=manifest.artifact_id,
                sha256=manifest.sha256,
                role="context_required",
            ),
            authoritative=True,
            expected_version=task.version,
        )
    line_count = max(1, sum(1 for _ in path.open("rb")))
    selector = Selector(type="line_range", value={"start": 1, "end": line_count})
    selected = store.read(
        task,
        manifest.artifact_id,
        selector,
        max_output_bytes=max_output_bytes,
        replay_content=True,
    )
    if selected["read_receipt_id"] not in task.read_receipt_ids:
        task = capsules.attach_read_receipt(
            task.task_id,
            selected["read_receipt_id"],
            expected_version=task.version,
        )
    if selected["truncated"] and not allow_truncated_excerpt:
        raise ValueError(f"CONTEXT_REVIEW_REQUIRED: required evidence exceeds budget: {path}")
    content = str(selected["content"])
    if selected["truncated"]:
        content += "\n\n# [truncated by context gateway]\n"
    entry = {
        "artifact_id": manifest.artifact_id,
        "artifact_sha256": manifest.sha256,
        "selector": selector.model_dump(mode="json"),
        "read_receipt_id": selected["read_receipt_id"],
        "admission_status": "required_and_present" if not selected["truncated"] else "optional_omitted",
        "truncated": selected["truncated"],
        "task_capsule_id": task.task_id,
        "capsule_version": task.version,
    }
    return content, entry


def _write_context_text(path: Path, text: str) -> None:
    target = path
    if os.name == "nt" and len(str(path.resolve())) >= 248:
        target = Path("\\\\?\\" + str(path.resolve()))
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _selected_file_entry(
    repo_root: Path,
    path: Path,
    reason: str,
    artifact_entry: dict[str, Any],
    access: str = "read_only",
) -> dict[str, Any]:
    line_count = max(1, sum(1 for _ in path.open("rb")))
    return {
        "path": _rel(path, repo_root),
        "sha256": _sha256(path),
        "line_ranges": [[1, line_count]],
        "reason": reason,
        "access": access,
        **artifact_entry,
    }


def _retrieval_query(task_text: str, task_id: str) -> str:
    return f"{task_id}\n{task_text[:4000]}"


def _retrieval_context(repo_root: Path, task_text: str, task_id: str, limit: int = 8) -> tuple[str, str, dict[str, Any]]:
    result = query_context_index(repo_root, _retrieval_query(task_text, task_id), limit=limit)
    if not result.get("ok"):
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "skipped",
            "reason": result.get("reason") or "query_failed",
            "index_path": result.get("path"),
            "results": [],
        }
        text = (
            "# Retrieval Context\n\n"
            "No local context index was used for this task. This is non-blocking; "
            "the materializer should rely on the other context_pack files.\n"
        )
        return text, json.dumps(report, ensure_ascii=False, indent=2), report
    compact_results = []
    lines = [
        "# Retrieval Context",
        "",
        f"- mode: {result.get('mode')}",
        f"- results: {len(result.get('results') or [])}",
        "",
    ]
    for idx, item in enumerate(result.get("results") or [], start=1):
        text = str(item.get("text") or "")
        snippet = text[:900] + ("\n[truncated]\n" if len(text) > 900 else "")
        compact = {
            "rank": idx,
            "doc_id": item.get("doc_id"),
            "source_type": item.get("source_type"),
            "title": item.get("title"),
            "tags": item.get("tags") or [],
            "paths": item.get("paths") or [],
            "trust_level": item.get("trust_level"),
            "score": item.get("score"),
            "snippet": snippet,
        }
        compact_results.append(compact)
        lines.extend(
            [
                f"## {idx}. {compact['title']}",
                "",
                f"- doc_id: {compact['doc_id']}",
                f"- source_type: {compact['source_type']}",
                f"- trust_level: {compact['trust_level']}",
                f"- paths: {', '.join(compact['paths']) if compact['paths'] else 'none'}",
                "",
                snippet,
                "",
            ]
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "used",
        "mode": result.get("mode"),
        "index_path": result.get("path"),
        "result_count": len(compact_results),
        "results": compact_results,
    }
    return "\n".join(lines), json.dumps(report, ensure_ascii=False, indent=2), report


def _safe_jsonl(path: Path, limit: int = 800) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if len(rows) >= limit:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _tokens_for_grounding(value: Any) -> set[str]:
    text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True) if not isinstance(value, str) else value
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _compact_grounded_entry(entry: dict[str, Any]) -> dict[str, Any]:
    remote = entry.get("remote") if isinstance(entry.get("remote"), dict) else {}
    run = entry.get("run") if isinstance(entry.get("run"), dict) else {}
    decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
    produced = entry.get("produced_artifacts") or entry.get("important_paths") or entry.get("paths") or []
    if isinstance(produced, list):
        produced = produced[:16]
    compact = {
        "task_id": entry.get("task_id"),
        "run_id": entry.get("run_id"),
        "kind": entry.get("kind") or entry.get("experiment_kind") or entry.get("phase"),
        "status": entry.get("status"),
        "worker": entry.get("worker"),
        "script_id": entry.get("script_id") or entry.get("script_ids"),
        "summary": entry.get("summary") or entry.get("conclusion") or entry.get("log_summary"),
        "claim_scope": entry.get("claim_scope") or decision.get("claim_allowed") or decision.get("claim_scope"),
        "result_path": entry.get("result_path"),
        "produced_artifacts": produced,
        "remote": {
            "host": remote.get("host") or entry.get("host"),
            "remote_workdir": remote.get("remote_workdir") or entry.get("remote_workdir"),
            "remote_root": remote.get("remote_root") or entry.get("remote_root"),
        },
        "run": {
            "execution_success": run.get("execution_success"),
            "failure_type": run.get("failure_type") or entry.get("failure_type"),
            "warning_type": run.get("warning_type") or entry.get("warning_type"),
        },
    }
    if decision:
        compact["decision"] = {
            key: decision.get(key)
            for key in (
                "claim_allowed",
                "claim_scope",
                "requested_model",
                "model",
                "model_fallback_reason",
                "recommended_next_step",
            )
            if decision.get(key) is not None
        }
    return {key: value for key, value in compact.items() if value not in (None, {}, [])}


def _grounded_context_from_artifact_index(
    repo_root: Path,
    task_text: str,
    task_id: str,
    *,
    limit: int = 8,
) -> tuple[str, str, dict[str, Any]]:
    artifact_index = repo_root / ".solution" / "context" / "artifact_index.jsonl"
    rows = _safe_jsonl(artifact_index)
    if not rows:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "missing",
            "artifact_index": _rel(artifact_index, repo_root),
            "entries": [],
        }
        text = "# Grounded Context\n\nNo artifact_index.jsonl entries are available yet.\n"
        return text, yaml.safe_dump(report, sort_keys=False, allow_unicode=True), report

    query_tokens = _tokens_for_grounding(f"{task_id}\n{task_text}")
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, row in enumerate(rows):
        row_tokens = _tokens_for_grounding(
            {
                "task_id": row.get("task_id"),
                "run_id": row.get("run_id"),
                "kind": row.get("kind") or row.get("experiment_kind") or row.get("phase"),
                "summary": row.get("summary") or row.get("conclusion") or row.get("log_summary"),
                "script_id": row.get("script_id") or row.get("script_ids"),
                "tags": row.get("tags"),
                "paths": row.get("produced_artifacts") or row.get("important_paths") or row.get("paths"),
                "decision": row.get("decision"),
            }
        )
        score = len(query_tokens & row_tokens)
        if score > 0 or row.get("kind") in {"collected_run", "workplace_snapshot"}:
            scored.append((score, idx, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [_compact_grounded_entry(row) for _, _, row in scored[:limit]]
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "used" if selected else "empty",
        "artifact_index": _rel(artifact_index, repo_root),
        "entry_count": len(rows),
        "selected_count": len(selected),
        "selection_policy": "token_overlap_then_recent_tail",
        "entries": selected,
    }
    lines = [
        "# Grounded Context",
        "",
        "Relevant prior run/artifact memory selected from .solution/context/artifact_index.jsonl.",
        "Use this to avoid treating the project as first-seen; preserve claim boundaries and fallback notes.",
        "",
    ]
    for idx, entry in enumerate(selected, start=1):
        lines.append(f"## {idx}. {entry.get('task_id') or entry.get('run_id') or 'unknown'}")
        for key in ("status", "kind", "script_id", "claim_scope", "result_path", "summary"):
            if entry.get(key):
                lines.append(f"- {key}: {entry[key]}")
        decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
        if decision:
            lines.append("- decision:")
            for key, value in decision.items():
                lines.append(f"  - {key}: {value}")
        produced = entry.get("produced_artifacts") if isinstance(entry.get("produced_artifacts"), list) else []
        if produced:
            lines.append("- produced_artifacts:")
            for path in produced[:8]:
                lines.append(f"  - {path}")
        lines.append("")
    return "\n".join(lines), yaml.safe_dump(report, sort_keys=False, allow_unicode=True), report


def build_context_pack(
    *,
    task_id: str,
    task_path: Path,
    repo_root: Path,
    out_dir: Path,
    remote_worker: str = "materialized_execution",
    workspace_profile: str = "local",
    remote_conda_env: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Build a bounded, auditable task context pack.

    This first implementation is intentionally deterministic and conservative:
    it writes an audit pack but does not give the API arbitrary source or output
    access.
    """
    repo_root = repo_root.resolve()
    task_path = task_path.resolve()
    out_dir = out_dir.resolve()
    run_dir = out_dir.parent
    workplace_output_root = _infer_workplace_output_root(run_dir)
    default_output_dir = run_dir / "artifacts"
    pack_dir = out_dir / "context_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    ignore_patterns = _load_ignore_patterns(repo_root)

    task_text, task_artifact_entry = _artifact_read_file(
        repo_root,
        task_path,
        task_scope=task_id,
        max_output_bytes=12000,
    )
    registry_path = _script_registry_source(repo_root)
    if registry_path.exists():
        registry_text, registry_artifact_entry = _artifact_read_file(
            repo_root,
            registry_path,
            task_scope=task_id,
            max_output_bytes=40000,
            allow_truncated_excerpt=True,
        )
    else:
        registry_text, registry_artifact_entry = "scripts: {}\n", None
    registry_entries = _script_registry_entries(registry_path)
    project_map_text, project_map_meta = _bounded_project_map_excerpt(repo_root)
    repo_map = (
        "# Repo Map\n\n"
        "Repository-wide discovery is disabled in the integrated focus runtime. "
        "Only Task Capsule artifact refs and explicit indexes may enter context.\n"
    )
    redactions: list[dict[str, str]] = []
    retrieval_text, retrieval_json, retrieval_meta = _retrieval_context(repo_root, task_text, task_id)
    grounded_text, grounded_yaml, grounded_meta = _grounded_context_from_artifact_index(repo_root, task_text, task_id)
    artifact_summary, selected_artifacts = _artifact_summary_from_dependencies(repo_root, workplace_output_root, task_text)
    selected_files = [_selected_file_entry(repo_root, task_path, "task source", task_artifact_entry)]
    if registry_path.exists():
        assert registry_artifact_entry is not None
        selected_files.append(
            _selected_file_entry(
                repo_root,
                registry_path,
                "canonical execution-port script registry",
                registry_artifact_entry,
            )
        )

    task_brief = (
        f"# Task Brief\n\n"
        f"- task_id: {task_id}\n"
        f"- remote_worker: {remote_worker}\n"
        f"- workspace_profile: {workspace_profile}\n"
        f"- remote_conda_env: {remote_conda_env or 'unspecified'}\n\n"
        "## Task Markdown\n\n"
        f"{task_text}\n"
    )
    hints = {
        "schema_version": SCHEMA_VERSION,
        "api_access_policy": "context_pack_only",
        "commands_md_executable": False,
        "output_policy": {
            "mode": "workplace_bound",
            "run_dir": _posix(run_dir),
            "default_output_dir": _posix(default_output_dir),
            "workplace_output_root": _posix(workplace_output_root),
            "allowed_artifact_roots": [
                _posix(repo_root),
                _posix(workplace_output_root),
            ],
            "rules": [
                "If the task book omits output_dir, use output_policy.default_output_dir.",
                "Keep experiment outputs under default_output_dir unless the task explicitly names a safer subdirectory under workplace_output_root.",
                "Do not write experiment outputs into the source repository by default.",
                "Do not write checkpoints, logs, metrics, or generated configs outside allowed_artifact_roots.",
                "For dependency artifacts, use exact pointers from artifact_summary.md; never invent run ids, timestamps, or remote paths.",
            ],
        },
        "execution_plan_gate": {
            "required_for_materialized_execution": True,
            "prefer_sources": [
                "existing parquet/csv/json/yaml manifests",
                "dataset config files",
                "project_map_excerpt.yaml",
                "artifact_summary.md",
                "bounded samples only when explicitly needed",
            ],
            "blocked_without_task_approval": [
                "full recursive dataset scans",
                "opening every image file/header",
                "reading image pixels",
                "copying or moving source data",
                "modifying datasets or checkpoints",
                "unbounded reads over raw dataset roots",
            ],
            "approval_flags": [
                "allow_heavy_data_scan",
                "allow_recursive_dataset_scan",
                "allow_image_header_scan",
                "allow_image_pixel_access",
                "allow_data_copy",
                "allow_dataset_modification",
            ],
        },
        "read_scope": {
            "include": ["task.md", "script_registry_excerpt.yaml", "workspace_context_excerpt.yaml", "project_map_excerpt.yaml", "grounded_context.md", "grounded_context.yaml", "retrieval_context.md", "retrieval_context.json", "repo_map_excerpt.md", "selected_summaries.md", "artifact_summary.md"],
            "exclude": ignore_patterns,
        },
        "write_scope": {
            "include": ["_worker_out/**", "generated/**", f"{_posix(default_output_dir)}/**"],
            "exclude": ["solution_agent/**", "scripts/**", ".git/**", "results/**", "runs/**"],
        },
        "missing_context_policy": {
            "allowed_kinds": ["file_summary", "symbol_summary", "script_registry_entry", "artifact_summary"],
            "denied_kinds": ["raw_checkpoint", "full_log", "secret_file", "arbitrary_shell_scan"],
            "max_rounds": 2,
            "max_added_tokens": 6000,
        },
    }
    selected_summaries = (
        "# Selected Summaries\n\n"
        "- task.md: included as task brief source.\n"
        "- script_registry_excerpt.yaml: canonical execution allowlist excerpt.\n"
        "- workspace_context_excerpt.yaml: sanitized remote workplace/profile/init context when available.\n"
        "- project_map_excerpt.yaml: bounded project asset/workflow map when available.\n"
        "- grounded_context.yaml/md: relevant prior run/artifact memory with claim boundaries.\n"
    )
    workspace_context = {
        "schema_version": SCHEMA_VERSION,
        "workspace_profile": workspace_profile,
        "remote_conda_env_from_task": remote_conda_env,
        "remote_execution_profile": _remote_execution_profile_excerpt(repo_root, workspace_profile),
        "workplace_init": _workplace_agent_context_excerpt(repo_root, workspace_profile),
        "workplace_output": {
            "policy": "workplace_bound",
            "run_dir": _posix(run_dir),
            "default_output_dir": _posix(default_output_dir),
            "workplace_output_root": _posix(workplace_output_root),
            "notes": [
                "Tasks should target a workplace, not hard-code a global output path.",
                "When output_dir is absent, materialized_execution should write outputs to default_output_dir.",
            ],
        },
        "notes": [
            "This file is a bounded summary for model orientation.",
            "Do not scan state/workplaces, logs, outputs, or remote roots unless an allowed missing_context_request is approved.",
            "Secrets and auth token values are intentionally omitted.",
        ],
    }

    files = {
        "task_brief.md": task_brief,
        "repo_map_excerpt.md": repo_map,
        "script_registry_excerpt.yaml": registry_text,
        "workspace_context_excerpt.yaml": yaml.safe_dump(workspace_context, sort_keys=False, allow_unicode=True),
        "project_map_excerpt.yaml": project_map_text,
        "grounded_context.md": grounded_text,
        "grounded_context.yaml": grounded_yaml,
        "selected_summaries.md": selected_summaries,
        "artifact_summary.md": artifact_summary,
        "retrieval_context.md": retrieval_text,
        "retrieval_context.json": retrieval_json,
        "execution_hints.yaml": yaml.safe_dump(hints, sort_keys=False, allow_unicode=True),
        "selected_files.json": json.dumps(selected_files, ensure_ascii=False, indent=2),
    }
    for name, text in files.items():
        _write_context_text(pack_dir / name, text)

    estimated_tokens = sum(_estimate_tokens(text) for text in files.values())
    context_pack_id = f"ctx_{now_iso().replace('-', '').replace(':', '').replace('+', '_').replace('T', '_')}_{task_id}"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "context_pack_id": context_pack_id,
        "task_id": task_id,
        "created_at": now_iso(),
        "repo_commit": _git_commit(repo_root),
        "workspace_profile": workspace_profile,
        "remote_worker": remote_worker,
        "remote_conda_env": remote_conda_env,
        "budget": {
            "estimated_tokens": estimated_tokens,
            "max_tokens": max_tokens,
            "reserved_output_tokens": 4000,
        },
        "inputs": {
            "task_md": _rel(task_path, repo_root),
            "script_registry": _rel(registry_path, repo_root) if registry_path.exists() else None,
            "project_map": project_map_meta.get("path"),
            "artifact_index": grounded_meta.get("artifact_index"),
            "grounded_context": "context_pack/grounded_context.yaml",
            "context_index": retrieval_meta.get("index_path"),
            "artifact_summary": "context_pack/artifact_summary.md",
            "solutionignore": ".solutionignore" if (repo_root / ".solutionignore").exists() else None,
            "workspace_context": "context_pack/workspace_context_excerpt.yaml",
            "default_output_dir": _posix(default_output_dir),
        },
        "retrieval": {
            "status": retrieval_meta.get("status"),
            "mode": retrieval_meta.get("mode"),
            "result_count": retrieval_meta.get("result_count", 0),
        },
        "grounding": {
            "status": grounded_meta.get("status"),
            "artifact_index": grounded_meta.get("artifact_index"),
            "entry_count": grounded_meta.get("entry_count", 0),
            "selected_count": grounded_meta.get("selected_count", 0),
        },
        "selected": {
            "files": selected_files,
            "registry_entries": registry_entries,
            "dependency_artifacts": selected_artifacts,
        },
        "redactions": redactions,
        "missing_context_rounds": 0,
    }
    _write_context_text(pack_dir / "manifest.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    return manifest

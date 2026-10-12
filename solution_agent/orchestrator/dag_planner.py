from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .paths import result_latest_dir, root
from .contract_validator import validate_final_contract
from .registry import load_registry
from .schema import TaskBook
from .task_loader import load_task


TASK_STATUSES = ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed")


def _norm(value: object) -> str:
    return str(value or "").strip()


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "gpu"}


def _listify(value: object) -> List[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _string_list(value: object) -> List[str]:
    return [_norm(item) for item in _listify(value) if _norm(item)]


def _scope_value(task: TaskBook, key: str) -> str:
    raw = task.raw_front_matter or {}
    aliases = {
        "line": ("line", "task_line"),
        "batch": ("batch", "batch_id"),
        "dag_root": ("dag_root", "root_task", "dag"),
    }
    for name in aliases.get(key, (key,)):
        if _norm(raw.get(name)):
            return _norm(raw.get(name))
    tags = raw.get("tags")
    if isinstance(tags, dict):
        for name in aliases.get(key, (key,)):
            if _norm(tags.get(name)):
                return _norm(tags.get(name))
    return ""


def _load_all_tasks() -> Dict[str, Dict[str, object]]:
    tasks: Dict[str, Dict[str, object]] = {}
    task_root = root() / "tasks"
    for status in TASK_STATUSES:
        status_dir = task_root / status
        if not status_dir.exists():
            continue
        for task_path in sorted(status_dir.glob("*/task.md")):
            task = load_task(task_path)
            tasks[task.task_id] = {"task": task, "status": status, "path": task_path}
    return tasks


def _matches_scope(
    task: TaskBook,
    *,
    line: Optional[str],
    batch: Optional[str],
    dag_root: Optional[str],
    prefix: Optional[str],
) -> bool:
    if _norm(line) and _scope_value(task, "line") != _norm(line):
        return False
    if _norm(batch) and _scope_value(task, "batch") != _norm(batch):
        return False
    explicit_root = _scope_value(task, "dag_root")
    if _norm(dag_root) and explicit_root and explicit_root != _norm(dag_root):
        return False
    if _norm(prefix) and not task.task_id.startswith(_norm(prefix)):
        return False
    return True


def _dependency_sections(raw: Dict[str, Any]) -> tuple[List[str], List[str]]:
    deps = raw.get("dependencies")
    blocking: List[str] = []
    optional: List[str] = []
    if isinstance(deps, dict):
        blocking.extend(_string_list(deps.get("blocking")))
        optional.extend(_string_list(deps.get("optional")))
    blocking.extend(_string_list(raw.get("depends_on")))
    optional.extend(_string_list(raw.get("optional_depends_on")))
    seen: set[str] = set()
    blocking_unique = []
    for dep in blocking:
        if dep not in seen:
            blocking_unique.append(dep)
            seen.add(dep)
    optional_unique = []
    for dep in optional:
        if dep and dep not in seen and dep not in optional_unique:
            optional_unique.append(dep)
    return blocking_unique, optional_unique


def _artifact_requirements(raw: Dict[str, Any]) -> List[Dict[str, object]]:
    requirements: List[Dict[str, object]] = []
    for item in _listify(raw.get("requires_artifacts_from")):
        if isinstance(item, dict):
            from_task = _norm(item.get("task_id") or item.get("from_task"))
            files = _string_list(item.get("artifacts") or item.get("files"))
        else:
            from_task = _norm(item)
            files = []
        if from_task:
            requirements.append({"from_task": from_task, "files": files})
    for item in _listify(raw.get("input_requirements")):
        if not isinstance(item, dict):
            continue
        from_task = _norm(item.get("from_task") or item.get("task_id"))
        files = _string_list(item.get("artifacts") or item.get("files") or item.get("required_artifacts"))
        if from_task:
            requirements.append({"from_task": from_task, "files": files})
    return requirements


def _infer_lane(task: TaskBook) -> str:
    raw = task.raw_front_matter or {}
    explicit = _norm(raw.get("resource_lane"))
    if explicit:
        return explicit
    worker = _norm(task.worker)
    intent = _norm(raw.get("intent") or raw.get("execution_stage")).lower()
    resource_hint = _norm(raw.get("resource_hint")).lower()
    if _boolish(raw.get("needs_gpu")) or resource_hint == "gpu":
        return "gpu_run"
    if raw.get("allow_code_changes") is True or intent in {"source_change", "code_change", "implementation"}:
        return "source_change"
    if worker == "remote_materializer" or "material" in intent:
        return "materializer_api"
    return "cpu_analysis"


def _infer_resource_hint(task: TaskBook) -> str:
    raw = task.raw_front_matter or {}
    explicit = _norm(raw.get("resource_hint")).lower()
    if explicit:
        return explicit
    return "gpu" if _infer_lane(task) == "gpu_run" else "cpu"


def _outputs(task: TaskBook) -> List[str]:
    raw = task.raw_front_matter or {}
    return _string_list(raw.get("outputs") or raw.get("expected_outputs") or task.expected_outputs)


def _infer_dag_root(scope_tasks: Iterable[TaskBook], requested_root: Optional[str]) -> str:
    if _norm(requested_root):
        return _norm(requested_root)
    tasks = list(scope_tasks)
    explicit = [_scope_value(task, "dag_root") for task in tasks if _scope_value(task, "dag_root")]
    if explicit:
        return sorted(explicit)[0]
    no_deps = []
    for task in tasks:
        blocking, _ = _dependency_sections(task.raw_front_matter or {})
        if not blocking:
            no_deps.append(task)
    preferred_tokens = ("_000", "_001", "root", "prepare", "inventory")
    preferred = [task for task in no_deps if any(token in task.task_id.lower() for token in preferred_tokens)]
    candidates = preferred or no_deps or tasks
    return sorted((task.task_id for task in candidates))[0] if candidates else ""


def metabolize_task(task: TaskBook, *, dag_root: str = "") -> Dict[str, object]:
    raw = task.raw_front_matter or {}
    blocking, optional = _dependency_sections(raw)
    return {
        "task_id": task.task_id,
        "title": task.title,
        "line": _scope_value(task, "line"),
        "batch": _scope_value(task, "batch"),
        "dag_root": _scope_value(task, "dag_root") or dag_root,
        "worker": _norm(task.worker),
        "intent": _norm(raw.get("intent") or raw.get("execution_stage")),
        "resource_lane": _infer_lane(task),
        "resource_hint": _infer_resource_hint(task),
        "needs_gpu": _infer_resource_hint(task) == "gpu",
        "blocking": blocking,
        "optional": optional,
        "artifact_requirements": _artifact_requirements(raw),
        "outputs": _outputs(task),
        "allow_provisional": bool((raw.get("partial_policy") or {}).get("allow_provisional")) if isinstance(raw.get("partial_policy"), dict) else bool(raw.get("allow_provisional", False)),
        "decision_contract": raw.get("decision_contract") if isinstance(raw.get("decision_contract"), dict) else {},
    }


def _record_status(records: Dict[str, object], task_id: str) -> str:
    record = records.get(task_id)
    if isinstance(record, dict):
        return _norm(record.get("status"))
    return ""


def _result_root_for(records: Dict[str, object], task_id: str) -> Optional[Path]:
    record = records.get(task_id)
    candidates: List[object] = []
    if isinstance(record, dict):
        candidates.extend([record.get("latest_result_path"), record.get("result_path")])
    candidates.append(result_latest_dir(task_id))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if path.exists():
            return path
    return None


def _artifact_blockers(contract: Dict[str, object], records: Dict[str, object]) -> List[Dict[str, object]]:
    blockers: List[Dict[str, object]] = []
    for req in contract.get("artifact_requirements", []) or []:
        if not isinstance(req, dict):
            continue
        from_task = _norm(req.get("from_task"))
        if not from_task:
            continue
        status = _record_status(records, from_task)
        if status not in {"completed", "partial"}:
            blockers.append({"task_id": from_task, "reason": "artifact_source_not_ready", "status": status or "missing"})
            continue
        result_root = _result_root_for(records, from_task)
        files = _string_list(req.get("files"))
        if not result_root:
            blockers.append({"task_id": from_task, "reason": "artifact_root_missing", "files": files})
            continue
        for name in files:
            if not (result_root / name).exists() and not (result_root / "artifacts" / name).exists():
                blockers.append({"task_id": from_task, "reason": "required_artifact_missing", "artifact": name, "artifact_root": str(result_root)})
    return blockers


def _readiness(contract: Dict[str, object], records: Dict[str, object]) -> Dict[str, object]:
    blocked_by: List[Dict[str, object]] = []
    for dep in contract.get("blocking", []) or []:
        status = _record_status(records, str(dep))
        if status != "completed":
            blocked_by.append({"task_id": str(dep), "reason": "blocking_dependency_not_completed", "status": status or "missing"})
    blocked_by.extend(_artifact_blockers(contract, records))
    optional_missing = []
    for dep in contract.get("optional", []) or []:
        status = _record_status(records, str(dep))
        if status != "completed":
            optional_missing.append({"task_id": str(dep), "reason": "optional_dependency_not_completed", "status": status or "missing"})
    ready_full = not blocked_by and not optional_missing
    ready_partial = not blocked_by and bool(optional_missing) and bool(contract.get("allow_provisional"))
    return {
        "ready": ready_full or ready_partial,
        "ready_full": ready_full,
        "ready_partial": ready_partial,
        "blocked_by": blocked_by,
        "optional_missing": optional_missing,
    }


def _contract_status(contract: Dict[str, object], records: Dict[str, object]) -> Dict[str, object]:
    task_id = str(contract["task_id"])
    status = _record_status(records, task_id)
    decision = contract.get("decision_contract") if isinstance(contract.get("decision_contract"), dict) else {}
    required_files = _string_list(decision.get("required_files"))
    required_fields = _string_list(decision.get("required_fields"))
    output_files = _string_list(contract.get("outputs"))
    if not required_files and any(name.endswith(".json") or name.endswith(".md") for name in output_files):
        required_files = output_files
    if status != "completed":
        return {"checked": False, "reason": "task_not_completed", "required_files": required_files, "required_fields": required_fields}
    result_root = _result_root_for(records, task_id)
    task = contract.get("_task")
    if isinstance(task, TaskBook) and isinstance(decision, dict) and decision:
        if not result_root:
            return {
                "checked": True,
                "ok": False,
                "reason": "result_root_missing",
                "result_root": None,
                "missing_files": required_files,
                "required_files": required_files,
                "required_fields": required_fields,
            }
        validation = validate_final_contract(task, result_root)
        payload = validation.to_dict()
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        payload.update(
            {
                "result_root": str(result_root),
                "required_files": required_files,
                "required_fields": required_fields,
                "missing_files": details.get("missing_files", []),
                "missing_fields": details.get("missing_fields", []),
            }
        )
        return payload
    missing_files = []
    if result_root:
        for name in required_files:
            if not (result_root / name).exists() and not (result_root / "artifacts" / name).exists():
                missing_files.append(name)
    else:
        missing_files = required_files
    return {
        "checked": True,
        "ok": not missing_files,
        "result_root": str(result_root) if result_root else None,
        "missing_files": missing_files,
        "required_files": required_files,
        "required_fields": required_fields,
    }


def _wave_plan(contracts: List[Dict[str, object]], records: Dict[str, object]) -> List[Dict[str, object]]:
    pending = {str(contract["task_id"]): contract for contract in contracts if _record_status(records, str(contract["task_id"])) not in {"completed", "running", "launching"}}
    completed = {task_id for task_id, record in records.items() if isinstance(record, dict) and _norm(record.get("status")) == "completed"}
    waves: List[Dict[str, object]] = []
    for index in range(len(pending) + 1):
        ready = []
        for task_id, contract in pending.items():
            deps = [str(dep) for dep in contract.get("blocking", []) or []]
            if all(dep in completed or dep not in pending for dep in deps):
                readiness = _readiness(contract, records)
                if readiness["ready"] or all(dep in completed for dep in deps if dep in pending):
                    ready.append(contract)
        if not ready:
            break
        lanes: Dict[str, List[Dict[str, object]]] = {}
        for contract in sorted(ready, key=lambda item: str(item["task_id"])):
            lanes.setdefault(str(contract.get("resource_lane") or "cpu_analysis"), []).append(
                {"task_id": contract["task_id"], "reason": "blocking dependencies satisfied within DAG wave"}
            )
        waves.append({"wave": index, "lanes": lanes})
        for contract in ready:
            task_id = str(contract["task_id"])
            completed.add(task_id)
            pending.pop(task_id, None)
    return waves


def dag_plan(
    *,
    line: Optional[str] = None,
    batch: Optional[str] = None,
    dag_root: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Dict[str, object]:
    loaded = _load_all_tasks()
    scope_entries = [
        entry
        for entry in loaded.values()
        if _matches_scope(entry["task"], line=line, batch=batch, dag_root=dag_root, prefix=prefix)  # type: ignore[index]
    ]
    scope_tasks = [entry["task"] for entry in scope_entries]  # type: ignore[index]
    inferred_root = _infer_dag_root(scope_tasks, dag_root)
    contracts = [metabolize_task(task, dag_root=inferred_root) for task in scope_tasks]
    registry = load_registry()
    records = registry.get("tasks", {}) if isinstance(registry, dict) and isinstance(registry.get("tasks"), dict) else {}
    task_statuses = {entry["task"].task_id: entry["status"] for entry in scope_entries}  # type: ignore[index,union-attr]
    normalized = []
    for contract in sorted(contracts, key=lambda item: str(item["task_id"])):
        task_obj = loaded.get(str(contract["task_id"]), {}).get("task") if isinstance(loaded.get(str(contract["task_id"])), dict) else None
        contract_for_status = {**contract, "_task": task_obj} if isinstance(task_obj, TaskBook) else contract
        readiness = _readiness(contract, records)
        normalized.append(
            {
                **contract,
                "status": task_statuses.get(str(contract["task_id"]), _record_status(records, str(contract["task_id"]))),
                "readiness": readiness,
                "final_contract_status": _contract_status(contract_for_status, records),
            }
        )
    missing_dependencies = sorted(
        {
            str(dep)
            for contract in normalized
            for dep in list(contract.get("blocking", []) or []) + list(contract.get("optional", []) or [])
            if dep and dep not in loaded and dep not in records
        }
    )
    return {
        "selector": {"line": line, "batch": batch, "dag_root": dag_root, "prefix": prefix},
        "dag_root": inferred_root,
        "task_count": len(normalized),
        "tasks": normalized,
        "missing_dependencies": missing_dependencies,
        "waves": _wave_plan(contracts, records),
    }

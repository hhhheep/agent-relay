from __future__ import annotations

import html
import json
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .orchestrator import core as lab
from .orchestrator.registry import load_registry
from .orchestrator.paths import result_latest_dir, root, state_dir, task_file
from .orchestrator.supervisor import DEFAULT_OLLAMA_MODEL, call_ollama
from .orchestrator.task_loader import load_task


DEFAULT_HOST = "EXAMPLE_WORKER_B"
DEFAULT_REMOTE_ROOT = "/srv/example-project/outputs/lab-runs"
DEFAULT_REMOTE_WORKDIR = "/srv/example-project"
DEFAULT_ALLOWED_GPUS = "0,1,2,4"
DEFAULT_GPU_SNAPSHOT_CACHE_SECONDS = 600
DEFAULT_REMOTE_WATCH_INTERVAL_SECONDS = 30
MAX_CUSTOM_REMOTE_PRESETS = 10
DASHBOARD_VERSION = "local v1.3.1"
DEFAULT_REMOTE_WORKER = "materialized_execution"
REMOTE_CONTEXT_CACHE_KEEP_LATEST = 50
REMOTE_WORKER_CHOICES = [
    ("materialized_execution", "Own agent"),
    ("codex", "Codex"),
]
DEFAULT_REMOTE_PRESETS = [
    {
        "label": "EXAMPLE_WORKER_B deepfake",
        "host": DEFAULT_HOST,
        "remote_workdir": DEFAULT_REMOTE_WORKDIR,
        "remote_root": DEFAULT_REMOTE_ROOT,
        "allowed_gpu_ids": DEFAULT_ALLOWED_GPUS,
        "cuda_mapping_note": "CUDA 0/1/2/3 maps to nvidia-smi 0/1/2/4 on this server.",
    }
]


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _remote_worker_label(value: object) -> str:
    worker = str(value or DEFAULT_REMOTE_WORKER)
    for key, label in REMOTE_WORKER_CHOICES:
        if key == worker:
            return label
    return worker


def _remote_worker_select(name: str, active_worker: object, *, autosubmit: bool = False) -> str:
    active = str(active_worker or DEFAULT_REMOTE_WORKER)
    onchange = ' onchange="this.form.submit()"' if autosubmit else ""
    options = "".join(
        f'<option value="{_esc(value)}" {"selected" if active == value else ""}>{_esc(label)}</option>'
        for value, label in REMOTE_WORKER_CHOICES
    )
    return f'<select name="{_esc(name)}"{onchange}>{options}</select>'


def _read_text(path: Path, limit: int = 40000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n\n...[truncated]"
    return text


def _tail(path: Path, limit: int = 8000) -> str:
    text = _read_text(path, limit=limit * 2)
    if len(text) <= limit:
        return text
    return text[-limit:]


def _dashboard_config_path() -> Path:
    return state_dir() / "dashboard_config.json"


def _load_dashboard_config() -> Dict[str, object]:
    path = _dashboard_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_dashboard_config(config: Dict[str, object]) -> None:
    path = _dashboard_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    saved_keys = {
        "host",
        "remote_root",
        "remote_workdir",
        "allowed_gpu_ids",
        "min_free_gpu_mem_gb",
        "max_starts_per_cycle",
        "max_running_total",
        "max_running_gpu",
        "max_running_cpu",
        "max_tasks_per_gpu",
        "interval_minutes",
        "model",
        "remote_presets",
        "cuda_mapping_note",
        "gpu_snapshot_cache_seconds",
        "hidden_history_workplaces",
        "remote_watch_interval_seconds",
        "remote_worker",
    }
    payload = {key: config.get(key) for key in sorted(saved_keys) if key in config}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _ssh_host_aliases() -> List[str]:
    config_path = Path.home() / ".ssh" / "config"
    try:
        exists = config_path.exists()
    except OSError:
        return []
    if not exists:
        return []
    aliases: List[str] = []
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2 or parts[0].lower() != "host":
            continue
        for alias in parts[1:]:
            if "*" in alias or "?" in alias:
                continue
            if alias not in aliases:
                aliases.append(alias)
    return aliases


def _preset_signature(preset: Dict[str, object]) -> tuple[str, str, str]:
    return (
        str(preset.get("host") or ""),
        str(preset.get("remote_workdir") or ""),
        str(preset.get("remote_root") or ""),
    )


def _current_remote_preset(config: Dict[str, object], label: str = "Current target") -> Dict[str, str]:
    return {
        "label": label,
        "host": str(config.get("host") or DEFAULT_HOST),
        "remote_workdir": str(config.get("remote_workdir") or DEFAULT_REMOTE_WORKDIR),
        "remote_root": str(config.get("remote_root") or DEFAULT_REMOTE_ROOT),
        "allowed_gpu_ids": str(config.get("allowed_gpu_ids") or DEFAULT_ALLOWED_GPUS),
        "cuda_mapping_note": str(config.get("cuda_mapping_note") or ""),
    }


def _custom_remote_presets(config: Dict[str, object]) -> List[Dict[str, str]]:
    raw = config.get("remote_presets")
    if not isinstance(raw, list):
        return []
    presets: List[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        preset = {
            "label": str(item.get("label") or item.get("host") or "Remote target"),
            "host": str(item.get("host") or ""),
            "remote_workdir": str(item.get("remote_workdir") or ""),
            "remote_root": str(item.get("remote_root") or ""),
            "allowed_gpu_ids": str(item.get("allowed_gpu_ids") or DEFAULT_ALLOWED_GPUS),
            "cuda_mapping_note": str(item.get("cuda_mapping_note") or ""),
        }
        signature = _preset_signature(preset)
        if not all(signature) or signature in seen:
            continue
        presets.append(preset)
        seen.add(signature)
        if len(presets) >= MAX_CUSTOM_REMOTE_PRESETS:
            break
    return presets


def _remember_remote_preset(config: Dict[str, object]) -> None:
    current = _current_remote_preset(config, label=str(config.get("host") or "Remote target"))
    signature = _preset_signature(current)
    built_in_signatures = {_preset_signature(item) for item in DEFAULT_REMOTE_PRESETS}
    custom = [item for item in _custom_remote_presets(config) if _preset_signature(item) != signature]
    if signature not in built_in_signatures:
        custom.insert(0, current)
    config["remote_presets"] = custom[:MAX_CUSTOM_REMOTE_PRESETS]


def _delete_remote_preset(config: Dict[str, object], index: int) -> bool:
    custom = _custom_remote_presets(config)
    if index < 0 or index >= len(custom):
        return False
    del custom[index]
    config["remote_presets"] = custom
    return True


def _remote_presets(config: Dict[str, object]) -> List[Dict[str, str]]:
    presets = [dict(item) | {"builtin": "1"} for item in DEFAULT_REMOTE_PRESETS]
    custom = _custom_remote_presets(config)
    presets.extend(dict(item) | {"custom_index": str(index)} for index, item in enumerate(custom))
    current = _current_remote_preset(config)
    if all(_preset_signature(item) != _preset_signature(current) for item in presets):
        presets.insert(0, current)
    return presets


def _execution_profile_for_target(host: object, remote_workdir: object) -> Dict[str, object]:
    data = lab.load_remote_execution_profiles()
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    host_text = str(host or "")
    workdir_text = str(remote_workdir or "").rstrip("/")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        profile_host = str(profile.get("host") or name)
        profile_workdir = str(profile.get("remote_workdir") or "").rstrip("/")
        if profile_host == host_text and profile_workdir == workdir_text:
            return dict(profile) | {"profile_name": str(name)}
    for name, profile in profiles.items():
        if isinstance(profile, dict) and str(profile.get("host") or name) == host_text:
            return dict(profile) | {"profile_name": str(name), "profile_match": "host_only"}
    return {}


def _profile_execution_summary(profile: Dict[str, object]) -> str:
    if not profile:
        return '<span class="badge warn">no env profile</span>'
    conda = profile.get("conda") if isinstance(profile.get("conda"), dict) else {}
    env = conda.get("prefix") or conda.get("env") or profile.get("python") or "python3"
    match = "host-only" if profile.get("profile_match") == "host_only" else "matched"
    return f'<span class="badge">{_esc(profile.get("profile_name") or "profile")}</span> <span class="muted">{_esc(env)} · {_esc(match)}</span>'


def _remote_workplace_key(item: Dict[str, object]) -> tuple[str, str]:
    return (
        str(item.get("host") or ""),
        str(item.get("remote_root") or ""),
    )


def _workplace_config_key(host: str, remote_root: str) -> str:
    return json.dumps([str(host or ""), str(remote_root or "")], separators=(",", ":"))


def _parse_workplace_config_key(value: str) -> tuple[str, str]:
    try:
        data = json.loads(value)
    except Exception:
        return ("", "")
    if not isinstance(data, list) or len(data) != 2:
        return ("", "")
    return (str(data[0] or ""), str(data[1] or ""))


def _hidden_history_workplaces(config: Dict[str, object]) -> set[str]:
    raw = config.get("hidden_history_workplaces")
    if not isinstance(raw, list):
        return set()
    hidden: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            hidden.add(item)
            continue
        if isinstance(item, dict):
            hidden.add(_workplace_config_key(str(item.get("host") or ""), str(item.get("remote_root") or "")))
    return hidden


def _hide_history_workplace(config: Dict[str, object], host: str, remote_root: str) -> bool:
    if not host or not remote_root:
        return False
    hidden = _hidden_history_workplaces(config)
    hidden.add(_workplace_config_key(host, remote_root))
    config["hidden_history_workplaces"] = sorted(hidden)
    return True


def _show_hidden_history_workplaces(config: Dict[str, object]) -> int:
    count = len(_hidden_history_workplaces(config))
    config["hidden_history_workplaces"] = []
    return count


def _show_history_workplace(config: Dict[str, object], host: str, remote_root: str) -> bool:
    key = _workplace_config_key(host, remote_root)
    hidden = _hidden_history_workplaces(config)
    if key not in hidden:
        return False
    hidden.remove(key)
    config["hidden_history_workplaces"] = sorted(hidden)
    return True


def _bulk_history_workplaces(config: Dict[str, object], keys: Iterable[str], operation: str) -> Dict[str, object]:
    changed = 0
    valid = 0
    for raw_key in keys:
        host, remote_root = _parse_workplace_config_key(str(raw_key))
        if not host or not remote_root:
            continue
        valid += 1
        if operation == "forget":
            changed += 1 if _hide_history_workplace(config, host, remote_root) else 0
        elif operation == "restore":
            changed += 1 if _show_history_workplace(config, host, remote_root) else 0
    return {
        "ok": True,
        "operation": operation,
        "selected_count": valid,
        "changed_count": changed,
        "hidden_history_count": len(_hidden_history_workplaces(config)),
    }


LOCAL_CLEANUP_CATEGORIES = {
    "temp": {
        "label": "Temp folders",
        "paths": [".tmp", ".test_tmp"],
        "description": "Local dashboard/test scratch folders.",
    },
    "test_cache": {
        "label": "Pytest/test cache",
        "paths": [".pytest_cache"],
        "description": "Local pytest cache residue. Safe to remove; tests do not depend on it.",
    },
    "logs": {
        "label": "Logs",
        "paths": ["logs"],
        "description": "Local dashboard and worker logs.",
    },
    "dispatch": {
        "label": "Inactive dispatch bundles",
        "paths": ["dispatch"],
        "description": "Launch bundles for tasks that are not currently running.",
    },
    "worktrees": {
        "label": "Inactive worktrees",
        "paths": ["worktrees"],
        "description": "Local worker output/worktree folders for tasks that are not currently running.",
    },
    "inbox_archive": {
        "label": "Processed inbox archive",
        "paths": ["tasks/inbox/processed", "tasks/inbox/_discarded"],
        "description": "Recoverable inbox staging archive and discarded batches.",
    },
    "hidden_completed_results": {
        "label": "Hidden completed results",
        "paths": ["results"],
        "description": "Collected result folders only for completed tasks hidden from the board.",
    },
    "remote_context_cache": {
        "label": "Old remote context cache",
        "paths": ["state/remote_context_*"],
        "description": f"Old remote context tarballs. Keeps the newest {REMOTE_CONTEXT_CACHE_KEEP_LATEST} for reconnect/debug.",
    },
}


def _safe_local_cleanup_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(root().resolve())
    except ValueError:
        return False
    return resolved != root().resolve()


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _active_task_ids() -> set[str]:
    tasks = load_registry().get("tasks", {})
    active: set[str] = set()
    for task_id, record in (tasks.items() if isinstance(tasks, dict) else []):
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "") in {"running", "launching", "launch_unknown"}:
            active.add(str(record.get("task_id") or task_id))
    return active


def _local_cleanup_candidates(categories: Iterable[str]) -> List[Dict[str, object]]:
    selected = [item for item in categories if item in LOCAL_CLEANUP_CATEGORIES]
    active = _active_task_ids()
    hidden_completed = _hidden_completed()
    tasks = load_registry().get("tasks", {})
    candidates: List[Dict[str, object]] = []
    seen: set[Path] = set()

    def add(path: Path, category: str, reason: str) -> None:
        if not path.exists() or not _safe_local_cleanup_path(path):
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(
            {
                "category": category,
                "path": str(path),
                "size_bytes": _path_size(path),
                "reason": reason,
            }
        )

    for category in selected:
        if category == "test_cache":
            add(root() / ".pytest_cache", category, LOCAL_CLEANUP_CATEGORIES[category]["description"])
            state_base = state_dir()
            if state_base.exists():
                for child in state_base.glob("pytest-cache-files-*"):
                    add(child, category, "pytest temp cache residue under state/")
                add(state_base / "pytest_cache", category, "pytest cache directory under state/")
            continue
        if category == "dispatch":
            base = root() / "dispatch"
            if base.exists():
                for child in base.iterdir():
                    task_id = child.name
                    if task_id not in active:
                        add(child, category, "inactive dispatch artifact")
            continue
        if category == "worktrees":
            base = root() / "worktrees"
            if base.exists():
                for child in base.iterdir():
                    task_id = child.name
                    if task_id not in active:
                        add(child, category, "inactive local worktree/output")
            continue
        if category == "hidden_completed_results":
            if isinstance(tasks, dict):
                for task_id in sorted(hidden_completed):
                    record = tasks.get(task_id)
                    if isinstance(record, dict) and str(record.get("status") or "") == "completed":
                        add(root() / "results" / task_id, category, "completed result hidden from dashboard")
            continue
        if category == "remote_context_cache":
            state_base = state_dir()
            if state_base.exists():
                items = sorted(
                    [item for item in state_base.glob("remote_context_*") if item.exists()],
                    key=lambda path: path.stat().st_mtime if path.exists() else 0,
                    reverse=True,
                )
                for child in items[REMOTE_CONTEXT_CACHE_KEEP_LATEST:]:
                    add(child, category, f"old remote context cache; newest {REMOTE_CONTEXT_CACHE_KEEP_LATEST} are kept")
            continue
        for rel in LOCAL_CLEANUP_CATEGORIES[category]["paths"]:
            add(root() / rel, category, LOCAL_CLEANUP_CATEGORIES[category]["description"])
    return candidates


def _local_cleanup_plan(categories: Iterable[str]) -> Dict[str, object]:
    selected = [item for item in categories if item in LOCAL_CLEANUP_CATEGORIES]
    if not selected:
        return {
            "ok": False,
            "error": "no_cleanup_categories_selected",
            "dry_run": True,
            "categories": [],
            "candidate_count": 0,
            "total_size_bytes": 0,
            "total_size": "0 B",
            "candidates": [],
            "truncated": False,
        }
    candidates = _local_cleanup_candidates(selected)
    total = sum(int(item.get("size_bytes") or 0) for item in candidates)
    return {
        "ok": True,
        "dry_run": True,
        "categories": selected,
        "candidate_count": len(candidates),
        "total_size_bytes": total,
        "total_size": _format_bytes(total),
        "candidates": candidates[:80],
        "truncated": len(candidates) > 80,
    }


def _delete_local_cleanup_categories(categories: Iterable[str]) -> Dict[str, object]:
    selected = [item for item in categories if item in LOCAL_CLEANUP_CATEGORIES]
    if not selected:
        return {
            "ok": False,
            "error": "no_cleanup_categories_selected",
            "dry_run": False,
            "categories": [],
            "deleted_count": 0,
            "error_count": 0,
            "total_size_bytes": 0,
            "total_size": "0 B",
            "deleted": [],
            "errors": [],
            "truncated": False,
        }
    candidates = _local_cleanup_candidates(selected)
    deleted: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    for item in candidates:
        path = Path(str(item.get("path") or ""))
        if not _safe_local_cleanup_path(path):
            errors.append({"path": str(path), "error": "unsafe_path"})
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            deleted.append(item)
        except Exception as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    total = sum(int(item.get("size_bytes") or 0) for item in deleted)
    return {
        "ok": not errors,
        "dry_run": False,
        "categories": selected,
        "deleted_count": len(deleted),
        "error_count": len(errors),
        "total_size_bytes": total,
        "total_size": _format_bytes(total),
        "deleted": deleted[:80],
        "errors": errors[:20],
        "truncated": len(deleted) > 80 or len(errors) > 20,
    }


def _display_remote_workdir(remote: Dict[str, object], config: Dict[str, object]) -> str:
    env = remote.get("remote_execution_env") if isinstance(remote.get("remote_execution_env"), dict) else {}
    workdir = str(env.get("remote_workdir") or remote.get("remote_workdir") or "")
    remote_root = str(remote.get("remote_root") or "")
    remote_task_dir = str(remote.get("remote_task_dir") or "")
    if remote_task_dir and workdir == remote_task_dir:
        workdir = ""
    if remote_root and workdir.startswith(remote_root.rstrip("/") + "/"):
        workdir = ""
    return workdir or str(config.get("remote_workdir") or DEFAULT_REMOTE_WORKDIR)


def _remote_task_counts(tasks: Dict[str, object]) -> Dict[tuple[str, str], Dict[str, int]]:
    counts: Dict[tuple[str, str], Dict[str, int]] = {}
    for record in tasks.values():
        if not isinstance(record, dict):
            continue
        remote = record.get("remote") if isinstance(record.get("remote"), dict) else {}
        if not remote:
            continue
        key = _remote_workplace_key(remote)
        item = counts.setdefault(key, {"total": 0, "active": 0})
        item["total"] += 1
        if str(record.get("status") or "") in {"running", "launching", "launch_unknown"}:
            item["active"] += 1
    return counts


def _remote_history_workplaces(config: Dict[str, object], *, include_hidden: bool = False) -> List[Dict[str, object]]:
    tasks = load_registry().get("tasks", {})
    hidden_history = _hidden_history_workplaces(config)
    counts = _remote_task_counts(tasks if isinstance(tasks, dict) else {})
    history: List[Dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in (tasks.values() if isinstance(tasks, dict) else []):
        if not isinstance(record, dict):
            continue
        remote = record.get("remote") if isinstance(record.get("remote"), dict) else {}
        if not remote:
            continue
        host = str(remote.get("host") or "")
        remote_root = str(remote.get("remote_root") or "")
        key = (host, remote_root)
        if not key[0] or not key[1] or key in seen:
            continue
        hidden = _workplace_config_key(host, remote_root) in hidden_history
        if hidden and not include_hidden:
            continue
        seen.add(key)
        item = {
            "label": str(remote_root or host or "Recorded workplace").rstrip("/").split("/")[-1],
            "host": host,
            "remote_workdir": _display_remote_workdir(remote, config),
            "remote_root": remote_root,
            "allowed_gpu_ids": str(config.get("allowed_gpu_ids") or DEFAULT_ALLOWED_GPUS),
            "cuda_mapping_note": "",
            "from_registry": "1",
            "hidden": "1" if hidden else "",
            "counts": counts.get(key, {"total": 0, "active": 0}),
        }
        history.append(item)
    return history


def _remote_workplaces(config: Dict[str, object]) -> List[Dict[str, object]]:
    workplaces: List[Dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    def add(item: Dict[str, object]) -> None:
        key = _remote_workplace_key(item)
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        workplaces.append(dict(item))

    current = _current_remote_preset(config, label="Active workplace")
    current["active"] = "1"
    add(current)
    for preset in _remote_presets(config):
        add(preset)
    tasks = load_registry().get("tasks", {})
    for workplace in _remote_history_workplaces(config):
        add(workplace)
    counts = _remote_task_counts(tasks if isinstance(tasks, dict) else {})
    for workplace in workplaces:
        workplace["counts"] = counts.get(_remote_workplace_key(workplace), {"total": 0, "active": 0})
    return workplaces


def _records_by_status() -> Dict[str, List[Dict[str, object]]]:
    tasks = load_registry().get("tasks", {})
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for task_id, record in tasks.items():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "unknown")
        grouped.setdefault(status, []).append(record)
    for items in grouped.values():
        items.sort(key=lambda item: str(item.get("created_at") or item.get("started_at") or ""))
    return grouped


def _parse_logged_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _remote_call_usage() -> Dict[str, object]:
    path = state_dir() / "remote_call_log.jsonl"
    now = datetime.now().astimezone()
    windows = {5: 0, 10: 0, 30: 0, 60: 0}
    by_kind_60: Dict[str, int] = {}
    recent_failures: List[Dict[str, object]] = []
    total = 0
    last_ts = None
    if not path.exists():
        return {
            "log": str(path),
            "total": 0,
            "windows": windows,
            "by_kind_60": by_kind_60,
            "recent_failures": [],
            "consecutive_failures": 0,
            "mode": "normal",
            "last_ts": None,
        }
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        total += 1
        ts = _parse_logged_time(item.get("ts"))
        if not ts:
            continue
        last_ts = item.get("ts")
        age_minutes = (now - ts.astimezone(now.tzinfo)).total_seconds() / 60
        for minutes in windows:
            if age_minutes <= minutes:
                windows[minutes] += 1
        if age_minutes <= 60:
            kind = str(item.get("kind") or "unknown")
            by_kind_60[kind] = by_kind_60.get(kind, 0) + 1
        if bool(item.get("ok")) is False or int(item.get("returncode", 0) or 0) != 0:
            recent_failures.append(item)
    consecutive_failures = 0
    for item in reversed(lines):
        try:
            payload = json.loads(item)
        except json.JSONDecodeError:
            continue
        if "returncode" not in payload and "ok" not in payload:
            continue
        if bool(payload.get("ok")) is False or int(payload.get("returncode", 0) or 0) != 0:
            consecutive_failures += 1
            continue
        break
    if windows[5] > 40 or windows[60] > 120 or consecutive_failures >= 3:
        mode = "danger"
    elif windows[5] > 20 or windows[60] >= 60:
        mode = "attention"
    else:
        mode = "normal"
    return {
        "log": str(path),
        "total": total,
        "windows": windows,
        "by_kind_60": dict(sorted(by_kind_60.items(), key=lambda kv: (-kv[1], kv[0]))),
        "recent_failures": recent_failures[-10:],
        "consecutive_failures": consecutive_failures,
        "mode": mode,
        "last_ts": last_ts,
    }


def _relative_age_text(value: object) -> str:
    ts = _parse_logged_time(value)
    if not ts:
        return "last: -"
    now = datetime.now().astimezone()
    seconds = max(0, int((now - ts.astimezone(now.tzinfo)).total_seconds()))
    if seconds < 90:
        return f"last: {seconds}s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"last: {minutes}m ago"
    hours = minutes // 60
    return f"last: {hours}h ago"


def _remote_count_class(minutes: int, count: int) -> str:
    if minutes == 5:
        if count > 40:
            return "failed"
        if count > 20:
            return "partial"
        return "running"
    if minutes == 60:
        if count > 120:
            return "failed"
        if count >= 60:
            return "partial"
        return "running"
    if minutes == 30:
        if count > 60:
            return "failed"
        if count >= 30:
            return "partial"
        return "running"
    return "running"


def _latest_report_paths(task_id: str) -> Dict[str, Path]:
    latest = result_latest_dir(task_id)
    return {
        "report.md": latest / "report.md",
        "worker_notes.md": latest / "worker_notes.md",
        "result.yaml": latest / "result.yaml",
        "metrics.json": latest / "metrics.json",
        "stdout.log": latest / "stdout.log",
        "stderr.log": latest / "stderr.log",
    }


def _short_summary(record: Dict[str, object], limit: int = 180) -> str:
    text = str(record.get("summary_short") or "")
    if not text:
        supervision = record.get("last_supervision")
        if isinstance(supervision, dict):
            decision = supervision.get("decision")
            if isinstance(decision, dict):
                text = str(decision.get("summary") or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _task_meta(task_id: str, status: object = None) -> Dict[str, object]:
    statuses = [str(status or ""), "running", "pending", "completed", "partial", "failed"]
    seen = set()
    for item in statuses:
        if not item or item in seen:
            continue
        seen.add(item)
        path = task_file(item, task_id)
        if not path.exists():
            continue
        try:
            return load_task(path).raw_front_matter
        except Exception:
            return {}
    return {}


def _hidden_completed_path() -> Path:
    return state_dir() / "dashboard_hidden_completed.json"


def _hidden_attention_path() -> Path:
    return state_dir() / "dashboard_hidden_attention.json"


def _dashboard_flash_path() -> Path:
    return state_dir() / "dashboard_flash.json"


def _advisor_feedback_path() -> Path:
    return state_dir() / "scheduler_advisor_feedback.md"


def _why_ai_dir() -> Path:
    return state_dir() / "why_ai"


def _result_ai_dir() -> Path:
    return state_dir() / "result_ai"


def _safe_task_filename(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._") or "task"


def _why_ai_path(task_id: str) -> Path:
    return _why_ai_dir() / f"{_safe_task_filename(task_id)}.md"


def _result_ai_path(task_id: str) -> Path:
    return _result_ai_dir() / f"{_safe_task_filename(task_id)}.md"


def _advisor_feedback() -> str:
    path = _advisor_feedback_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _save_advisor_feedback(text: str) -> None:
    path = _advisor_feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")


def _read_why_ai(task_id: str) -> str:
    return _read_text(_why_ai_path(task_id), limit=10000)


def _save_why_ai(task_id: str, text: str) -> Path:
    path = _why_ai_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    return path


def _read_result_ai(task_id: str) -> str:
    return _read_text(_result_ai_path(task_id), limit=14000)


def _save_result_ai(task_id: str, text: str) -> Path:
    path = _result_ai_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    return path


def _hidden_completed() -> set[str]:
    path = _hidden_completed_path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload}


def _write_hidden_completed(task_ids: Iterable[str]) -> None:
    path = _hidden_completed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(set(task_ids)), indent=2), encoding="utf-8")


def _hidden_attention() -> set[str]:
    path = _hidden_attention_path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload}


def _write_hidden_attention(task_ids: Iterable[str]) -> None:
    path = _hidden_attention_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(set(task_ids)), indent=2), encoding="utf-8")


def _write_dashboard_flash(payload: Dict[str, object]) -> None:
    path = _dashboard_flash_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


def _pop_dashboard_flash() -> Optional[Dict[str, object]]:
    path = _dashboard_flash_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _gpu_status(gpu: Dict[str, object], min_free_gb: float) -> str:
    free = float(gpu.get("free_gb") or 0)
    own = int(gpu.get("own_compute_app_count") or 0)
    external = int(gpu.get("external_compute_app_count") or 0)
    util = float(gpu.get("utilization_pct") or 0)
    if free < min_free_gb:
        return "low memory"
    if external and own:
        return "shared"
    if external:
        return "external occupied"
    if own:
        return "own running"
    if util >= 80:
        return "busy"
    return "usable"


def _human_skip_reason(item: Dict[str, object]) -> str:
    reason = str(item.get("reason") or "")
    if reason == "target_workplace_mismatch":
        mismatches = item.get("mismatches") if isinstance(item.get("mismatches"), list) else []
        details: List[str] = []
        for mismatch in mismatches:
            if not isinstance(mismatch, dict):
                continue
            field = str(mismatch.get("field") or "").strip()
            expected = str(mismatch.get("expected") or "").strip()
            active = str(mismatch.get("active") or "").strip()
            if field:
                details.append(f"{field}: expected {expected or '-'}, active {active or '-'}")
        if details:
            return "target workplace mismatch; " + "; ".join(details)
        return "target workplace mismatch; switch active workplace or edit task target_workplace"
    if reason == "dependencies_not_ready":
        return "blocked by unfinished depends_on / required artifacts"
    if reason == "source_code_change_serialized":
        running = item.get("running_source_change_tasks") or []
        if running:
            return "waiting because source-changing task is running: " + ", ".join(str(x) for x in running)
        return "waiting for source-code-change lock"
    if reason == "gpu_capacity_full":
        return "waiting for GPU capacity"
    if reason == "cpu_capacity_full":
        return "waiting for CPU capacity"
    if reason == "prepare_first_policy":
        return "waiting because a prepare task must run first"
    if reason == "max_running_total_reached":
        return "waiting because max running task limit is reached"
    if reason == "no_gpu_available":
        return "waiting for allowed GPU with enough free memory"
    return reason.replace("_", " ") or "waiting"


def _skip_attention_type(item: Dict[str, object]) -> tuple[str, str]:
    task_id = str(item.get("task_id") or "")
    reason = str(item.get("reason") or "")
    queue_item = None
    try:
        for candidate in lab.queue():
            if str(candidate.get("task_id") or "") == task_id:
                queue_item = candidate
                break
    except Exception:
        queue_item = None
    dep = queue_item.get("dependency_state") if isinstance(queue_item, dict) and isinstance(queue_item.get("dependency_state"), dict) else {}
    incomplete = dep.get("incomplete", []) if isinstance(dep, dict) else []
    missing = dep.get("missing", []) if isinstance(dep, dict) else []
    missing_artifacts = dep.get("missing_artifacts", []) if isinstance(dep, dict) else []
    incomplete_statuses = {
        str(entry.get("status") or "")
        for entry in incomplete
        if isinstance(entry, dict)
    }
    if reason == "start_budget_full":
        return "waiting_slot", "ready; waiting for this scheduler pass start budget / launch slot"
    if reason == "max_running_total_reached":
        return "waiting_capacity", "ready; max running task limit reached"
    if reason == "gpu_capacity_full":
        return "waiting_gpu_capacity", "ready; max running GPU task limit reached"
    if reason == "cpu_capacity_full":
        return "waiting_cpu_capacity", "ready; max running CPU task limit reached"
    if reason == "no_gpu_available":
        return "waiting_gpu_memory", "ready; waiting for allowed GPU with enough free memory"
    if reason == "source_code_change_serialized":
        return "waiting_source_lock", _human_skip_reason(item)
    if reason == "target_workplace_mismatch":
        return "blocked_workplace", _human_skip_reason(item)
    if reason == "prepare_first_policy":
        return "waiting_prepare_first", _human_skip_reason(item)
    if reason == "dependencies_not_ready":
        if missing:
            return "blocked_missing_parent", "dependency task is missing from registry/queue"
        if missing_artifacts:
            if incomplete_statuses == {"running"}:
                return "waiting_upstream_running", "upstream is still running; required artifacts are not ready yet"
            if "partial" in incomplete_statuses:
                return "blocked_partial_parent", "upstream is partial; may need use-partial decision or rerun"
            if "failed" in incomplete_statuses:
                return "blocked_failed_parent", "upstream failed; needs repair/rerun before this can start"
            return "waiting_artifacts", "required upstream artifacts are missing"
        if incomplete:
            if incomplete_statuses == {"running"}:
                return "waiting_upstream_running", "upstream is still running"
            if "partial" in incomplete_statuses:
                return "blocked_partial_parent", "upstream partial blocks completed-only dependency"
            if "failed" in incomplete_statuses:
                return "blocked_failed_parent", "upstream failed"
            return "waiting_deps", "dependency exists but is not completed"
        return "waiting_deps", "dependency condition is not satisfied"
    return "waiting_policy", _human_skip_reason(item)


def _queue_dependency_graph() -> tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, object]]:
    items = lab.queue()
    pending_ids = {str(item.get("task_id") or "") for item in items}
    deps_by_task: Dict[str, List[str]] = {}
    direct_blocked_by: Dict[str, List[str]] = {}
    queue_by_id: Dict[str, object] = {}
    for item in items:
        task_id = str(item.get("task_id") or "")
        if not task_id:
            continue
        queue_by_id[task_id] = item
        dep = item.get("dependency_state") if isinstance(item.get("dependency_state"), dict) else {}
        deps_by_task[task_id] = [str(dep_id) for dep_id in dep.get("depends_on", []) or []]
        blockers: List[str] = []
        for key in ("incomplete", "missing"):
            for entry in dep.get(key, []) or []:
                blockers.append(str(entry.get("task_id", entry)) if isinstance(entry, dict) else str(entry))
        for entry in dep.get("missing_artifacts", []) or []:
            blockers.append(str(entry))
        for blocker in blockers:
            direct_blocked_by.setdefault(blocker, []).append(task_id)
    downstream_by_task: Dict[str, List[str]] = {}
    for blocker in set(direct_blocked_by) | pending_ids:
        seen: set[str] = set()
        stack = list(direct_blocked_by.get(blocker, []))
        while stack:
            child = stack.pop()
            if child in seen:
                continue
            seen.add(child)
            for maybe_child, deps in deps_by_task.items():
                if child in deps:
                    stack.append(maybe_child)
        downstream_by_task[blocker] = sorted(seen)
    return direct_blocked_by, downstream_by_task, queue_by_id


def _render_blocker_priority() -> str:
    try:
        direct, downstream, _ = _queue_dependency_graph()
    except Exception as exc:
        return f'<div class="panel"><h2>Blocker Priority</h2><div class="inner"><pre>{_esc(exc)}</pre></div></div>'
    registry = load_registry().get("tasks", {})
    rows = []
    for blocker, downstream_tasks in sorted(downstream.items(), key=lambda item: (-len(item[1]), item[0])):
        if not downstream_tasks:
            continue
        record = registry.get(blocker, {}) if isinstance(registry.get(blocker), dict) else {}
        status = str(record.get("status") or "not registered")
        if status == "completed":
            continue
        latest = str(record.get("latest_run_id") or "")
        direct_count = len(set(direct.get(blocker, [])))
        rows.append(
            "<tr>"
            f"<td><a href=\"/task?id={urllib.parse.quote(blocker)}\">{_esc(blocker)}</a></td>"
            f"<td><span class=\"{_esc(status)}\">{_esc(status)}</span></td>"
            f"<td>{_esc(direct_count)}</td>"
            f"<td>{_esc(len(downstream_tasks))}</td>"
            f"<td class=\"muted\"><div class=\"truncate\">{_esc(', '.join(downstream_tasks))}<br>{_esc(latest)}</div></td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5" class="muted">No unresolved dependency blockers.</td></tr>')
    return (
        '<div class="panel"><h2>Blocker Priority</h2><div class="inner scroll queue-scroll">'
        '<p class="muted">These are not running tasks; they are upstream tasks blocking pending work.</p>'
        '<table><tr><th>blocker</th><th>status</th><th>direct</th><th>total downstream</th><th>blocked tasks / run</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _render_unblock_planner(plan: Optional[Dict[str, object]]) -> str:
    report = plan.get("dependency_reconcile") if isinstance(plan, dict) else None
    if not isinstance(report, dict):
        try:
            report = lab.dependency_reconcile(apply_safe=False)
        except Exception as exc:
            return f'<div class="panel"><h2>Blocked / Unblock Planner</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'
    promoted = report.get("promoted", []) or []
    unblockable = report.get("newly_unblockable", []) or []
    decisions = report.get("needs_user_decision", []) or []
    blocked = report.get("still_blocked", []) or []
    rows = []
    for kind, items in (
        ("ready_front", promoted or unblockable),
        ("needs choice", decisions),
        ("still blocked", blocked),
    ):
        for item in items[:12]:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "")
            reason = str(item.get("reason") or item.get("ready_reason") or "")
            if not reason and item.get("input_requirements"):
                reqs = item.get("input_requirements") or []
                reason = "; ".join(str(req.get("reason") or "") for req in reqs if isinstance(req, dict))[:240]
            rows.append(
                "<tr>"
                f"<td><span class=\"badge\">{_esc(kind)}</span></td>"
                f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
                f"<td>{_esc(reason or 'artifact dependency diagnosis')}</td>"
                "</tr>"
            )
    if not rows:
        rows.append('<tr><td colspan="3" class="muted">No artifact-dependency unblock actions are available.</td></tr>')
    return (
        '<div class="panel"><h2>Blocked / Unblock Planner</h2><div class="inner scroll queue-scroll">'
        '<p class="muted">This only promotes tasks with explicit input_requirements and valid parent artifacts. Partial parents stay partial.</p>'
        '<table><tr><th>lane</th><th>task</th><th>reason</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _render_scheduling_decisions(config: Dict[str, object], plan: Optional[Dict[str, object]]) -> str:
    if not isinstance(plan, dict):
        return '<div class="panel"><h2>Scheduling Decisions</h2><div class="inner muted">No dry-run plan is available.</div></div>'
    limits = plan.get("limits") if isinstance(plan.get("limits"), dict) else {}
    capacity = plan.get("capacity") if isinstance(plan.get("capacity"), dict) else {}
    planned = [item for item in (plan.get("plan", []) or []) if isinstance(item, dict)]
    skipped = [item for item in (plan.get("skipped", []) or []) if isinstance(item, dict)]
    _, downstream, _ = _queue_dependency_graph()
    registry = load_registry().get("tasks", {})

    policy_rows = [
        ("Start budget", f"{len(planned)} planned / max {limits.get('max_starts', '-')}; running total {capacity.get('total', '-')}/{limits.get('max_running_total', '-')}"),
        ("GPU slots", f"running {capacity.get('gpu', '-')}/{limits.get('max_running_gpu', '-')}; max per GPU {limits.get('max_tasks_per_gpu', '-')}"),
        ("GPU rule", f"free >= {limits.get('min_free_gpu_mem_gb', config.get('min_free_gpu_mem_gb', 10))}GB; external-process cards can take one task only"),
        ("Launch precheck", f"recheck selected GPU before starting; cache max {limits.get('launch_gpu_precheck_max_age_seconds', '-')}s"),
        ("Prepare-first", "GPU runs wait while a ready prepare/code task exists" if plan.get("ready_prepare_exists") else "no ready prepare task is blocking GPU runs"),
        ("Source changes", "serialized; one source-changing task at a time"),
    ]
    policy_html = "".join(
        f'<div class="health-card"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div></div>'
        for label, value in policy_rows
    )

    decision_rows = []
    if planned:
        for item in planned[:6]:
            why = [
                f"stage={item.get('execution_stage')}",
                f"resource={item.get('resource_hint')}",
            ]
            if item.get("selected_gpu_id"):
                why.append(f"gpu={item.get('selected_gpu_id')}")
            if item.get("risk_flags"):
                why.append("risk=" + ",".join(str(flag) for flag in item.get("risk_flags") or []))
            decision_rows.append(
                "<tr>"
                f'<td><span class="running">will start</span></td>'
                f'<td><a href="/why?id={urllib.parse.quote(str(item.get("task_id") or ""))}">{_esc(item.get("task_id"))}</a></td>'
                f"<td>{_esc('; '.join(why))}</td>"
                "</tr>"
            )
    else:
        reason_counts: Dict[str, int] = {}
        for item in skipped:
            reason = str(item.get("reason") or "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reason_text = ", ".join(
            f"{reason.replace('_', ' ')}: {count}"
            for reason, count in sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        )
        decision_rows.append(
            '<tr><td><span class="partial">no launch</span></td>'
            f"<td>this cycle</td><td>{_esc(reason_text or 'no pending runnable task')}</td></tr>"
        )

    blocker_rows = []
    for blocker, downstream_tasks in sorted(downstream.items(), key=lambda item: (-len(item[1]), item[0]))[:5]:
        record = registry.get(blocker, {}) if isinstance(registry.get(blocker), dict) else {}
        status = str(record.get("status") or "not registered")
        if status == "completed" or not downstream_tasks:
            continue
        blocker_rows.append(
            "<tr>"
            f'<td><a href="/why?id={urllib.parse.quote(blocker)}">{_esc(blocker)}</a></td>'
            f'<td><span class="{_esc(status)}">{_esc(status)}</span></td>'
            f"<td>{_esc(len(downstream_tasks))}</td>"
            f'<td class="muted"><div class="truncate">{_esc(", ".join(downstream_tasks[:6]))}</div></td>'
            "</tr>"
        )
    if not blocker_rows:
        blocker_rows.append('<tr><td colspan="4" class="muted">No active dependency blocker is influencing the schedule.</td></tr>')

    return (
        '<div class="panel"><h2>Scheduling Decisions <span class="badge">why this order</span></h2>'
        '<div class="inner scroll queue-scroll">'
        '<p class="headline">Visible scheduling logic for this dry-run.</p>'
        '<div class="health-grid">'
        + policy_html
        + "</div>"
        '<h3>Current Decision</h3>'
        '<table><tr><th>decision</th><th>task / cycle</th><th>reason</th></tr>'
        + "".join(decision_rows)
        + "</table>"
        '<h3>Biggest Blockers</h3>'
        '<table><tr><th>blocker</th><th>status</th><th>blocked count</th><th>affected tasks</th></tr>'
        + "".join(blocker_rows)
        + "</table>"
        '<p class="muted">This is a dry-run view: it explains the scheduler order before any launch. Apply-time still revalidates remote state, GPU, dependencies, and locks.</p>'
        "</div></div>"
    )


def _run_health(record: Dict[str, object]) -> str:
    status = str(record.get("status") or "")
    remote_state = str(record.get("remote_state") or "")
    if status == "launching" and remote_state == "running":
        return "remote running, local status needs sync"
    if status == "launching":
        return "launching, waiting for runner heartbeat"
    if status == "launch_unknown":
        return "launch unknown, verify remote"
    if status == "launch_failed":
        remote_run = record.get("remote_run") if isinstance(record.get("remote_run"), dict) else {}
        run = remote_run.get("run") if isinstance(remote_run.get("run"), dict) else {}
        stderr = str(run.get("stderr") or "").strip()
        return "launch failed" + (f": {stderr[:90]}" if stderr else "")
    if status != "running":
        return str(record.get("status") or "unknown")
    supervision = record.get("last_supervision")
    status_text = ""
    decision_action = ""
    reason_code = ""
    decision_summary = ""
    checked_text = ""
    if isinstance(supervision, dict):
        checked_text = _relative_age_text(supervision.get("checked_at")).replace("last: ", "checked ")
        decision = supervision.get("decision")
        if isinstance(decision, dict):
            decision_action = str(decision.get("action") or "")
            reason_code = str(decision.get("reason_code") or "")
            decision_summary = str(decision.get("summary") or decision.get("reason") or "")
        status = supervision.get("status")
        if isinstance(status, dict):
            remote_status = status.get("status")
            if isinstance(remote_status, dict):
                status_text = str(remote_status.get("stdout") or "")
    if reason_code.startswith("pid_dead"):
        detail = reason_code.replace("_", " ")
        if decision_summary:
            detail += f": {decision_summary[:100]}"
        return detail
    if reason_code == "remote_status_unavailable":
        detail = "remote status unavailable"
        if decision_summary:
            detail += f": {decision_summary[:100]}"
        return detail
    if reason_code in {"gpu_oom_detected", "result_contract_invalid", "runtime_budget_exceeded"}:
        return reason_code.replace("_", " ")
    if reason_code == "worker_active_progressing":
        return "running; " + (checked_text or "recently checked")
    if "run_state:\nfinished" in status_text or decision_action == "collect":
        return "finished remote, needs collect"
    if "running:\nno" in status_text:
        return "zombie registry"
    match = re.search(r"heartbeat_age_seconds:\s*\n?(\d+)", status_text)
    if match:
        age = int(match.group(1))
        if age > 1800:
            return f"stale heartbeat {age // 60}m"
        if age > 600:
            return f"quiet heartbeat {age // 60}m"
    return "running" + (f"; {checked_text}" if checked_text else "")


def _last_remote_check_text(record: Dict[str, object]) -> str:
    supervision = record.get("last_supervision") if isinstance(record.get("last_supervision"), dict) else {}
    checked = supervision.get("checked_at") if isinstance(supervision, dict) else None
    if checked:
        return _relative_age_text(checked).replace("last: ", "")
    remote_seen = record.get("last_remote_seen_at")
    if remote_seen:
        return _relative_age_text(remote_seen).replace("last: ", "remote ")
    return "-"


def _result_summary(task_id: str) -> str:
    metrics_path = result_latest_dir(task_id) / "metrics.json"
    if not metrics_path.exists():
        result_path = result_latest_dir(task_id) / "result.yaml"
        if result_path.exists():
            text = _read_text(result_path, limit=3000)
            match = re.search(r"summary_short:\s*>?\s*(.+)", text)
            return match.group(1).strip()[:140] if match else "result.yaml"
        return "-"
    try:
        payload = json.loads(metrics_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return "metrics.json parse issue"
    if not isinstance(payload, dict):
        return "metrics.json"
    rows = payload.get("per_checkpoint_dataset_metrics")
    if isinstance(rows, list):
        best = None
        for row in rows:
            if not isinstance(row, dict) or row.get("video_auc") is None:
                continue
            if best is None or float(row.get("video_auc") or 0) > float(best.get("video_auc") or 0):
                best = row
        if best:
            return f"{best.get('dataset', 'dataset')} best {best.get('checkpoint')}: AUC {float(best.get('video_auc') or 0):.4f}"
    decision = payload.get("decision") or payload.get("decisions")
    if isinstance(decision, dict):
        bits = [f"{key}={value}" for key, value in list(decision.items())[:3]]
        return "; ".join(bits)
    for key in ("primary_metric", "status", "summary"):
        if payload.get(key):
            return f"{key}: {payload.get(key)}"
    return "metrics.json"


def _suggested_action(record: Dict[str, object]) -> str:
    status = str(record.get("status") or "")
    health = _run_health(record)
    if status == "launching":
        return "verify launch"
    if status == "launch_unknown":
        return "verify launch"
    if status == "launch_failed":
        return "retry or reset pending"
    if "needs collect" in health:
        return "supervise / collect"
    if "remote status unavailable" in health:
        return "check SSH / wait for next status"
    if "pid dead" in health or "result incomplete" in health:
        return "verify remote / collect or repair"
    if "zombie" in health or "stale" in health:
        return "check remote status"
    if status == "partial":
        return "inspect report"
    if status == "failed":
        return "check stderr"
    if status == "completed":
        return "hide or compare"
    if status == "running":
        return "monitor"
    return "-"


def _requeue_partial_form(task_id: str) -> str:
    return (
        f'<div class="actions"><a class="button" href="/why?id={urllib.parse.quote(task_id)}">Inspect Evidence</a>'
        '<form method="post" action="/action" '
        f'onsubmit="return confirm(\'Create requeue request for partial task { _esc(task_id) }? This will not execute until the CLI processor applies it with --allow-dangerous.\')">'
        '<input type="hidden" name="action" value="requeue_partial">'
        f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
        '<button class="warn">Request Requeue</button>'
        "</form></div>"
    )


def _maintenance_action_allowed() -> bool:
    mode = lab.load_maintenance_mode()
    return bool(not mode.get("enabled") or mode.get("allow_action_requests"))


def _maintenance_banner() -> str:
    mode = lab.load_maintenance_mode()
    if not mode.get("enabled"):
        return ""
    return (
        '<div class="notice maintenance-banner">'
        '<div class="headline">Maintenance Mode</div>'
        f'<div>{_esc(mode.get("reason") or "Dashboard is read-only.")}</div>'
        '<div class="muted">State-changing dashboard actions are disabled. Scheduler launch is blocked unless explicitly allowed.</div>'
        "</div>"
    )


def _dashboard_action_request(action_type: str, *, task_id: Optional[str] = None, payload: Optional[Dict[str, object]] = None, reason: str = "", safe_level: str = "safe_after_revalidation", expected_task_status: Optional[str] = None) -> Dict[str, object]:
    return lab.create_action_request(
        action_type,
        task_id=task_id,
        payload=payload or {},
        reason=reason,
        actor="user",
        source="dashboard",
        safe_level=safe_level,
        expected_task_status=expected_task_status,
        requires_scheduler_validation=True,
    )


def _render_action_request_history() -> str:
    try:
        payload = lab.list_action_requests()
    except Exception as exc:
        return f'<div class="panel"><h2>Action Requests</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'
    requests = payload.get("requests", []) if isinstance(payload, dict) else []
    rows = []
    order = {"pending": 0, "processing": 1, "failed": 2, "rejected": 3, "done": 4}
    items = sorted(
        [item for item in requests if isinstance(item, dict)],
        key=lambda item: (order.get(str(item.get("_status_dir") or ""), 9), str(item.get("created_at") or item.get("processed_at") or ""), str(item.get("action_id") or "")),
    )
    for item in items[:30]:
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        rows.append(
            "<tr>"
            f"<td><span class=\"badge\">{_esc(item.get('_status_dir'))}</span><br>{_esc(item.get('action_id'))}</td>"
            f"<td>{_esc(item.get('action_type'))}<br><span class=\"muted\">{_esc(item.get('created_at'))}</span></td>"
            f"<td>{_esc(item.get('task_id') or '-')}</td>"
            f"<td>{_esc(response.get('decision') or item.get('safe_level') or '')}<br><span class=\"muted\">{_esc(response.get('message') or item.get('reason') or '')}</span></td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="muted">No action requests yet.</td></tr>')
    counts = payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}
    count_text = " · ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
    return (
        '<div class="panel"><h2>Action Requests</h2><div class="inner scroll queue-scroll">'
        f'<p class="muted">{_esc(count_text or "No queued action requests.")} Dashboard creates requests; CLI processor validates and applies them.</p>'
        '<table><tr><th>request</th><th>action</th><th>task</th><th>decision / message</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _disabled_button(label: str, reason: str, css_class: str = "") -> str:
    klass = f' class="{_esc(css_class)}"' if css_class else ""
    return f'<button{klass} disabled title="{_esc(reason)}">{_esc(label)}</button>'


def _render_page(title: str, body: str, refresh: bool = True) -> bytes:
    refresh_tag = '<meta http-equiv="refresh" content="30">' if refresh else ""
    maintenance_actions_disabled = "true" if not _maintenance_action_allowed() else "false"
    maintenance_banner = _maintenance_banner()
    action_flash = _pop_dashboard_flash()
    action_flash_json = json.dumps(action_flash, ensure_ascii=False, default=str) if action_flash else "null"
    heartbeat_script = """
  <script>
    (function () {
      var maintenanceActionsDisabled = __MAINTENANCE_ACTIONS_DISABLED__;
      function ping() {
        try {
          if (navigator.sendBeacon) {
            navigator.sendBeacon('/client-ping');
            return;
          }
        } catch (e) {}
        try { fetch('/client-ping', {method: 'POST', keepalive: true}).catch(function () {}); } catch (e) {}
      }
      ping();
      window.setInterval(ping, 5000);
      window.addEventListener('pagehide', ping);
      window.addEventListener('beforeunload', ping);
      try { if ('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch (e) {}

      function scrollStorageKey() {
        return 'solution-agent-dashboard-scroll:' + window.location.pathname + window.location.search;
      }

      function saveScrollPosition() {
        try {
          sessionStorage.setItem(scrollStorageKey(), String(window.scrollY || window.pageYOffset || 0));
        } catch (e) {}
      }

      function restoreScrollPosition() {
        try {
          var value = sessionStorage.getItem(scrollStorageKey());
          if (value === null) return;
          var y = parseInt(value, 10);
          if (!isFinite(y) || y <= 0) return;
          window.requestAnimationFrame(function () {
            window.scrollTo(0, y);
          });
        } catch (e) {}
      }

      window.addEventListener('beforeunload', saveScrollPosition);
      window.addEventListener('pagehide', saveScrollPosition);
      window.addEventListener('DOMContentLoaded', restoreScrollPosition);

      function refreshGpuPanel() {
        var target = document.getElementById('gpu-live-body');
        if (!target) return;
        fetch('/api/gpu', {cache: 'no-store'})
          .then(function (response) {
            if (!response.ok) throw new Error('gpu api ' + response.status);
            return response.text();
          })
          .then(function (html) { target.innerHTML = html; })
          .catch(function () {
            var note = target.querySelector('[data-gpu-live-note]');
            if (note) note.textContent = 'Live GPU refresh paused; use Refresh GPU Now.';
          });
      }
      window.setInterval(refreshGpuPanel, 5000);

      var actionLabels = {
        refresh_gpu: 'Refreshing...',
        remote_watch_start: 'Starting watcher...',
        remote_monitor_task: 'Monitoring...',
        scheduler_start: 'Starting scheduler...',
        supervise_once: 'Checking...',
        schedule_once: 'Scheduling...',
        show_completed: 'Showing...',
        scheduler_stop: 'Stopping...',
        remote_watch_stop: 'Stopping watcher...',
        clear_completed: 'Clearing...',
        hide_attention_task: 'Hiding...',
        hide_attention_group: 'Hiding group...',
        bulk_hide_attention: 'Hiding selected...',
        bulk_hide_completed: 'Hiding selected...',
        show_attention: 'Restoring...',
        sync_profile: 'Syncing...',
        remote_stop: 'Stopping remote...',
        requeue_partial: 'Requeueing...',
        use_partial_continue_downstream: 'Queueing downstream...',
        requeue_partial_chain: 'Requeueing chain...',
        advisor_feedback: 'Saving...',
        explain_why_ai: 'Explaining...',
        summarize_result_ai: 'Summarizing...',
        local_init: 'Checking local machine...',
        remote_preflight: 'Checking remote...',
        workplace_init: 'Initializing workplace...',
        save_remote_config: 'Saving...',
          delete_remote_preset: 'Deleting...',
          hide_history_workplace: 'Forgetting...',
          show_history_workplace: 'Restoring...',
          bulk_history_workplaces: 'Applying...',
          show_hidden_history_workplaces: 'Restoring...',
          local_cleanup_plan: 'Planning cleanup...',
          local_cleanup_delete: 'Deleting local files...'
        };

      function maintenanceBlock(event) {
        if (!maintenanceActionsDisabled) return false;
        var form = event.target;
        if (!form || form.tagName !== 'FORM') return false;
        var actionPath = '';
        try { actionPath = new URL(form.action, window.location.href).pathname; } catch (e) {}
        if (form.method && form.method.toLowerCase() === 'post' && actionPath === '/action') {
          event.preventDefault();
          alert('Maintenance mode is enabled. Dashboard actions are read-only until maintenance is cleared.');
          return true;
        }
        return false;
      }

      function showActionModal(payload) {
        payload = payload || {};
        var existing = document.querySelector('.action-modal-backdrop');
        if (existing) existing.remove();
        var backdrop = document.createElement('div');
        backdrop.className = 'action-modal-backdrop';
        var card = document.createElement('div');
        card.className = 'action-modal-card ' + (payload.is_error ? 'error' : 'ok');
        var title = document.createElement('h2');
        title.textContent = payload.headline || (payload.is_error ? 'Action failed' : 'Action completed');
        var list = document.createElement('ul');
        var details = Array.isArray(payload.details) && payload.details.length ? payload.details : ['Done.'];
        details.forEach(function (line) {
          var item = document.createElement('li');
          item.textContent = String(line || '');
          list.appendChild(item);
        });
        var actions = document.createElement('div');
        actions.className = 'actions action-modal-actions';
        var ok = document.createElement('button');
        ok.type = 'button';
        ok.textContent = 'OK';
        ok.addEventListener('click', function () {
          backdrop.remove();
          if (payload.reload !== false) {
            saveScrollPosition();
            window.location.reload();
          }
        });
        actions.appendChild(ok);
        card.appendChild(title);
        card.appendChild(list);
        card.appendChild(actions);
        backdrop.appendChild(card);
        document.body.appendChild(backdrop);
        ok.focus();
      }

      var initialActionFlash = __ACTION_FLASH_JSON__;
      if (initialActionFlash) {
        window.addEventListener('DOMContentLoaded', function () {
          showActionModal(initialActionFlash);
        });
      }

      window.addEventListener('DOMContentLoaded', function () {
        if (!maintenanceActionsDisabled) return;
        document.querySelectorAll('form[action="/action"] button').forEach(function (button) {
          button.disabled = true;
          button.title = 'Disabled by maintenance mode';
        });
      });

      document.addEventListener('submit', function (event) {
        if (maintenanceBlock(event)) return;
        var form = event.target;
        if (!form || form.tagName !== 'FORM') return;
        if (event.defaultPrevented) return;
        if (form.dataset.submitting === '1') {
          event.preventDefault();
          return;
        }
        var button = event.submitter || form.querySelector('button, input[type="submit"]');
        if (!button) return;
        var actionInput = form.querySelector('input[name="action"]');
        var action = actionInput ? actionInput.value : '';
        var loadingText = form.dataset.loadingText || button.dataset.loadingText || actionLabels[action] || 'Working...';
        var actionPath = '';
        try { actionPath = new URL(form.action, window.location.href).pathname; } catch (e) {}
        var useAjax = form.method && form.method.toLowerCase() === 'post' && actionPath === '/action' && form.dataset.fullSubmit !== '1';
        var metaRefresh = document.querySelector('meta[http-equiv="refresh"]');
        if (useAjax) {
          event.preventDefault();
          if (metaRefresh) metaRefresh.remove();
        } else {
          saveScrollPosition();
        }

        function resetLoading() {
          form.dataset.submitting = '';
          form.classList.remove('is-submitting');
          var group = form.closest('.actions');
          if (group) {
            group.classList.remove('has-submitting');
            group.querySelectorAll('button').forEach(function (other) {
              other.disabled = false;
              other.classList.remove('quiet-disabled');
            });
          }
          button.classList.remove('is-loading');
          button.removeAttribute('aria-busy');
          if (button.dataset.originalText) {
            if (button.tagName === 'INPUT') button.value = button.dataset.originalText;
            else button.textContent = button.dataset.originalText;
          }
        }

        form.dataset.submitting = '1';
        window.requestAnimationFrame(function () {
          form.classList.add('is-submitting');
          var group = form.closest('.actions');
          if (group) group.classList.add('has-submitting');
          button.dataset.originalText = button.textContent || button.value || '';
          button.classList.add('is-loading');
          button.setAttribute('aria-busy', 'true');
          if (button.tagName === 'INPUT') button.value = loadingText;
          else button.textContent = loadingText;
          button.disabled = true;
          if (group) {
            group.querySelectorAll('button').forEach(function (other) {
              if (other !== button) {
                other.disabled = true;
                other.classList.add('quiet-disabled');
              }
            });
          }
        });
        if (useAjax) {
          fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            headers: {'X-Dashboard-Ajax': '1'}
          })
            .then(function (response) {
              return response.json().then(function (payload) {
                if (!response.ok) payload.is_error = true;
                return payload;
              });
            })
            .then(function (payload) {
              resetLoading();
              showActionModal(payload);
            })
            .catch(function (error) {
              resetLoading();
              showActionModal({
                is_error: true,
                headline: 'Action failed',
                details: [error && error.message ? error.message : String(error)]
              });
            });
        }
      });
    })();
  </script>
"""
    heartbeat_script = heartbeat_script.replace("__MAINTENANCE_ACTIONS_DISABLED__", maintenance_actions_disabled)
    heartbeat_script = heartbeat_script.replace("__ACTION_FLASH_JSON__", action_flash_json)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_tag}
  <title>{_esc(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #101114;
      --panel: #181b20;
      --panel2: #20242b;
      --text: #f2f4f8;
      --muted: #a8b0bd;
      --line: #303640;
      --green: #70d188;
      --amber: #e4c168;
      --red: #ef7f7f;
      --blue: #8ab4ff;
      --button: #2f68a9;
      --button-hover: #3979c2;
      --button-warn: #a76b2c;
      --button-warn-hover: #bf7c33;
      --button-danger: #a43c45;
      --button-danger-hover: #be4650;
      --focus: rgba(138,180,255,.42);
    }}
    body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: var(--bg); color: var(--text); }}
    header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--line); background: #15171b; position: sticky; top: 0; z-index: 2; }}
    a {{ color: var(--blue); text-decoration: none; }}
    main {{ padding: 12px; display: flex; flex-direction: column; gap: 12px; min-height: calc(100vh - 54px); box-sizing: border-box; overflow: visible; }}
    .dashboard-stack {{ display: flex; flex-direction: column; gap: 12px; }}
    .grid, .responsive-pair {{ display: grid; grid-template-columns: 1fr; gap: 12px; align-items: start; min-height: 0; }}
    .top-grid {{ min-height: 0; }}
    .status-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; min-height: 0; }}
    .pipeline-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; min-height: 0; }}
    .health-grid {{ display: grid; grid-template-columns: repeat(6, minmax(140px, 1fr)); gap: 10px; }}
    .health-card {{ background: #111419; border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-height: 58px; }}
    .health-card.ok {{ border-color: rgba(112,209,136,.55); background: rgba(112,209,136,.06); }}
    .health-card.warn {{ border-color: rgba(228,193,104,.65); background: rgba(228,193,104,.07); }}
    .health-card.bad {{ border-color: rgba(239,127,127,.72); background: rgba(239,127,127,.07); }}
    .health-card .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; }}
    .health-card .value {{ margin-top: 6px; font-size: 16px; font-weight: 700; }}
    .gpu-card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; }}
    .gpu-card {{ border: 1px solid var(--line); border-radius: 8px; background: #111419; padding: 10px; min-width: 0; }}
    .gpu-card.usable {{ border-color: rgba(112,209,136,.45); }}
    .gpu-card.busy, .gpu-card.shared {{ border-color: rgba(228,193,104,.45); }}
    .gpu-card.low-memory {{ border-color: rgba(239,127,127,.55); }}
    .gpu-card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }}
    .gpu-id {{ font-size: 16px; font-weight: 700; }}
    .gpu-name {{ color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .gpu-main {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }}
    .gpu-stat .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; }}
    .gpu-stat .value {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}
    .meter {{ height: 8px; border-radius: 999px; background: #242a33; overflow: hidden; margin-top: 6px; }}
    .meter-fill {{ height: 100%; background: #4f8ad8; width: 0%; }}
    .meter-fill.memory-ok {{ background: #70d188; }}
    .meter-fill.memory-warn {{ background: #e4c168; }}
    .meter-fill.memory-bad {{ background: #ef7f7f; }}
    .gpu-process-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .recommendation-grid {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
    .recommendation-card {{ background: #111419; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .recommendation-card .title {{ font-weight: 700; margin-bottom: 6px; }}
    .recommendation-card .meta {{ color: var(--muted); font-size: 12px; margin-top: 6px; }}
    .role-note {{ color: var(--muted); font-size: 13px; margin: 0 0 10px; }}
    @media (min-width: 1400px) {{
      .recommendation-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; min-height: 0; }}
    .panel h2 {{ margin: 0; padding: 10px 12px; font-size: 14px; background: var(--panel2); border-bottom: 1px solid var(--line); }}
    .panel .inner {{ padding: 10px 12px; }}
    .subpanel {{ border: 1px solid var(--line); border-radius: 8px; background: #111419; padding: 12px; margin: 10px 0 14px; }}
    .subpanel h3 {{ margin-top: 0; }}
    .notice {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #111419; }}
    .notice.ok {{ border-color: rgba(112,209,136,.55); }}
    .notice.error {{ border-color: rgba(239,127,127,.65); }}
    .notice.maintenance-banner {{ border-color: rgba(228,193,104,.75); background: rgba(228,193,104,.08); }}
    .notice .headline {{ font-size: 18px; font-weight: 700; margin-bottom: 8px; }}
    .action-modal-backdrop {{
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(5,7,10,.72);
      backdrop-filter: blur(4px);
    }}
    .action-modal-card {{
      width: min(560px, calc(100vw - 32px));
      max-height: min(70vh, 620px);
      overflow: auto;
      background: #181b20;
      border: 1px solid rgba(112,209,136,.55);
      border-radius: 10px;
      box-shadow: 0 20px 70px rgba(0,0,0,.55);
      padding: 18px;
    }}
    .action-modal-card.error {{ border-color: rgba(239,127,127,.7); }}
    .action-modal-card h2 {{ margin: 0 0 10px; font-size: 18px; }}
    .action-modal-card ul {{ margin: 0 0 14px; padding-left: 18px; color: var(--muted); }}
    .action-modal-card li {{ margin: 5px 0; }}
    .action-modal-actions {{ justify-content: flex-end; }}
    .scroll {{ overflow: auto; max-height: 100%; }}
    .queue-scroll, .status-scroll {{ height: auto; min-height: 0; max-height: min(520px, 60vh); }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; background: rgba(255,255,255,.025); }}
    td {{ overflow-wrap: anywhere; }}
    .task-col {{ width: 28%; }}
    .title-col {{ width: 30%; }}
    .time-col {{ width: 18%; }}
    .summary-col {{ width: 42%; }}
    .ready-col {{ width: 56px; }}
    .badge {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 12px; background: #2a2f38; color: var(--muted); }}
    .badge.ok {{ background: rgba(112,209,136,.14); color: var(--green); border: 1px solid rgba(112,209,136,.45); }}
    .badge.warn {{ background: rgba(228,193,104,.14); color: var(--amber); border: 1px solid rgba(228,193,104,.5); }}
    .badge.bad {{ background: rgba(239,127,127,.14); color: var(--red); border: 1px solid rgba(239,127,127,.5); }}
    .attention-group {{ border: 1px solid var(--line); border-radius: 8px; background: #12151a; margin-bottom: 10px; overflow: hidden; }}
    .attention-group > summary {{ cursor: pointer; padding: 10px 12px; background: #1b1f26; font-weight: 700; }}
    .attention-group .group-body {{ padding: 10px 12px; }}
    .mini-note {{ color: var(--muted); font-size: 12px; margin: 0 0 8px; }}
    .completed {{ color: var(--green); }}
    .running {{ color: var(--blue); }}
    .launching {{ color: var(--blue); }}
    .launch_failed {{ color: var(--red); }}
    .partial {{ color: var(--amber); }}
    .failed {{ color: var(--red); }}
    .pending {{ color: var(--muted); }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .actions form {{ display: inline-flex; margin: 0; }}
    .control-row {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,.06); }}
    .control-row:first-of-type {{ border-top: 0; padding-top: 0; }}
    .danger-zone {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }}
    button, .button {{
      appearance: none;
      background: linear-gradient(180deg, var(--button-hover), var(--button));
      color: white;
      border: 1px solid rgba(255,255,255,.12);
      border-bottom-color: rgba(0,0,0,.28);
      border-radius: 7px;
      padding: 7px 11px;
      min-height: 32px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 650;
      line-height: 1.15;
      white-space: nowrap;
      box-shadow: 0 1px 0 rgba(255,255,255,.08) inset, 0 2px 7px rgba(0,0,0,.20);
      transition: transform .08s ease, background-color .12s ease, border-color .12s ease, box-shadow .12s ease, filter .12s ease;
      user-select: none;
    }}
    button:hover, .button:hover {{
      filter: brightness(1.08);
      transform: translateY(-1px);
      border-color: rgba(255,255,255,.20);
      box-shadow: 0 1px 0 rgba(255,255,255,.12) inset, 0 5px 13px rgba(0,0,0,.28);
    }}
    button:active, .button:active {{
      transform: translateY(0);
      filter: brightness(.96);
      box-shadow: 0 1px 2px rgba(0,0,0,.35) inset;
    }}
    button:focus-visible, .button:focus-visible {{
      outline: 3px solid var(--focus);
      outline-offset: 2px;
    }}
    button:disabled, .button.disabled {{
      opacity: .48;
      cursor: not-allowed;
      filter: grayscale(.25);
      transform: none;
      box-shadow: none;
    }}
    .actions.has-submitting form:not(.is-submitting) {{ opacity: .48; transition: opacity .12s ease; }}
    button.quiet-disabled {{ opacity: .38; }}
    button.is-loading {{
      position: relative;
      opacity: .92;
      cursor: wait;
      filter: brightness(1.04);
      box-shadow: 0 0 0 1px rgba(138,180,255,.18), 0 0 0 5px rgba(138,180,255,.08), 0 2px 10px rgba(0,0,0,.26);
    }}
    button.is-loading::before {{
      content: "";
      width: 12px;
      height: 12px;
      border-radius: 999px;
      border: 2px solid rgba(255,255,255,.42);
      border-top-color: #fff;
      display: inline-block;
      animation: spin .75s linear infinite;
      flex: 0 0 auto;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    button.warn {{
      background: linear-gradient(180deg, var(--button-warn-hover), var(--button-warn));
      color: #fff7ed;
    }}
    button.start {{
      background: linear-gradient(180deg, #2f9e55, #217a3d);
      color: #f1fff5;
      border-color: rgba(112,209,136,.42);
    }}
    button.stop {{
      background: linear-gradient(180deg, #b96d26, #8f4f19);
      color: #fff7ed;
      border-color: rgba(228,193,104,.35);
    }}
    button.danger {{
      background: linear-gradient(180deg, var(--button-danger-hover), var(--button-danger));
      color: #fff5f5;
    }}
    button.start:hover, button.stop:hover, button.warn:hover, button.danger:hover {{ filter: brightness(1.1); }}
    input, select {{ background: #0e1014; color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 7px; }}
    label {{ display: flex; flex-direction: column; gap: 5px; color: var(--muted); font-size: 12px; }}
    label input, label select {{ color: var(--text); font-size: 13px; }}
    .form-grid {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
    .preset-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }}
    .preset-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #111419; }}
    .check-row {{ display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 8px; font-weight: 700; }}
    .check-row input {{ width: 16px; height: 16px; }}
    .workplace-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 12px; }}
    .workplace-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #111419; min-width: 0; }}
    .workplace-card.active {{ border-color: rgba(112,209,136,.55); background: rgba(47,158,85,.08); }}
    .remote-run-toolbar {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
    .remote-run-filter-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 10px; }}
    .remote-run-tabs {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .remote-run-tabs a {{ color: var(--muted); border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; text-decoration: none; }}
    .remote-run-tabs a.active {{ color: #dbeafe; border-color: rgba(107,166,255,.65); background: rgba(47,104,184,.18); }}
    .remote-run-workplaces a {{ max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .remote-thumb-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }}
    .remote-thumb {{ border: 1px solid var(--line); border-radius: 8px; background: #111419; padding: 10px; min-width: 0; }}
    .remote-thumb.running, .remote-thumb.launching, .remote-thumb.launch_unknown {{ border-color: rgba(107,166,255,.5); }}
    .remote-thumb.completed {{ border-color: rgba(112,209,136,.38); }}
    .remote-thumb.failed {{ border-color: rgba(239,127,127,.55); }}
    .remote-thumb.partial {{ border-color: rgba(228,193,104,.48); }}
    .remote-thumb-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }}
    .remote-thumb-title {{ font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .remote-thumb-subtitle {{ color: var(--muted); font-size: 12px; min-height: 32px; margin-top: 6px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
    .remote-thumb-meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5px 8px; margin: 10px 0; font-size: 12px; color: var(--muted); }}
    .remote-thumb-meta strong {{ color: var(--text); }}
    .remote-thumb .actions {{ gap: 6px; }}
    .remote-thumb button {{ min-height: 29px; padding: 5px 8px; font-size: 12px; }}
    .pager {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px; }}
    .pager a {{ color: #9ec5ff; border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; text-decoration: none; }}
    .pager .disabled {{ color: #667085; border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; }}
    .run-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .run-meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 10px; font-size: 12px; color: var(--muted); }}
    .log-snippet {{ max-height: 170px; font-size: 12px; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #0b0c0f; border: 1px solid var(--line); border-radius: 6px; padding: 10px; max-height: 520px; overflow: auto; }}
    .muted {{ color: var(--muted); }}
    .stack {{ display: flex; flex-direction: column; gap: 12px; min-height: 0; }}
    .truncate {{ display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }}
    h3 {{ margin: 12px 0 8px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0; }}
    details.section {{ border: 1px solid var(--line); border-radius: 8px; background: rgba(255,255,255,.015); overflow: hidden; }}
    details.section > summary {{ cursor: pointer; padding: 10px 12px; background: var(--panel2); font-weight: 700; }}
    details.section > .section-body {{ padding: 12px; display: flex; flex-direction: column; gap: 12px; }}
    @media (min-width: 1400px) {{
      .responsive-pair {{ grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); }}
      .pipeline-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .form-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (min-width: 1600px) {{
      .status-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
{heartbeat_script}
</head>
<body>
  <header>
    <div><strong>Solution Agent Dashboard</strong> <span class="muted">{_esc(DASHBOARD_VERSION)}</span></div>
    <nav><a href="/">Dashboard</a> · <a href="/manage">Manage</a> · <a href="/settings">Remote Target</a></nav>
  </header>
  <main>{maintenance_banner}{body}</main>
</body>
</html>"""
    return html_text.encode("utf-8")


def _render_health_summary(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    scheduler = lab.scheduler_status()
    scheduler_running = bool(scheduler.get("running")) if isinstance(scheduler, dict) else False
    maintenance = lab.load_maintenance_mode()
    drain = lab.load_drain_mode()
    try:
        hygiene = lab.registry_hygiene(apply_safe=False)
    except Exception as exc:
        hygiene = {"issue_count": "?", "error": str(exc)}
    try:
        v1_health = lab.v1_stability_report()
    except Exception as exc:
        v1_health = {"ok": False, "status": "error", "issues": [{"code": "v1_health_error", "message": str(exc)}], "remote_summary": {}}
    grouped = _records_by_status()
    running_count = len(grouped.get("running", []))
    launching_count = len(grouped.get("launching", []))
    failed_count = len(grouped.get("failed", [])) + len(grouped.get("launch_failed", []))
    partial_count = len(grouped.get("partial", []))
    blocked_count = len(plan.get("skipped", []) if isinstance(plan, dict) else [])
    gpu_snapshot = (plan or {}).get("gpu_snapshot") if isinstance(plan, dict) else {}
    gpus = gpu_snapshot.get("gpus", []) if isinstance(gpu_snapshot, dict) else []
    min_free = float(config.get("min_free_gpu_mem_gb") or 10)
    usable = sum(1 for gpu in gpus if isinstance(gpu, dict) and _gpu_status(gpu, min_free) in {"usable", "shared", "external occupied"})
    total_gpus = len(gpus)
    next_items = (plan or {}).get("plan", []) if isinstance(plan, dict) else []
    remote_usage = _remote_call_usage()
    remote_windows = remote_usage.get("windows") if isinstance(remote_usage, dict) else {}
    remote_mode = str(remote_usage.get("mode") or "normal") if isinstance(remote_usage, dict) else "normal"
    remote_class = "failed" if remote_mode == "danger" else ("partial" if remote_mode == "attention" else "running")
    remote_card_class = "bad" if remote_mode == "danger" else ("warn" if remote_mode == "attention" else "ok")
    remote_badge_class = remote_card_class
    c5 = int((remote_windows or {}).get(5, 0) or 0)
    c30 = int((remote_windows or {}).get(30, 0) or 0)
    c60 = int((remote_windows or {}).get(60, 0) or 0)
    remote_counts = (
        f'<span class="{_remote_count_class(5, c5)}">{_esc(c5)}/5m</span> · '
        f'<span class="{_remote_count_class(30, c30)}">{_esc(c30)}/30m</span> · '
        f'<span class="{_remote_count_class(60, c60)}">{_esc(c60)}/60m</span>'
    )
    remote_kind = remote_usage.get("by_kind_60") if isinstance(remote_usage, dict) else {}
    kind_labels = {"ssh": "ssh", "scp_to": "upload", "scp_from": "download", "ssh_stream": "stream"}
    remote_kind_text = " · ".join(
        f"{kind_labels.get(str(key), str(key))} {value}"
        for key, value in list((remote_kind or {}).items())[:3]
    )
    remote_detail = _relative_age_text(remote_usage.get("last_ts")).replace("last: ", "last ")
    failures = int(remote_usage.get("consecutive_failures", 0) or 0) if isinstance(remote_usage, dict) else 0
    if failures:
        remote_detail += f" · failures {failures}"
    if remote_kind_text:
        remote_detail += f" · {remote_kind_text}"
    v1_ok = bool(v1_health.get("ok")) if isinstance(v1_health, dict) else False
    v1_status = str(v1_health.get("status") or ("ok" if v1_ok else "attention")) if isinstance(v1_health, dict) else "attention"
    v1_issues = v1_health.get("issues") if isinstance(v1_health, dict) else []
    v1_warnings = v1_health.get("warnings") if isinstance(v1_health, dict) else []
    v1_remote = v1_health.get("remote_summary") if isinstance(v1_health, dict) else {}
    v1_active_remote = (v1_remote or {}).get("active_records", 0) if isinstance(v1_remote, dict) else 0
    v1_detail = f"issues {len(v1_issues or [])} · warnings {len(v1_warnings or [])} · active remote {v1_active_remote}"
    hygiene_issues = hygiene.get("issue_count")
    maintenance_on = bool(maintenance.get("enabled"))
    drain_on = bool(drain.get("enabled"))
    if maintenance_on:
        mode_text = "maintenance"
        mode_class = "partial"
    elif drain_on:
        mode_text = "drain"
        mode_class = "partial"
    else:
        mode_text = "normal"
        mode_class = "running"
    if maintenance_on:
        next_action = "read-only maintenance"
    elif hygiene_issues:
        next_action = "run hygiene"
    elif failed_count:
        next_action = "inspect failed"
    elif drain_on:
        next_action = "collect / reconcile"
    elif next_items:
        next_action = "schedule once"
    elif blocked_count:
        next_action = "check blockers"
    else:
        next_action = "monitor"
    return (
        '<div class="panel"><h2>Current Situation</h2><div class="inner"><div class="health-grid">'
        f'<div class="health-card"><div class="label">Mode</div><div class="value {mode_class}">{_esc(mode_text)}</div><div class="muted">{_esc(maintenance.get("reason") if maintenance_on else drain.get("reason") if drain_on else "launch allowed")}</div></div>'
        f'<div class="health-card"><div class="label">Scheduler</div><div class="value {"running" if scheduler_running else "failed"}">{_esc("running" if scheduler_running else "stopped")}</div></div>'
        f'<div class="health-card {"bad" if hygiene_issues else "ok"}"><div class="label">Registry hygiene</div><div class="value {"failed" if hygiene_issues else "running"}">{_esc(hygiene_issues)}</div><div class="muted">{_esc(hygiene.get("error") or "issues")}</div></div>'
        f'<div class="health-card"><div class="label">GPU usable</div><div class="value">{_esc(usable)}/{_esc(total_gpus)}</div></div>'
        f'<div class="health-card"><div class="label">Running / Launching</div><div class="value running">{_esc(running_count)} / {_esc(launching_count)}</div></div>'
        f'<div class="health-card"><div class="label">Blocked</div><div class="value partial">{_esc(blocked_count)}</div></div>'
        f'<div class="health-card"><div class="label">Partial / Failed</div><div class="value">{_esc(partial_count)} / <span class="failed">{_esc(failed_count)}</span></div></div>'
        f'<div class="health-card {"ok" if v1_ok else "bad"}"><div class="label">V1 Health</div><div class="value {"running" if v1_ok else "failed"}">{_esc(v1_status)}</div><div class="muted">{_esc(v1_detail)}</div></div>'
        f'<div class="health-card {remote_card_class}"><div class="label">Remote calls <span class="badge {remote_badge_class}">{_esc(remote_mode)}</span></div><div class="value {remote_class}">{remote_counts}</div><div class="muted">{_esc(remote_detail)}</div></div>'
        f'<div class="health-card"><div class="label">Next action</div><div class="value">{_esc(next_action)}</div></div>'
        "</div></div></div>"
    )


def _render_scheduler(config: Dict[str, object]) -> str:
    status = lab.scheduler_status()
    watcher = lab.remote_watch_status()
    maintenance = lab.load_maintenance_mode()
    drain = lab.load_drain_mode()
    maintenance_on = bool(maintenance.get("enabled"))
    drain_on = bool(drain.get("enabled"))
    lock = status.get("lock") if isinstance(status, dict) else {}
    running = bool(status.get("running"))
    stale = bool(status.get("stale")) if isinstance(status, dict) else False
    health_text = "stale" if stale else ("running" if running else "stopped")
    health_class = "failed" if stale else ("running" if running else "failed")
    age = status.get("heartbeat_age_seconds") if isinstance(status, dict) else None
    age_text = f" | age: {age}s" if age is not None else ""
    watcher_lock = watcher.get("lock") if isinstance(watcher, dict) else {}
    watcher_state = watcher.get("state") if isinstance(watcher, dict) else {}
    watcher_running = bool(watcher.get("running")) if isinstance(watcher, dict) else False
    watcher_stale = bool(watcher.get("stale")) if isinstance(watcher, dict) else False
    watcher_text = "stale" if watcher_stale else ("streaming" if watcher_running else "stopped")
    watcher_class = "failed" if watcher_stale else ("running" if watcher_running else "failed")
    watcher_age = watcher.get("heartbeat_age_seconds") if isinstance(watcher, dict) else None
    watcher_age_text = f" | age: {watcher_age}s" if watcher_age is not None else ""
    watcher_detail = ""
    if isinstance(watcher_state, dict):
        watcher_detail = (
            f"state: {watcher_state.get('state') or '-'} | "
            f"frames: {watcher_state.get('frames') or 0} | "
            f"last success: {watcher_state.get('last_success_at') or '-'}"
        )
    action_disabled_reason = "maintenance mode is active" if maintenance_on else ""
    launch_disabled_reason = action_disabled_reason or ("drain mode blocks new launches" if drain_on else "")
    schedule_once = (
        _disabled_button("Request Schedule Once", launch_disabled_reason, "start")
        if launch_disabled_reason
        else '<form method="post" action="/action" onsubmit="return confirm(\'Request a schedule-once launch action? The CLI processor must apply it with --allow-launch.\')"><input type="hidden" name="action" value="schedule_once"><button class="start">Request Schedule Once</button></form>'
    )
    reconcile = (
        _disabled_button("Request Reconcile", action_disabled_reason)
        if action_disabled_reason
        else '<form method="post" action="/action"><input type="hidden" name="action" value="dependency_reconcile"><button>Request Reconcile</button></form>'
    )
    body = [
        '<div class="panel"><h2>Scheduler</h2><div class="inner">',
        f'<p>Status: <strong class="{health_class}">{_esc(health_text)}</strong></p>',
        f'<p class="muted">PID: {_esc((lock or {}).get("pid"))} | heartbeat: {_esc((lock or {}).get("heartbeat_at"))}{_esc(age_text)}</p>',
        f'<p>Remote GPU watcher: <strong class="{watcher_class}">{_esc(watcher_text)}</strong></p>',
        f'<p class="muted">PID: {_esc((watcher_lock or {}).get("pid"))} | heartbeat: {_esc((watcher_lock or {}).get("heartbeat_at"))}{_esc(watcher_age_text)}<br>{_esc(watcher_detail)}</p>',
        '<div class="control-row"><h3>Lifecycle</h3><div class="actions">',
        '<form method="post" action="/action"><input type="hidden" name="action" value="scheduler_start"><button class="start">Start Scheduler</button></form>',
        '<form method="post" action="/action" onsubmit="return confirm(\'Stop scheduler?\')"><input type="hidden" name="action" value="scheduler_stop"><button class="stop">Stop Scheduler</button></form>',
        '<form method="post" action="/action"><input type="hidden" name="action" value="remote_watch_start"><button class="start">Start GPU Watcher</button></form>',
        '<form method="post" action="/action" onsubmit="return confirm(\'Stop the persistent GPU watcher SSH stream?\')"><input type="hidden" name="action" value="remote_watch_stop"><button class="stop">Stop GPU Watcher</button></form>',
        '</div></div>',
        '<div class="control-row"><h3>Routine Actions</h3><div class="actions">',
        '<form method="get" action="/"><button>Refresh Board From Cache</button></form>',
        '<form method="post" action="/action"><input type="hidden" name="action" value="local_init"><button>Initialize Local</button></form>',
        '<form method="post" action="/action"><input type="hidden" name="action" value="remote_preflight"><button>Preflight Remote</button></form>',
        '<form method="post" action="/action"><input type="hidden" name="action" value="refresh_gpu"><button>Refresh GPU Now</button></form>',
        '<form method="post" action="/action"><input type="hidden" name="action" value="supervise_once"><button>Supervise / Collect Once</button></form>',
        reconcile,
        '<form method="get" action="/"><button>Schedule Dry Run</button></form>',
        schedule_once,
        '<form method="post" action="/action"><input type="hidden" name="action" value="show_completed"><button>Show Completed</button></form>',
        '</div></div>',
        '<div class="danger-zone"><h3>Cleanup</h3><div class="actions">',
        '<form method="post" action="/action" onsubmit="return confirm(\'Hide completed tasks from the board? Results will not be deleted.\') && confirm(\'Second confirmation: this only changes the local board view, not files.\')"><input type="hidden" name="action" value="clear_completed"><button class="warn">Clear Completed From Board</button></form>',
        "</div></div></div></div>",
    ]
    return "\n".join(body)


def _render_remote_target(config: Dict[str, object]) -> str:
    mapping_note = str(config.get("cuda_mapping_note") or "").strip()
    mapping_html = f'<p class="muted">{_esc(mapping_note)}</p>' if mapping_note else ""
    active_worker = str(config.get("remote_worker") or DEFAULT_REMOTE_WORKER)
    worker_select = (
        '<form class="inline-form" method="post" action="/action">'
        '<input type="hidden" name="action" value="save_remote_config">'
        f'<input type="hidden" name="host" value="{_esc(config.get("host"))}">'
        f'<input type="hidden" name="remote_workdir" value="{_esc(config.get("remote_workdir"))}">'
        f'<input type="hidden" name="remote_root" value="{_esc(config.get("remote_root"))}">'
        f'<input type="hidden" name="allowed_gpu_ids" value="{_esc(config.get("allowed_gpu_ids"))}">'
        f'<input type="hidden" name="cuda_mapping_note" value="{_esc(config.get("cuda_mapping_note") or "")}">'
        f'<label>Default worker {_remote_worker_select("remote_worker", active_worker, autosubmit=True)}</label>'
        "</form>"
    )
    return (
        '<div class="panel"><h2>Remote Target</h2><div class="inner">'
        f'<p><strong>{_esc(config.get("host"))}</strong> '
        f'<span class="muted">workdir: {_esc(config.get("remote_workdir"))} | output: {_esc(config.get("remote_root"))}</span></p>'
        f'<p class="muted">GPUs: nvidia-smi ids {_esc(config.get("allowed_gpu_ids"))} | new launches use: <strong>{_esc(_remote_worker_label(active_worker))}</strong></p>'
        + mapping_html
        + '<div class="actions"><a href="/settings"><button>Change Target</button></a>'
        + worker_select
        + '<form method="post" action="/action"><input type="hidden" name="action" value="remote_preflight"><button>Preflight Remote</button></form>'
        '<form method="post" action="/action" onsubmit="return confirm(\'Initialize this remote workplace? This writes a small marker/profile report but does not install packages.\')"><input type="hidden" name="action" value="workplace_init"><button>Initialize Workplace</button></form>'
        '<form method="post" action="/action"><input type="hidden" name="action" value="sync_profile"><button>Sync Profile</button></form></div>'
        "</div></div>"
    )


def _render_workplaces(config: Dict[str, object]) -> str:
    active_key = _remote_workplace_key(_current_remote_preset(config))
    cards: List[str] = []
    hidden_count = len(_hidden_history_workplaces(config))
    for workplace in _remote_workplaces(config):
        key = _remote_workplace_key(workplace)
        active = key == active_key
        counts = workplace.get("counts") if isinstance(workplace.get("counts"), dict) else {}
        source_badge = "active" if active else "available"
        if workplace.get("custom_index") is not None:
            source_badge = "custom"
        elif workplace.get("builtin"):
            source_badge = "built-in"
        elif workplace.get("from_registry"):
            source_badge = "history"
        switch_form = ""
        if not active:
            switch_form = (
                '<form method="post" action="/action">'
                '<input type="hidden" name="action" value="save_remote_config">'
                f'<input type="hidden" name="host" value="{_esc(workplace.get("host"))}">'
                f'<input type="hidden" name="remote_workdir" value="{_esc(workplace.get("remote_workdir"))}">'
                f'<input type="hidden" name="remote_root" value="{_esc(workplace.get("remote_root"))}">'
                f'<input type="hidden" name="allowed_gpu_ids" value="{_esc(workplace.get("allowed_gpu_ids") or config.get("allowed_gpu_ids"))}">'
                f'<input type="hidden" name="cuda_mapping_note" value="{_esc(workplace.get("cuda_mapping_note") or "")}">'
                f'<input type="hidden" name="remote_worker" value="{_esc(config.get("remote_worker") or DEFAULT_REMOTE_WORKER)}">'
                '<button>Switch Active</button>'
                "</form>"
            )
        delete_form = ""
        if workplace.get("custom_index") is not None:
            delete_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Delete this saved workplace? Existing runs are not deleted.\') && confirm(\'Second confirmation: remove this saved workplace from the UI?\')">'
                '<input type="hidden" name="action" value="delete_remote_preset">'
                f'<input type="hidden" name="preset_index" value="{_esc(workplace.get("custom_index"))}">'
                '<button class="warn">Delete</button>'
                "</form>"
            )
        elif workplace.get("from_registry") and not active:
            delete_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Forget this history workplace from the UI? Remote files, local task history, and results are not deleted.\') && confirm(\'Second confirmation: hide this history workplace from Manage/Workplaces?\')">'
                '<input type="hidden" name="action" value="hide_history_workplace">'
                f'<input type="hidden" name="host" value="{_esc(workplace.get("host"))}">'
                f'<input type="hidden" name="remote_root" value="{_esc(workplace.get("remote_root"))}">'
                '<button class="warn">Forget</button>'
                "</form>"
            )
        cards.append(
            f'<div class="workplace-card {"active" if active else ""}">'
            f'<div class="run-head"><strong>{_esc(workplace.get("label") or workplace.get("host"))}</strong>'
            f'<span class="badge {"running" if active else ""}">{_esc(source_badge)}</span></div>'
            f'<p class="muted">host: {_esc(workplace.get("host"))}<br>workdir: {_esc(workplace.get("remote_workdir"))}<br>root: {_esc(workplace.get("remote_root"))}</p>'
            f'<p class="muted">runs: {_esc(counts.get("total", 0))} · active workers: {_esc(counts.get("active", 0))}</p>'
            '<div class="actions">'
            + switch_form
            + delete_form
            + "</div></div>"
        )
    restore_form = ""
    if hidden_count:
        restore_form = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="show_hidden_history_workplaces">'
            f'<button>Show Hidden History ({_esc(hidden_count)})</button>'
            "</form>"
        )
    return (
        '<div class="panel"><h2>Workplaces</h2><div class="inner">'
        '<p class="muted">Switching the active workplace only changes where new scheduling actions go. Existing remote workers keep running in their own remote_task_dir.</p>'
        '<div class="actions"><a href="/settings"><button>Add / Edit Workplace</button></a><a href="/manage"><button>Manage</button></a>'
        + restore_form
        + "</div>"
        '<div class="workplace-grid">'
        + "".join(cards)
        + "</div></div></div>"
    )


def _latest_remote_status_text(record: Dict[str, object]) -> str:
    reconcile = record.get("last_remote_reconcile") if isinstance(record.get("last_remote_reconcile"), dict) else {}
    status = reconcile.get("status") if isinstance(reconcile, dict) else {}
    status_run = status.get("status") if isinstance(status, dict) else {}
    if isinstance(status_run, dict):
        text = status_run.get("stdout")
        if text:
            return str(text)
    remote_status = record.get("remote_status") if isinstance(record.get("remote_status"), dict) else {}
    if isinstance(remote_status, dict):
        text = remote_status.get("stdout")
        if text:
            return str(text)
    return ""


def _remote_section(text: str, label: str, *, stop_labels: Iterable[str] = ()) -> str:
    if not text:
        return ""
    pattern = re.compile(rf"^{re.escape(label)}:\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    stops = [item for item in stop_labels] or [
        "files",
        "pid",
        "exit_code",
        "run_state",
        "state_json",
        "heartbeat_json",
        "heartbeat_age_seconds",
        "running",
        "stderr_tail",
        "stdout_tail",
    ]
    stop_pattern = re.compile(r"^(?:" + "|".join(re.escape(item) for item in stops if item != label) + r"):\s*$", re.MULTILINE)
    stop = stop_pattern.search(text, start)
    end = stop.start() if stop else len(text)
    return text[start:end].strip()


def _remote_run_rows(
    config: Dict[str, object],
    *,
    limit: Optional[int] = 18,
    all_workplaces: bool = True,
    status_filter: str = "active",
    workplace_filter: str = "all",
) -> List[Dict[str, object]]:
    tasks = load_registry().get("tasks", {})
    hidden_completed = _hidden_completed()
    hidden_attention = _hidden_attention()
    rows: List[Dict[str, object]] = []
    for task_id, record in tasks.items():
        if not isinstance(record, dict):
            continue
        remote = record.get("remote") if isinstance(record.get("remote"), dict) else {}
        if not remote:
            continue
        remote_root = str(remote.get("remote_root") or "")
        configured_root = str(config.get("remote_root") or "")
        if not all_workplaces and configured_root and remote_root and configured_root != remote_root:
            continue
        if workplace_filter != "all":
            key = "|".join(
                [
                    str(remote.get("host") or ""),
                    remote_root,
                ]
            )
            if key != workplace_filter:
                continue
        status = str(record.get("status") or "unknown")
        task_key = str(record.get("task_id") or task_id)
        if status == "completed" and task_key in hidden_completed:
            continue
        if status in {"failed", "partial", "launch_failed"} and task_key in hidden_attention:
            continue
        if status_filter == "active" and status not in {"running", "launching", "launch_unknown", "failed", "partial"}:
            continue
        if status_filter != "all" and status_filter not in {"active", status}:
            continue
        status_text = _latest_remote_status_text(record)
        env = remote.get("remote_execution_env") if isinstance(remote.get("remote_execution_env"), dict) else {}
        isolation = env.get("isolation") if isinstance(env.get("isolation"), dict) else {}
        rows.append(
            {
                "task_id": str(record.get("task_id") or task_id),
                "title": str(record.get("title") or ""),
                "status": status,
                "remote_state": str(record.get("remote_state") or ""),
                "run_id": str(remote.get("run_id") or record.get("latest_run_id") or ""),
                "worker": str((env or {}).get("worker") or (remote.get("worker") or DEFAULT_REMOTE_WORKER)),
                "host": str(remote.get("host") or config.get("host") or ""),
                "remote_workdir": str(remote.get("remote_workdir") or ""),
                "remote_root": remote_root,
                "remote_task_dir": str(remote.get("remote_task_dir") or ""),
                "env_mode": str((env or {}).get("mode") or "-"),
                "conda_env": str((env or {}).get("conda_env") or "-"),
                "isolation": isolation,
                "pid": _remote_section(status_text, "pid"),
                "exit_code": _remote_section(status_text, "exit_code"),
                "run_state": _remote_section(status_text, "run_state"),
                "running": _remote_section(status_text, "running"),
                "heartbeat_age": _remote_section(status_text, "heartbeat_age_seconds"),
                "stderr_tail": _remote_section(status_text, "stderr_tail")[-900:],
                "stdout_tail": _remote_section(status_text, "stdout_tail")[-900:],
                "seen_at": str(record.get("last_remote_seen_at") or ""),
                "started_at": str(record.get("started_at") or record.get("updated_at") or record.get("created_at") or ""),
            }
        )
    rows.sort(
        key=lambda item: (
            1 if str(item.get("status")) in {"running", "launching", "launch_unknown"} else 0,
            str(item.get("started_at") or ""),
        ),
        reverse=True,
    )
    return rows[:limit] if limit is not None else rows


def _remote_workplace_query_key(workplace: Dict[str, object]) -> str:
    return "|".join(
        [
            str(workplace.get("host") or ""),
            str(workplace.get("remote_root") or ""),
        ]
    )


def _remote_workplace_short_label(workplace: Dict[str, object]) -> str:
    root = str(workplace.get("remote_root") or "")
    label = root.rstrip("/").split("/")[-1] if root else str(workplace.get("label") or "workplace")
    active = " *" if workplace.get("active") else ""
    return f'{label}{active}'


def _remote_runs_query_link(status_filter: str, page: int, workplace_filter: str = "all") -> str:
    query = urllib.parse.urlencode(
        {"remote_status": status_filter, "remote_page": max(1, page), "remote_workplace": workplace_filter}
    )
    return "/?" + query + "#remote-runs"


def _render_remote_runs(config: Dict[str, object], params: Optional[Dict[str, List[str]]] = None) -> str:
    params = params or {}
    allowed_filters = {"active", "running", "failed", "partial", "completed", "all"}
    status_filter = (params.get("remote_status") or ["active"])[0]
    if status_filter not in allowed_filters:
        status_filter = "active"
    workplace_filter = (params.get("remote_workplace") or ["all"])[0]
    workplace_keys = {"all"} | {_remote_workplace_query_key(item) for item in _remote_workplaces(config)}
    if workplace_filter not in workplace_keys:
        workplace_filter = "all"
    try:
        page = max(1, int((params.get("remote_page") or ["1"])[0]))
    except ValueError:
        page = 1
    page_size = 12
    all_rows = _remote_run_rows(
        config,
        limit=None,
        all_workplaces=True,
        status_filter=status_filter,
        workplace_filter=workplace_filter,
    )
    total = len(all_rows)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(page, page_count)
    start = (page - 1) * page_size
    rows = all_rows[start : start + page_size]
    worker = str(config.get("remote_worker") or DEFAULT_REMOTE_WORKER)
    limits = (
        f'max starts/cycle {_esc(config.get("max_starts_per_cycle"))} · '
        f'total {_esc(config.get("max_running_total"))} · '
        f'gpu {_esc(config.get("max_running_gpu"))} · cpu {_esc(config.get("max_running_cpu"))} · '
        f'per gpu {_esc(config.get("max_tasks_per_gpu"))}'
    )
    filters = [
        ("active", "Needs Attention"),
        ("running", "Running"),
        ("failed", "Failed"),
        ("partial", "Partial"),
        ("completed", "Completed"),
        ("all", "All"),
    ]
    tabs = "".join(
        f'<a class="{"active" if key == status_filter else ""}" href="{_esc(_remote_runs_query_link(key, 1, workplace_filter))}">{_esc(label)}</a>'
        for key, label in filters
    )
    workplace_links = [
        f'<a class="{"active" if workplace_filter == "all" else ""}" href="{_esc(_remote_runs_query_link(status_filter, 1, "all"))}">All Workplaces</a>'
    ]
    for workplace in _remote_workplaces(config):
        key = _remote_workplace_query_key(workplace)
        title = f'{workplace.get("host") or ""} {workplace.get("remote_root") or ""}'
        workplace_links.append(
            f'<a class="{"active" if key == workplace_filter else ""}" title="{_esc(title)}" href="{_esc(_remote_runs_query_link(status_filter, 1, key))}">{_esc(_remote_workplace_short_label(workplace))}</a>'
        )
    workplace_tabs = '<nav class="remote-run-tabs remote-run-workplaces">' + "".join(workplace_links) + "</nav>"
    toolbar = (
        '<div class="remote-run-toolbar">'
        f'<div><p class="muted">New launches: {_esc(_remote_worker_label(worker))} | {limits}</p>'
        '<p class="muted">Remote Runs is a compact monitor wall. Open a task for full paths, logs, result.yaml, and artifacts.</p></div>'
        f'<nav class="remote-run-tabs">{tabs}</nav>'
        "</div>"
        f'<div class="remote-run-filter-row">{workplace_tabs}</div>'
    )
    if not rows:
        return (
            '<div class="panel" id="remote-runs"><h2>Remote Runs</h2><div class="inner">'
            + toolbar
            + f'<p>No remote runs match filter: {_esc(status_filter)}.</p>'
            "</div></div>"
        )
    cards: List[str] = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        status = str(row.get("status") or "unknown")
        state = str(row.get("run_state") or row.get("remote_state") or "-")
        running = str(row.get("running") or "-")
        heartbeat = str(row.get("heartbeat_age") or "-")
        root_label = str(row.get("remote_root") or "").rstrip("/").split("/")[-1] or "-"
        monitor_form = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="remote_monitor_task">'
            f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
            f'<input type="hidden" name="host" value="{_esc(row.get("host"))}">'
            f'<input type="hidden" name="remote_root" value="{_esc(row.get("remote_root"))}">'
            '<button>Monitor / Collect</button>'
            "</form>"
        )
        stop_form = (
            '<form method="post" action="/action" onsubmit="return confirm(\'Request remote stop for this task?\')">'
            '<input type="hidden" name="action" value="remote_stop">'
            f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
            '<button class="stop">Stop Request</button>'
            "</form>"
        )
        if status == "completed":
            hide_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Hide this completed remote run from dashboard lists? Results are not deleted.\')">'
                '<input type="hidden" name="action" value="bulk_hide_completed">'
                f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
                '<button class="warn">Hide</button>'
                "</form>"
            )
        elif status in {"failed", "partial", "launch_failed"}:
            hide_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Hide this remote run from attention/dashboard lists? Files and results are not deleted.\')">'
                '<input type="hidden" name="action" value="hide_attention_task">'
                f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
                '<button class="warn">Hide</button>'
                "</form>"
            )
        else:
            hide_form = ""
        cards.append(
            f'<div class="remote-thumb {_esc(status)}">'
            '<div class="remote-thumb-head">'
            f'<div class="remote-thumb-title"><a href="/task?id={urllib.parse.quote(task_id)}">{_esc(task_id)}</a></div>'
            f'<span class="badge {_esc(status)}">{_esc(status)}</span>'
            "</div>"
            f'<div class="remote-thumb-subtitle">{_esc(row.get("title") or "")}</div>'
            '<div class="remote-thumb-meta">'
            f'<span>place<br><strong>{_esc(root_label)}</strong></span>'
            f'<span>state<br><strong>{_esc(state)}</strong></span>'
            f'<span>worker<br><strong>{_esc(row.get("worker"))}</strong></span>'
            f'<span>env<br><strong>{_esc(row.get("conda_env"))}</strong></span>'
            f'<span>running<br><strong>{_esc(running)}</strong></span>'
            f'<span>exit<br><strong>{_esc(row.get("exit_code") or "-")}</strong></span>'
            f'<span>pid<br><strong>{_esc(row.get("pid") or "-")}</strong></span>'
            f'<span>hb<br><strong>{_esc(heartbeat)}s</strong></span>'
            "</div>"
            f'<div class="actions">{monitor_form}<a href="/task?id={urllib.parse.quote(task_id)}"><button>Open</button></a>{hide_form}{stop_form}</div>'
            "</div>"
        )
    prev_link = (
        f'<a href="{_esc(_remote_runs_query_link(status_filter, page - 1, workplace_filter))}">Prev</a>' if page > 1 else '<span class="disabled">Prev</span>'
    )
    next_link = (
        f'<a href="{_esc(_remote_runs_query_link(status_filter, page + 1, workplace_filter))}">Next</a>'
        if page < page_count
        else '<span class="disabled">Next</span>'
    )
    pager = f'<div class="pager">{prev_link}<span class="muted">Page {page} / {page_count} · {total} runs</span>{next_link}</div>'
    return (
        '<div class="panel" id="remote-runs"><h2>Remote Runs</h2><div class="inner">'
        + toolbar
        + '<div class="remote-thumb-grid">'
        + "".join(cards)
        + "</div>"
        + pager
        + "</div></div>"
    )


def _render_scheduler_advisor(plan: Optional[Dict[str, object]]) -> str:
    try:
        advisory = lab.scheduler_advisory(plan)
    except Exception as exc:
        return f'<div class="panel"><h2>Scheduler Advisor</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'

    candidate_by_id = {
        str(item.get("candidate_id")): item
        for item in advisory.get("candidate_actions", [])
        if isinstance(item, dict)
    }
    filtered_count = 0

    def visible_action(kind: str, action: Dict[str, object]) -> bool:
        task_id = str(action.get("task_id") or "")
        if not task_id or task_id in _hidden_attention():
            return False
        try:
            record = lab.status(task_id)
        except Exception:
            record = {}
        status = str(record.get("status") or "")
        if status == "completed":
            return False
        action_type = str(action.get("action_type") or "")
        reason = str(action.get("reason_hint") or action.get("reason_code") or "")
        if action_type == "ask_user" and "Partial artifacts exist" in reason:
            if status != "partial":
                return False
            active_dependents = [
                item
                for item in _dependent_tasks(task_id)
                if str(item.get("status") or "") in {"pending", "running", "launching", "partial", "launch_unknown"}
            ]
            return bool(active_dependents)
        if action_type in {"verify_launch", "repair_launch_failed"}:
            return status in {"launching", "launch_unknown", "launch_failed", "running"}
        if kind == "hold" and status in {"failed", "completed"}:
            return False
        return True

    def action_row(kind: str, action: Dict[str, object]) -> str:
        task_id = str(action.get("task_id") or "")
        legal = str(action.get("legal_status") or "")
        score = action.get("base_score")
        reason = action.get("reason_hint") or action.get("reason_code") or action.get("action_type") or ""
        kind_label = {
            "start": "next to inspect/start",
            "hold": "hold for now",
            "ask": "needs your choice",
            "warn": "watch this",
        }.get(kind, kind)
        if action.get("action_type") == "start_task":
            reason = (
                f"{reason or 'start task'}; resource={action.get('resource')}; "
                f"stage={action.get('execution_stage')}; priority score={score}"
            )
        if legal == "legal":
            legal_text = "scheduler may run it"
        elif legal == "advisory_only":
            legal_text = "advice only"
        else:
            legal_text = legal or "needs check"
        return (
            "<tr>"
            f'<td><span class="{_esc("running" if kind == "start" else "partial" if kind in {"hold", "ask"} else "failed")}">{_esc(kind_label)}</span></td>'
            f'<td><a href="/why?id={urllib.parse.quote(task_id)}">{_esc(task_id)}</a></td>'
            f"<td>{_esc(legal_text)}</td>"
            f"<td>{_esc(reason)}</td>"
            "</tr>"
        )

    rows = []
    for candidate_id in advisory.get("recommended_candidate_ids", [])[:8]:
        action = candidate_by_id.get(str(candidate_id))
        if action and visible_action("start", action):
            rows.append(action_row("start", action))
        elif action:
            filtered_count += 1
    for action in advisory.get("holds", [])[:8]:
        if isinstance(action, dict) and visible_action("hold", action):
            rows.append(action_row("hold", action))
        elif isinstance(action, dict):
            filtered_count += 1
    for action in advisory.get("questions", [])[:8]:
        if isinstance(action, dict) and visible_action("ask", action):
            rows.append(action_row("ask", action))
        elif isinstance(action, dict):
            filtered_count += 1
    for action in advisory.get("warnings", [])[:8]:
        if isinstance(action, dict) and visible_action("warn", action):
            rows.append(action_row("warn", action))
        elif isinstance(action, dict):
            filtered_count += 1
    if not rows:
        rows.append('<tr><td colspan="4" class="muted">No advisor recommendations in this dry-run.</td></tr>')
    summary = str(advisory.get("summary") or "")
    feedback = _advisor_feedback()
    feedback_hint = f"<p class=\"muted\">Current feedback: {_esc(feedback[:260])}</p>" if feedback.strip() else ""
    filter_hint = (
        f'<p class="muted">Filtered {_esc(str(filtered_count))} resolved/history-only advisor item(s).</p>'
        if filtered_count
        else ""
    )
    top_start = None
    for candidate_id in advisory.get("recommended_candidate_ids", []):
        action = candidate_by_id.get(str(candidate_id))
        if action and action.get("action_type") == "start_task":
            top_start = action
            break
    if top_start:
        task_link = (
            f'<a href="/why?id={urllib.parse.quote(str(top_start.get("task_id") or ""))}">'
            f'{_esc(top_start.get("task_id"))}</a>'
        )
        next_line = f"Current suggestion: inspect/start {task_link} first."
    else:
        next_line = "Current suggestion: no clear task to start; check warnings or items needing your choice."
    return (
        '<div class="panel"><h2>Next-Step Advisor <span class="badge">advice only</span></h2>'
        '<div class="inner scroll queue-scroll">'
        '<p class="headline">This panel answers: if you want to intervene now, which task should you look at first?</p>'
        f'<p>{next_line}</p>'
        f'<p class="muted">It does not launch or change tasks. The real scheduler still rechecks GPU, dependencies, locks, and remote state before doing anything. {_esc(summary)}</p>'
        '<table><tr><th>suggestion</th><th>task</th><th>execution status</th><th>why</th></tr>'
        + "".join(rows)
        + "</table>"
        + filter_hint
        + feedback_hint
        + '<form method="post" action="/action">'
        + '<input type="hidden" name="action" value="advisor_feedback">'
        + '<label>Advisor feedback<textarea name="feedback" rows="3" placeholder="Example: do not prioritize GPU rerun; unblock partial analysis first; hold task X for now.">'
        + _esc(feedback)
        + '</textarea></label>'
        + '<div class="actions"><button>Save Advisor Feedback</button></div>'
        + "</form></div></div>"
    )
    summary = str(advisory.get("summary") or "")
    feedback = _advisor_feedback()
    feedback_hint = f"<p class=\"muted\">Current feedback: {_esc(feedback[:260])}</p>" if feedback.strip() else ""
    return (
        '<div class="panel"><h2>Replan Advisor <span class="badge">display only</span></h2>'
        '<div class="inner scroll queue-scroll">'
        f'<p class="muted">{_esc(summary)} No actions are executed from this panel.</p>'
        '<table><tr><th>kind</th><th>task / candidate</th><th>safe level</th><th>recommendation</th></tr>'
        + "".join(rows)
        + "</table>"
        + feedback_hint
        + '<form method="post" action="/action">'
        + '<input type="hidden" name="action" value="advisor_feedback">'
        + '<label>Reject / rewrite feedback<textarea name="feedback" rows="3" placeholder="例如：不要先跑 GPU rerun；優先解堵 partial analysis；exp_013c SSH 不穩先 hold。">'
        + _esc(feedback)
        + '</textarea></label>'
        + '<div class="actions"><button>Save Feedback For Rewrite</button></div>'
        + "</form></div></div>"
    )


def _render_top_recommendations(plan: Optional[Dict[str, object]]) -> str:
    try:
        advisory = lab.scheduler_advisory(plan)
    except Exception as exc:
        return f'<div class="panel"><h2>Top Recommendations</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'

    candidate_by_id = {
        str(item.get("candidate_id")): item
        for item in advisory.get("candidate_actions", [])
        if isinstance(item, dict)
    }
    hidden = _hidden_attention()
    filtered_count = 0

    def is_visible(kind: str, action: Dict[str, object]) -> bool:
        task_id = str(action.get("task_id") or "")
        if not task_id or task_id in hidden:
            return False
        try:
            record = lab.status(task_id)
        except Exception:
            record = {}
        status = str(record.get("status") or "")
        if status == "completed":
            return False
        action_type = str(action.get("action_type") or "")
        reason = str(action.get("reason_hint") or action.get("reason_code") or "")
        if action_type == "ask_user" and "Partial artifacts exist" in reason:
            if status != "partial":
                return False
            return any(
                str(item.get("status") or "") in {"pending", "running", "launching", "partial", "launch_unknown"}
                for item in _dependent_tasks(task_id)
            )
        if action_type in {"verify_launch", "repair_launch_failed"}:
            return status in {"launching", "launch_unknown", "launch_failed", "running"}
        if kind == "hold" and status in {"failed", "completed"}:
            return False
        return True

    def label_for(kind: str, action: Dict[str, object]) -> tuple[str, str]:
        action_type = str(action.get("action_type") or "")
        if action_type == "start_task":
            return "Inspect / Start Candidate", "running"
        if kind == "ask":
            return "Needs Your Choice", "partial"
        if kind == "hold":
            return "Hold For Now", "partial"
        return "Watch / Verify", "failed"

    def reason_for(action: Dict[str, object]) -> str:
        reason = str(action.get("reason_hint") or action.get("reason_code") or action.get("action_type") or "")
        if action.get("action_type") == "start_task":
            reason = (
                f"{reason or 'ready to start'}; resource={action.get('resource')}; "
                f"stage={action.get('execution_stage')}; priority score={action.get('base_score')}"
            )
        return reason

    visible: List[tuple[str, Dict[str, object]]] = []
    buckets = [
        ("start", [candidate_by_id.get(str(candidate_id)) for candidate_id in advisory.get("recommended_candidate_ids", [])]),
        ("ask", [item for item in advisory.get("questions", []) if isinstance(item, dict)]),
        ("warn", [item for item in advisory.get("warnings", []) if isinstance(item, dict)]),
        ("hold", [item for item in advisory.get("holds", []) if isinstance(item, dict)]),
    ]
    for kind, actions in buckets:
        for action in actions:
            if not isinstance(action, dict):
                continue
            if is_visible(kind, action):
                visible.append((kind, action))
            else:
                filtered_count += 1

    cards = []
    for kind, action in visible[:3]:
        task_id = str(action.get("task_id") or "")
        legal = str(action.get("legal_status") or "")
        legal_text = "scheduler may run it" if legal == "legal" else ("advice only" if legal == "advisory_only" else (legal or "needs check"))
        label, css_class = label_for(kind, action)
        cards.append(
            '<div class="recommendation-card">'
            f'<div><span class="{_esc(css_class)}">{_esc(label)}</span></div>'
            f'<div class="title"><a href="/why?id={urllib.parse.quote(task_id)}">{_esc(task_id)}</a></div>'
            f'<div>{_esc(reason_for(action))}</div>'
            f'<div class="meta">{_esc(legal_text)} · advisor only</div>'
            "</div>"
        )
    if not cards:
        cards.append(
            '<div class="recommendation-card">'
            '<div class="title">No intervention needed</div>'
            '<div class="muted">No current item needs your choice. Use Need Attention for the full problem inbox.</div>'
            "</div>"
        )
    feedback = _advisor_feedback()
    feedback_hint = f"<p class=\"muted\">Current feedback: {_esc(feedback[:260])}</p>" if feedback.strip() else ""
    filter_hint = f'<p class="muted">Filtered {_esc(str(filtered_count))} resolved/history-only advisor item(s).</p>' if filtered_count else ""
    summary = str(advisory.get("summary") or "")
    return (
        '<div class="panel"><h2>Top Recommendations <span class="badge">advisor only</span></h2>'
        '<div class="inner scroll queue-scroll">'
        '<p class="role-note"><strong>Role:</strong> triage only. Shows at most three things worth looking at first. It does not replace Need Attention and does not execute actions.</p>'
        f'<p class="muted">Scheduler still rechecks GPU, dependencies, locks, and remote state before launch. {_esc(summary)}</p>'
        '<div class="recommendation-grid">'
        + "".join(cards)
        + "</div>"
        + filter_hint
        + feedback_hint
        + '<details class="section"><summary>Advisor Feedback</summary><div class="section-body">'
        + '<form method="post" action="/action">'
        + '<input type="hidden" name="action" value="advisor_feedback">'
        + '<label>Advisor feedback<textarea name="feedback" rows="3" placeholder="Example: do not prioritize GPU rerun; unblock partial analysis first; hold task X for now.">'
        + _esc(feedback)
        + '</textarea></label>'
        + '<div class="actions"><button>Save Advisor Feedback</button></div>'
        + "</form></div></details></div></div>"
    )


def _render_gpu_inner(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    try:
        snapshot = (plan or {}).get("gpu_snapshot") if isinstance(plan, dict) else None
        if not isinstance(snapshot, dict):
            snapshot = lab.remote_gpu_snapshot(
                str(config["host"]),
                allowed_gpu_ids=str(config["allowed_gpu_ids"]).split(","),
                max_age_seconds=int(config.get("gpu_snapshot_cache_seconds") or DEFAULT_GPU_SNAPSHOT_CACHE_SECONDS),
                cache_only=True,
            )
        cards: List[str] = []
        min_free = float(config.get("min_free_gpu_mem_gb") or 10)
        for gpu in snapshot.get("gpus", []):
            status = _gpu_status(gpu, min_free)
            free_gb = float(gpu.get("free_gb") or 0)
            total_mb = float(gpu.get("total_mb") or 0)
            used_mb = float(gpu.get("used_mb") or 0)
            total_gb = total_mb / 1024 if total_mb else 0
            used_pct = max(0, min(100, (used_mb / total_mb * 100) if total_mb else 0))
            free_pct = max(0, min(100, (free_gb / total_gb * 100) if total_gb else 0))
            util_pct = max(0, min(100, float(gpu.get("utilization_pct") or 0)))
            memory_class = "memory-ok" if free_gb >= min_free else ("memory-warn" if free_gb >= max(1.0, min_free / 2) else "memory-bad")
            status_class = status.replace(" ", "-")
            own = int(gpu.get("own_compute_app_count") or 0)
            external = int(gpu.get("external_compute_app_count") or 0)
            process_bits = [
                f'<span class="badge {"ok" if own else ""}">own {own}</span>',
                f'<span class="badge {"warn" if external else ""}">external {external}</span>',
            ]
            cards.append(
                f'<div class="gpu-card {_esc(status_class)}">'
                '<div class="gpu-card-head">'
                f'<div><div class="gpu-id">GPU {_esc(gpu.get("index"))}</div><div class="gpu-name">{_esc(gpu.get("name") or "")}</div></div>'
                f'<span class="badge {_esc("ok" if status == "usable" else "warn" if status in {"external occupied", "busy", "shared"} else "bad")}">{_esc(status)}</span>'
                "</div>"
                '<div class="gpu-main">'
                f'<div class="gpu-stat"><div class="label">Free</div><div class="value">{_esc(round(free_gb, 1))} GiB</div><div class="muted">of {_esc(round(total_gb, 1))} GiB</div></div>'
                f'<div class="gpu-stat"><div class="label">Util</div><div class="value">{_esc(round(util_pct))}%</div><div class="muted">nvidia-smi</div></div>'
                "</div>"
                f'<div class="muted">Memory used { _esc(round(used_pct)) }%</div>'
                f'<div class="meter"><div class="meter-fill {memory_class}" style="width:{_esc(round(used_pct))}%"></div></div>'
                f'<div class="muted">Free threshold { _esc(min_free) } GiB · free { _esc(round(free_pct)) }%</div>'
                f'<div class="meter"><div class="meter-fill" style="width:{_esc(round(util_pct))}%"></div></div>'
                '<div class="gpu-process-row">' + "".join(process_bits) + "</div>"
                "</div>"
            )
        table = '<div class="gpu-card-grid">' + "".join(cards) + "</div>"
        cache = snapshot.get("cache") if isinstance(snapshot, dict) else {}
        if isinstance(cache, dict) and cache.get("hit"):
            age = cache.get("age_seconds")
            table += f'<p class="muted">GPU snapshot cache hit; age { _esc(age) } seconds.</p>'
        table += '<p class="muted" data-gpu-live-note>Live GPU refresh updates this panel every 5 seconds from the local cache/watcher.</p>'
    except Exception as exc:
        table = f"<pre>{_esc(type(exc).__name__ + ': ' + str(exc))}</pre>"
    mapping_note = str(config.get("cuda_mapping_note") or "").strip()
    mapping_html = f'<p class="muted">{_esc(mapping_note)}</p>' if mapping_note else ""
    return table + mapping_html


def _render_gpu(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    return f'<div class="panel"><h2>GPU</h2><div class="inner" id="gpu-live-body">{_render_gpu_inner(config, plan)}</div></div>'


def _render_queue() -> str:
    try:
        items = lab.queue()
    except Exception as exc:
        return f'<div class="panel"><h2>Queue</h2><div class="inner"><pre>{_esc(exc)}</pre></div></div>'
    lane_groups: Dict[str, List[Dict[str, object]]] = {
        "ready_front": [],
        "ready": [],
        "blocked": [],
        "normal": [],
    }
    for item in items:
        lane = str(item.get("queue_lane") or "normal")
        if item.get("ready") and lane == "ready_front":
            key = "ready_front"
        elif item.get("ready"):
            key = "ready"
        elif not item.get("ready"):
            key = "blocked"
        else:
            key = "normal"
        lane_groups.setdefault(key, []).append(item)
    rows = []
    for lane_name, lane_items in lane_groups.items():
        if not lane_items:
            continue
        rows.append(f'<tr><th colspan="6">{_esc(lane_name.replace("_", " ").title())}</th></tr>')
        for item in lane_items[:40]:
            dep = item.get("dependency_state") or {}
            blocked = ""
            if isinstance(dep, dict) and not item.get("ready"):
                incomplete = dep.get("incomplete") or []
                missing = dep.get("missing") or []
                blocked = ", ".join(str(x.get("task_id", x)) if isinstance(x, dict) else str(x) for x in list(incomplete) + list(missing))
            task_id = str(item.get("task_id") or "")
            rows.append(
                "<tr>"
                f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
                f"<td>{_esc(item.get('title'))}</td>"
                f"<td>{'yes' if item.get('ready') else 'no'}</td>"
                f"<td>{_esc(item.get('queue_lane') or 'normal')}</td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(blocked)}</div></td>"
                f"<td><a href=\"/why?id={urllib.parse.quote(task_id)}\">Why?</a></td>"
                "</tr>"
            )
    return '<div class="panel"><h2>Queue Lanes</h2><div class="inner scroll queue-scroll"><table><tr><th class="task-col">task</th><th>title</th><th class="ready-col">ready</th><th>lane</th><th>blocked by</th><th>diagnose</th></tr>' + "".join(rows) + "</table></div></div>"


def _schedule_dry_run(config: Dict[str, object]) -> Dict[str, object]:
    return lab.schedule_remote(
        host=str(config["host"]),
        remote_root=str(config["remote_root"]),
        remote_workdir=str(config["remote_workdir"]),
        max_starts=int(config["max_starts_per_cycle"]),
        max_running_total=int(config["max_running_total"]),
        max_running_gpu=int(config["max_running_gpu"]),
        max_running_cpu=int(config["max_running_cpu"]),
        min_free_gpu_mem_gb=float(config["min_free_gpu_mem_gb"]),
        allowed_gpu_ids=str(config["allowed_gpu_ids"]).split(","),
        max_tasks_per_gpu=int(config["max_tasks_per_gpu"]),
        gpu_snapshot_max_age_seconds=int(config.get("gpu_snapshot_cache_seconds") or DEFAULT_GPU_SNAPSHOT_CACHE_SECONDS),
        gpu_snapshot_cache_only=True,
        dry_run=True,
    )


def _render_schedule_plan(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    try:
        plan = plan or _schedule_dry_run(config)
    except Exception as exc:
        return f'<div class="panel"><h2>Schedule Plan</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'
    running_rows = []
    active_records = _records_by_status().get("launching", []) + _records_by_status().get("running", [])
    for record in active_records:
        task_id = str(record.get("task_id") or "")
        meta = _task_meta(task_id, record.get("status"))
        remote = record.get("remote") if isinstance(record.get("remote"), dict) else {}
        runs = record.get("runs") if isinstance(record.get("runs"), list) else []
        latest_run = runs[-1] if runs and isinstance(runs[-1], dict) else {}
        gpu = latest_run.get("gpu") if isinstance(latest_run.get("gpu"), dict) else {}
        nvidia_ids = (
            gpu.get("requested_nvidia_smi_ids")
            or gpu.get("requested_physical_ids")
            or remote.get("gpu_candidates")
            or "-"
        )
        running_rows.append(
            "<tr>"
            f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
            f"<td>{_esc(meta.get('execution_stage') or remote.get('execution_stage') or record.get('execution_stage') or '-')}</td>"
            f"<td>{_esc(meta.get('resource_hint') or remote.get('resource_hint') or record.get('resource_hint') or '-')}</td>"
            f"<td>{_esc(nvidia_ids)}</td>"
            f"<td class=\"muted\"><div class=\"truncate\">{_esc(_run_health(record))}<br>{_esc(record.get('latest_run_id') or latest_run.get('run_id') or '')}</div></td>"
            "</tr>"
        )
    if not running_rows:
        running_rows.append('<tr><td colspan="5" class="muted">No running tasks in registry.</td></tr>')

    next_rows = []
    for item in plan.get("plan", []) or []:
        next_rows.append(
            "<tr>"
            f"<td><a href=\"/task?id={urllib.parse.quote(str(item.get('task_id')))}\">{_esc(item.get('task_id'))}</a></td>"
            f"<td>{_esc(item.get('execution_stage'))}</td>"
            f"<td>{_esc(item.get('resource_hint'))}</td>"
            f"<td>{_esc(item.get('selected_gpu_id') or '-')}</td>"
            f"<td>{_esc(item.get('min_free_gpu_mem_gb') or '-')}</td>"
            "</tr>"
        )
    if not next_rows:
        next_rows.append('<tr><td colspan="5" class="muted">No task would start this cycle.</td></tr>')
    summary = (
        f"capacity total/gpu/cpu: {_esc((plan.get('capacity') or {}).get('total'))}/"
        f"{_esc((plan.get('capacity') or {}).get('gpu'))}/"
        f"{_esc((plan.get('capacity') or {}).get('cpu'))}"
    )
    return (
        '<div class="panel"><h2>Schedule Plan <span class="badge">dry-run</span></h2><div class="inner scroll queue-scroll">'
        f'<p class="muted">{summary}</p>'
        '<h3>Running Now</h3>'
        '<table><tr><th>task</th><th>stage</th><th>resource</th><th>GPU</th><th>health / run</th></tr>'
        + "".join(running_rows)
        + "</table>"
        '<h3>Next To Start</h3>'
        '<table><tr><th>next task</th><th>stage</th><th>resource</th><th>GPU</th><th>min free</th></tr>'
        + "".join(next_rows)
        + "</table></div></div>"
    )


def _render_skipped_waiting(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    try:
        plan = plan or _schedule_dry_run(config)
    except Exception as exc:
        return f'<div class="panel"><h2>Skipped / Waiting</h2><div class="inner"><pre>{_esc(type(exc).__name__ + ": " + str(exc))}</pre></div></div>'
    rows = []
    for item in (plan.get("skipped", []) or [])[:40]:
        wait_type, detail = _skip_attention_type(item)
        rows.append(
            "<tr>"
            f'<td><span class="badge">{_esc(wait_type)}</span></td>'
            f"<td>{_esc(item.get('task_id'))}</td>"
            f"<td>{_esc(detail)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="3" class="muted">Nothing skipped in this dry-run.</td></tr>')
    return (
        '<div class="panel"><h2>Skipped / Waiting</h2><div class="inner scroll queue-scroll">'
        '<table><tr><th>type</th><th>task</th><th>reason</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _wrap_section(title: str, body: str, open_by_default: bool = True) -> str:
    open_attr = " open" if open_by_default else ""
    return f'<details class="section"{open_attr}><summary>{_esc(title)}</summary><div class="section-body">{body}</div></details>'


def _render_active_now() -> str:
    rows = []
    grouped = _records_by_status()
    for record in grouped.get("launching", []) + grouped.get("running", []):
        task_id = str(record.get("task_id") or "")
        meta = _task_meta(task_id, record.get("status"))
        rows.append(
            "<tr>"
            f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
            f"<td>{_esc(meta.get('execution_stage') or '-')}</td>"
            f"<td>{_esc(meta.get('resource_hint') or '-')}</td>"
            f"<td>{_esc(_run_health(record))}</td>"
            f"<td>{_esc(_last_remote_check_text(record))}</td>"
            f"<td>{_esc(_suggested_action(record))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="muted">No active remote tasks.</td></tr>')
    return (
        '<div class="panel"><h2>Active Now</h2><div class="inner scroll queue-scroll">'
        '<table><tr><th>task</th><th>stage</th><th>resource</th><th>health</th><th>last check</th><th>suggested action</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _attention_group_key(task_id: str, record: Dict[str, object]) -> str:
    meta = _task_meta(task_id, record.get("status"))
    group = str(meta.get("parallel_group") or "").strip()
    if group:
        return group
    depends = meta.get("depends_on") or []
    artifacts = meta.get("requires_artifacts_from") or []
    for parent in list(depends) + list(artifacts):
        parent_id = str(parent or "").strip()
        if parent_id:
            parent_meta = _task_meta(parent_id)
            parent_group = str(parent_meta.get("parallel_group") or "").strip()
            if parent_group:
                return parent_group
            return parent_id
    return task_id


def _render_partial_attention_groups(grouped: Dict[str, List[Dict[str, object]]], hidden: set[str]) -> str:
    partial_groups: Dict[str, List[Dict[str, object]]] = {}
    for record in grouped.get("partial", [])[-40:]:
        task_id = str(record.get("task_id") or "")
        if not task_id or task_id in hidden:
            continue
        partial_groups.setdefault(_attention_group_key(task_id, record), []).append(record)
    if not partial_groups:
        return '<p class="muted">No partial tasks need attention.</p>'

    blocks: List[str] = []
    for group_name, records in sorted(partial_groups.items(), key=lambda item: item[0]):
        rows: List[str] = []
        chain_count = 0
        downstream_count = 0
        accepts_partial_count = 0
        for record in sorted(records, key=lambda item: str(item.get("task_id") or "")):
            task_id = str(record.get("task_id") or "")
            guidance = _requeue_guidance(task_id, record)
            scope = str(guidance.get("partial_scope") or "partial")
            if scope == "chain_partial":
                chain_count += 1
            active = guidance.get("active_dependents") if isinstance(guidance.get("active_dependents"), list) else []
            task_accepts_partial_count = sum(1 for item in active if isinstance(item, dict) and item.get("accepts_partial"))
            downstream_count += len(active)
            accepts_partial_count += task_accepts_partial_count
            action = (
                f'<div class="actions"><a class="button" href="/why?id={urllib.parse.quote(task_id)}">Inspect Evidence</a>'
                f'<a class="button" href="/task?id={urllib.parse.quote(task_id)}">Open Task</a></div>'
            )
            rows.append(
                "<tr>"
                f'<td><input type="checkbox" name="task_id" value="{_esc(task_id)}" form="attention-bulk-form"></td>'
                f'<td><a href="/task?id={urllib.parse.quote(task_id)}">{_esc(task_id)}</a></td>'
                f'<td><span class="badge">{_esc(scope)}</span></td>'
                f"<td>{_esc(len(active))}<br><span class=\"muted\">accepts partial: {_esc(task_accepts_partial_count)}</span></td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(_short_summary(record))}</div></td>"
                f"<td>{action}</td>"
                "</tr>"
            )
        if chain_count:
            recommendation = (
                f"{chain_count} chain partial; inspect as a pipeline before rerun. "
                f"Active downstream links: {downstream_count}; accepts partial: {accepts_partial_count}."
            )
            badge = "pipeline"
            open_attr = " open"
        else:
            recommendation = "Single-task partial group; no active downstream dependency was detected."
            badge = "single"
            open_attr = ""
        hide_group_form = (
            '<form method="post" action="/action" '
            f'onsubmit="return confirm(\'Hide all partial tasks in group { _esc(group_name) } from Need Attention? Files and registry will not be deleted.\')">'
            '<input type="hidden" name="action" value="hide_attention_group">'
            + "".join(
                f'<input type="hidden" name="task_id" value="{_esc(str(record.get("task_id") or ""))}">'
                for record in records
                if record.get("task_id")
            )
            + '<button class="warn">Hide This Partial Group</button>'
            "</form>"
        )
        blocks.append(
            f'<details class="attention-group"{open_attr}>'
            f'<summary>{_esc(group_name)} <span class="badge">{_esc(len(records))}</span> <span class="badge">{_esc(badge)}</span></summary>'
            '<div class="group-body">'
            f'<p class="mini-note">{_esc(recommendation)} Requeue buttons are intentionally only on the Why page.</p>'
            f'<div class="actions" style="margin-bottom:8px">{hide_group_form}</div>'
            '<table><tr><th></th><th>task</th><th>partial type</th><th>downstream</th><th>summary</th><th>decision</th></tr>'
            + "".join(rows)
            + "</table></div></details>"
        )
    return "".join(blocks)


def _render_attention_panel(config: Dict[str, object], plan: Optional[Dict[str, object]] = None) -> str:
    grouped = _records_by_status()
    hidden = _hidden_attention()
    partial_groups = _render_partial_attention_groups(grouped, hidden)
    rows = []
    for status in ("launch_failed", "failed"):
        for record in grouped.get(status, [])[-20:]:
            task_id = str(record.get("task_id") or "")
            if task_id in hidden:
                continue
            action = _suggested_action(record)
            hide_action = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Hide this task from Need Attention? Files and registry will not be deleted.\')">'
                '<input type="hidden" name="action" value="hide_attention_task">'
                f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
                '<button class="warn">Hide</button>'
                "</form>"
            )
            rows.append(
                "<tr>"
                f'<td><input type="checkbox" name="task_id" value="{_esc(task_id)}" form="attention-bulk-form"></td>'
                f"<td><span class=\"{status}\">{_esc(status)}</span></td>"
                f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
                f"<td>{action}</td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(_short_summary(record))}</div></td>"
                f"<td>{hide_action}</td>"
                "</tr>"
            )
    skipped = []
    if isinstance(plan, dict):
        skipped = plan.get("skipped", []) or []
    for item in skipped[:20]:
        task_id = str(item.get("task_id") or "")
        if task_id in hidden:
            continue
        wait_type, detail = _skip_attention_type(item)
        hide_action = (
            '<form method="post" action="/action" onsubmit="return confirm(\'Hide this blocked item from Need Attention? The task will stay in its queue.\')">'
            '<input type="hidden" name="action" value="hide_attention_task">'
            f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
            '<button class="warn">Hide</button>'
            "</form>"
        )
        rows.append(
            "<tr>"
            f'<td><input type="checkbox" name="task_id" value="{_esc(task_id)}" form="attention-bulk-form"></td>'
            f'<td><span class="badge">{_esc(wait_type)}</span></td>'
            f"<td><a href=\"/why?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
            "<td>查看原因</td>"
            f"<td class=\"muted\"><div class=\"truncate\">{_esc(detail)}</div></td>"
            f"<td>{hide_action}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="muted">No failed or blocked tasks need attention right now.</td></tr>')
    restore = ""
    if hidden:
        restore = (
            '<div class="actions" style="margin-bottom:10px">'
            f'<span class="badge">{len(hidden)} hidden</span>'
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="show_attention">'
            '<button>Show Hidden Attention</button>'
            "</form></div>"
        )
    return (
        '<div class="panel"><h2>Decision / Attention Inbox</h2><div class="inner scroll queue-scroll">'
        + restore
        + '<p class="muted">Items here need human strategy or inspection before the system should continue.</p>'
        '<form id="attention-bulk-form" method="post" action="/action" '
        'onsubmit="return confirm(\'Hide selected tasks from Need Attention? Files and registry will not be deleted.\') && confirm(\'Second confirmation: hide selected attention items?\')">'
        '<input type="hidden" name="action" value="bulk_hide_attention">'
        '<div class="actions" style="margin-bottom:10px"><button class="warn">Hide Selected</button></div></form>'
        + '<h3>Partial Decisions</h3>'
        + partial_groups
        + '<h3>Failed / Blocked / Waiting</h3>'
        + '<table><tr><th></th><th>type</th><th>task</th><th>suggested action</th><th>reason / summary</th><th>hide</th></tr>'
        + "".join(rows)
        + "</table></div></div>"
    )


def _all_board_tasks() -> Dict[str, Dict[str, object]]:
    tasks: Dict[str, Dict[str, object]] = {}
    for task_id, record in (load_registry().get("tasks", {}) or {}).items():
        if isinstance(record, dict):
            tasks[str(task_id)] = dict(record)
    try:
        for item in lab.queue():
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            record = tasks.setdefault(task_id, {})
            record.setdefault("task_id", task_id)
            record.setdefault("title", item.get("title"))
            record.setdefault("status", "pending")
            record["queue_ready"] = item.get("ready")
            record["dependency_state"] = item.get("dependency_state")
    except Exception:
        pass
    return tasks


def _stage_order(stage: object) -> int:
    return {
        "prepare": 10,
        "run": 20,
        "eval": 30,
        "evaluation": 30,
        "analysis": 40,
        "summary": 50,
    }.get(str(stage or "").lower(), 99)


def _group_rank(group: str, items: List[Dict[str, object]]) -> int:
    statuses = {str(item.get("status") or "pending") for item in items}
    active = bool(statuses & {"running", "pending", "failed"})
    if active:
        return 0
    if len(items) >= 3:
        return 1
    if any(word in group for word in ("summary", "analysis", "eval")):
        return 3
    return 2


def _group_label(group: str) -> str:
    if group.startswith("df40_"):
        return "DF40 frame-8 video-AUC"
    if group.startswith("recipe_ablation"):
        return "Recipe ablation"
    if group.startswith("degradation_scan"):
        return "Degradation scan"
    if group.startswith("benchmark_eval"):
        return "Canonical benchmark eval"
    if "summary" in group:
        return "Summary / analysis"
    return group.replace("_", " ")


def _infer_inbox_line(task_id: str, meta: Dict[str, object]) -> str:
    group = str(meta.get("parallel_group") or "").strip()
    if group:
        return group
    cleaned = re.sub(r"^exp_\d+[a-z]?_", "", task_id)
    cleaned = re.sub(r"^(prepare|run|analysis|eval|summary)_", "", cleaned)
    return cleaned or "ungrouped_inbox"


def _inbox_task_files() -> List[Path]:
    inbox = root() / "tasks" / "inbox"
    if not inbox.exists():
        return []
    files: List[Path] = []
    for path in sorted(inbox.glob("*.md")):
        if not lab._should_ignore_inbox_file(path):  # type: ignore[attr-defined]
            files.append(path)
    return files


def _inbox_line_status(record: Dict[str, object]) -> str:
    status = str(record.get("status") or "inbox_only")
    if status == "pending" and record.get("queue_ready") is False:
        return "blocked"
    return status


def _inbox_line_score(items: List[Dict[str, object]]) -> int:
    status_weight = {
        "running": 100,
        "launching": 95,
        "launch_unknown": 90,
        "failed": 88,
        "launch_failed": 88,
        "partial": 82,
        "blocked": 72,
        "ready_front": 68,
        "pending": 62,
        "inbox_only": 45,
        "completed": 8,
    }
    score = 0
    for item in items:
        score = max(score, status_weight.get(_inbox_line_status(item), 30))
        meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
        if str(meta.get("priority") or "").lower() == "high":
            score += 4
        if str(meta.get("resource_hint") or "").lower() == "gpu":
            score += 2
        if str(meta.get("execution_stage") or "").lower() in {"run", "eval", "evaluation"}:
            score += 2
    return score


def _staging_kind_badge(kind: str) -> str:
    pretty = {"folder": "folder", "zip": "zip", "loose_index": "legacy-loose"}.get(kind, kind or "?")
    return f'<span class="badge">{_esc(pretty)}</span>'


def _staging_task_row(line_name: str, task: Dict[str, object]) -> str:
    task_id = str(task.get("task_id") or "")
    title = str(task.get("title") or task_id)
    status = str(task.get("status") or "inbox_only")
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    stage = str((meta or {}).get("execution_stage") or "-")
    resource = str((meta or {}).get("resource_hint") or "-")
    priority = str((meta or {}).get("priority") or "-")
    load_error = bool(task.get("load_error"))
    lint = task.get("lint") if isinstance(task.get("lint"), dict) else {}
    lint_errors = [str(item) for item in (lint.get("errors") or [])] if isinstance(lint.get("errors"), list) else []
    lint_warnings = [str(item) for item in (lint.get("warnings") or [])] if isinstance(lint.get("warnings"), list) else []
    lint_html = ""
    if lint_errors or lint_warnings:
        parts = []
        if lint_errors:
            parts.append('<div class="failed">errors: ' + _esc("; ".join(lint_errors)) + "</div>")
        if lint_warnings:
            parts.append('<div class="muted">warnings: ' + _esc("; ".join(lint_warnings)) + "</div>")
        lint_html = "".join(parts)

    # Promote button: disabled when source could not be parsed.
    if load_error:
        promote_btn = '<span class="badge">cannot promote</span>'
    elif lint_errors:
        promote_btn = '<span class="badge failed">blocked</span>'
    else:
        promote_btn = (
            '<form method="post" action="/action" style="display:inline">'
            '<input type="hidden" name="action" value="inbox_promote_task">'
            f'<input type="hidden" name="line" value="{_esc(line_name)}">'
            f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
            '<button class="start">Promote</button>'
            "</form>"
        )

    discard_btn = (
        '<form method="post" action="/action" style="display:inline" '
        'onsubmit="return confirm(\'Move this task to tasks/inbox/_discarded/ ?\')">'
        '<input type="hidden" name="action" value="inbox_discard_task">'
        f'<input type="hidden" name="line" value="{_esc(line_name)}">'
        f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
        '<button class="stop">Discard</button>'
        "</form>"
    )

    return (
        "<tr>"
        f"<td><strong>{_esc(task_id)}</strong><div class=\"muted\">{_esc(title)}</div>{lint_html}</td>"
        f"<td><span class=\"{_esc(status)}\">{_esc(status)}</span></td>"
        f"<td>{_esc(stage)}</td>"
        f"<td>{_esc(resource)}</td>"
        f"<td>{_esc(priority)}</td>"
        f"<td>{promote_btn} {discard_btn}</td>"
        "</tr>"
    )


def _staging_line_card(line: Dict[str, object]) -> str:
    name = str(line.get("name") or "")
    line_key = str(line.get("line_key") or name)
    kind = str(line.get("kind") or "")
    index_name = str(line.get("index_name") or "TASK_BATCH_INDEX.md")
    tasks = line.get("tasks") or []
    note = str(line.get("note") or "")
    is_empty_loose_index = kind == "loose_index" and not tasks

    promote_all_btn = (
        (
            '<form method="post" action="/action" style="display:inline" '
            'onsubmit="return confirm(\'Promote ALL task books in this line into pending?\')">'
            '<input type="hidden" name="action" value="inbox_promote_line">'
            f'<input type="hidden" name="line" value="{_esc(line_key)}">'
            '<button class="start">Promote all</button>'
            "</form>"
        )
        if tasks
        else ""
    )
    discard_all_btn = (
        '<form method="post" action="/action" style="display:inline" '
        'onsubmit="return confirm(\'Discard this inbox line? It will be moved to tasks/inbox/_discarded/.\')">'
        '<input type="hidden" name="action" value="inbox_discard_line">'
        f'<input type="hidden" name="line" value="{_esc(line_key)}">'
        f'<button class="stop">{_esc("Discard empty index" if is_empty_loose_index else "Discard all")}</button>'
        "</form>"
    )

    if tasks:
        rows = "".join(_staging_task_row(line_key, dict(t)) for t in tasks)
        body = (
            '<table class="dense"><thead><tr>'
            "<th>Task</th><th>Status</th><th>Stage</th><th>Resource</th><th>Priority</th><th>Action</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
    else:
        if is_empty_loose_index:
            body = (
                '<p class="muted">This is an empty legacy inbox index at the inbox root. '
                'There are no task books to promote; discarding it only moves the index to '
                '<code>tasks/inbox/_discarded/</code> so the dashboard stops showing it.</p>'
            )
        else:
            body = f'<p class="muted">No task books attached to this line. {_esc(note) if note else ""}</p>'

    header = (
        f'<div class="title">{_esc(name)}</div>'
        f'<div class="muted">{_staging_kind_badge(kind)} index: {_esc(index_name)} | '
        f'{_esc("housekeeping" if is_empty_loose_index else str(len(tasks)) + " task book(s)")}</div>'
    )
    actions_row = f'<p>{promote_all_btn} {discard_all_btn}</p>'

    return (
        '<div class="recommendation-card" style="grid-column: 1 / -1">'
        + header
        + actions_row
        + body
        + "</div>"
    )


def _staging_anomaly_card(anomaly: Dict[str, object]) -> str:
    name = str(anomaly.get("name") or "")
    kind = str(anomaly.get("kind") or "")
    reason = str(anomaly.get("reason") or "unknown")
    path = str(anomaly.get("path") or "")
    discard_btn = (
        '<form method="post" action="/action" style="display:inline" '
        'onsubmit="return confirm(\'Move this anomaly to tasks/inbox/_discarded/ ?\')">'
        '<input type="hidden" name="action" value="inbox_discard_anomaly">'
        f'<input type="hidden" name="line" value="{_esc(name)}">'
        '<button class="stop">Discard</button>'
        "</form>"
    )
    return (
        '<div class="recommendation-card" style="grid-column: 1 / -1">'
        f'<div class="title">⚠ {_esc(name)}</div>'
        f'<div class="muted">{_staging_kind_badge(kind)} reason: {_esc(reason)}</div>'
        f'<p class="muted">Path: {_esc(path)}</p>'
        '<p class="muted">A line is only valid if its container holds at least one TASK_BATCH_INDEX*.md. '
        'Add the index file (or move tasks under one) and refresh, or discard.</p>'
        f'<p>{discard_btn}</p>'
        "</div>"
    )


def _render_inbox_lines() -> str:
    try:
        state = lab.scan_inbox_staging()
    except Exception as exc:
        return (
            '<div class="panel"><h2>Inbox Staging</h2><div class="inner">'
            f'<p class="muted">Could not scan inbox staging: {_esc(exc)}</p>'
            "</div></div>"
        )
    lines = state.get("lines") or []
    anomalies = state.get("anomalies") or []

    if not lines and not anomalies:
        return (
            '<div class="panel"><h2>Inbox Staging</h2><div class="inner">'
            '<p class="muted">No staging containers in tasks/inbox/. '
            'Drop a folder or zip that contains a TASK_BATCH_INDEX*.md to register a new line.</p>'
            "</div></div>"
        )

    blocks: List[str] = []
    if lines:
        actionable_count = sum(1 for line in lines if line.get("tasks"))
        cards = "".join(_staging_line_card(dict(line)) for line in lines)
        blocks.append(
            '<div class="panel"><h2>Inbox Staging '
            f'<span class="badge">{_esc(actionable_count)} actionable / {_esc(len(lines))} scanned</span></h2>'
            '<div class="inner scroll queue-scroll">'
            '<p class="muted">Each card is one batch container (folder or zip) holding a TASK_BATCH_INDEX*.md. '
            'Promote moves a task book into <code>tasks/pending/</code> and archives the index + task into '
            '<code>tasks/inbox/processed/&lt;index_root&gt;/</code>. Discard moves to '
            '<code>tasks/inbox/_discarded/</code> (recoverable).</p>'
            '<div class="recommendation-grid">'
            + cards
            + "</div></div></div>"
        )
    if anomalies:
        cards = "".join(_staging_anomaly_card(dict(a)) for a in anomalies)
        blocks.append(
            '<div class="panel"><h2>⚠ Inbox Anomalies '
            f'<span class="badge">{_esc(len(anomalies))} item(s)</span></h2>'
            '<div class="inner scroll queue-scroll">'
            '<p class="muted">These containers do not hold a TASK_BATCH_INDEX*.md, so the dashboard '
            'cannot promote them as a line. Fix the container (add an index) or discard.</p>'
            '<div class="recommendation-grid">'
            + cards
            + "</div></div></div>"
        )
    return "".join(blocks)


def _render_pipeline_view() -> str:
    hidden = _hidden_completed()
    hidden_attention = _hidden_attention()
    groups: Dict[str, List[Dict[str, object]]] = {}
    for task_id, record in _all_board_tasks().items():
        if task_id in hidden:
            continue
        if task_id in hidden_attention and str(record.get("status") or "") in {"launch_failed", "failed", "partial"}:
            continue
        meta = _task_meta(task_id, record.get("status"))
        group = str(meta.get("parallel_group") or "ungrouped")
        if group == "ungrouped" and not str(task_id).startswith("exp_"):
            continue
        item = dict(record)
        item["_meta"] = meta
        groups.setdefault(group, []).append(item)

    group_blocks = []
    compact_rows = []
    for group, items in sorted(groups.items(), key=lambda pair: (_group_rank(pair[0], pair[1]), pair[0])):
        items.sort(key=lambda item: (_stage_order((item.get("_meta") or {}).get("execution_stage")), str(item.get("created_at") or ""), str(item.get("task_id") or "")))
        rows = []
        for item in items:
            task_id = str(item.get("task_id") or "")
            meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
            deps = meta.get("depends_on") or []
            if isinstance(deps, list):
                deps_text = ", ".join(str(dep) for dep in deps)
            else:
                deps_text = str(deps)
            status = str(item.get("status") or "pending")
            rows.append(
                "<tr>"
                f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
                f"<td>{_esc(meta.get('execution_stage') or '-')}</td>"
                f"<td><span class=\"{_esc(status)}\">{_esc(status)}</span></td>"
                f"<td>{_esc(meta.get('resource_hint') or '-')}</td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(deps_text)}</div></td>"
                "</tr>"
            )
        rank = _group_rank(group, items)
        statuses = {}
        for item in items:
            status = str(item.get("status") or "pending")
            statuses[status] = statuses.get(status, 0) + 1
        status_text = ", ".join(f"{key}:{value}" for key, value in sorted(statuses.items()))
        if rank <= 1:
            open_attr = " open" if any(str(item.get("status") or "") in {"running", "failed"} for item in items) else ""
            group_blocks.append(
                f'<details class="section"{open_attr}>'
                f'<summary>{_esc(_group_label(group))} <span class="badge">{len(items)}</span> <span class="muted">{_esc(status_text)}</span></summary>'
                '<div class="section-body">'
                f'<p class="muted">group: {_esc(group)}</p>'
                '<div class="scroll queue-scroll"><table><tr><th>task</th><th>stage</th><th>status</th><th>resource</th><th>depends on</th></tr>'
                + "".join(rows)
                + "</table></div></div></details>"
            )
        else:
            sample = ", ".join(str(item.get("task_id") or "") for item in items[:3])
            compact_rows.append(
                "<tr>"
                f"<td>{_esc(_group_label(group))}<br><span class=\"muted\">{_esc(group)}</span></td>"
                f"<td>{_esc(len(items))}</td>"
                f"<td>{_esc(status_text)}</td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(sample)}</div></td>"
                "</tr>"
            )
    if not group_blocks:
        group_blocks.append('<p class="muted">No active or large pipelines to show.</p>')
    compact_table = (
        '<details class="section"><summary>Compact / Minor Groups <span class="badge">'
        + _esc(len(compact_rows))
        + '</span></summary><div class="section-body"><div class="scroll queue-scroll">'
        + '<table><tr><th>group</th><th>tasks</th><th>status mix</th><th>sample</th></tr>'
        + ("".join(compact_rows) if compact_rows else '<tr><td colspan="4" class="muted">No compact groups.</td></tr>')
        + "</table></div></div></details>"
    )
    return (
        '<div class="panel"><h2>Pipeline by Batch</h2><div class="inner">'
        + "".join(group_blocks[:8])
        + compact_table
        + "</div></div>"
    )


def _render_records() -> str:
    grouped = _records_by_status()
    hidden = _hidden_completed()
    hidden_attention = _hidden_attention()
    wanted = ["launching", "running", "partial", "launch_failed", "failed", "completed"]
    panels = []
    for status in wanted:
        rows = []
        bulk_form = ""
        for record in grouped.get(status, [])[-30:]:
            task_id = str(record.get("task_id"))
            if status == "completed" and task_id in hidden:
                continue
            if status in {"partial", "launch_failed", "failed"} and task_id in hidden_attention:
                continue
            health = _run_health(record) if status in {"launching", "running", "launch_failed"} else status
            result_signal = _result_summary(task_id) if status == "completed" else _suggested_action(record)
            action_cell = _requeue_partial_form(task_id) if status == "partial" else _esc(result_signal)
            select_cell = f'<td><input type="checkbox" name="task_id" value="{_esc(task_id)}" form="completed-bulk-form"></td>' if status == "completed" else ""
            rows.append(
                "<tr>"
                + select_cell
                + f"<td><a href=\"/task?id={urllib.parse.quote(task_id)}\">{_esc(task_id)}</a></td>"
                f"<td>{_esc(record.get('title'))}</td>"
                f"<td>{_esc(health)}<br><span class=\"muted\">{_esc(record.get('started_at') or record.get('finished_at') or '')}</span></td>"
                f"<td class=\"muted\"><div class=\"truncate\">{action_cell}</div></td>"
                f"<td class=\"muted\"><div class=\"truncate\">{_esc(_short_summary(record))}</div></td>"
                "</tr>"
            )
        if status == "completed":
            bulk_form = (
                '<form id="completed-bulk-form" method="post" action="/action" '
                'onsubmit="return confirm(\'Hide selected completed tasks from the board? Results will not be deleted.\') && confirm(\'Second confirmation: hide selected completed tasks?\')">'
                '<input type="hidden" name="action" value="bulk_hide_completed">'
                '<div class="actions" style="margin-bottom:8px"><button class="warn">Hide Selected Completed</button></div></form>'
            )
            header = '<tr><th></th><th class="task-col">task</th><th class="title-col">title</th><th class="time-col">health / time</th><th>result / action</th><th class="summary-col">summary</th></tr>'
        else:
            header = '<tr><th class="task-col">task</th><th class="title-col">title</th><th class="time-col">health / time</th><th>result / action</th><th class="summary-col">summary</th></tr>'
        panels.append(
            f'<div class="panel"><h2><span class="{status}">{_esc(status)}</span> <span class="badge">{len(grouped.get(status, []))}</span></h2>'
            '<div class="inner scroll status-scroll">'
            + bulk_form
            + "<table>"
            + header
            + "".join(rows)
            + "</table></div></div>"
        )
    return '<div class="status-grid">' + "".join(panels) + "</div>"


def _render_dashboard(config: Dict[str, object], params: Optional[Dict[str, List[str]]] = None) -> bytes:
    try:
        plan = _schedule_dry_run(config)
    except Exception:
        plan = None
    body = (
        '<div class="dashboard-stack">'
        + _render_health_summary(config, plan)
        + _render_inbox_lines()
        + _render_attention_panel(config, plan)
        + _render_top_recommendations(plan)
        + _render_scheduling_decisions(config, plan)
        + '<div class="responsive-pair">'
        + _render_schedule_plan(config, plan)
        + _render_gpu(config, plan)
        + "</div>"
        + _render_workplaces(config)
        + _render_remote_runs(config, params)
        + _wrap_section("Controls / Remote Target", _render_scheduler(config) + _render_remote_target(config), open_by_default=True)
        + _wrap_section("Action Requests", _render_action_request_history(), open_by_default=True)
        + _render_queue()
        + _wrap_section("Active Runs", _render_active_now(), open_by_default=False)
        + _wrap_section("Skipped / Waiting Details", _render_skipped_waiting(config, plan), open_by_default=False)
        + _wrap_section("Blocked / Unblock Planner", _render_unblock_planner(plan), open_by_default=True)
        + _wrap_section("Dependency Blockers", _render_blocker_priority(), open_by_default=False)
        + _wrap_section("Pipeline", _render_pipeline_view(), open_by_default=False)
        + _wrap_section("Completed / Metrics / Full Status", _render_records(), open_by_default=False)
        + "</div>"
    )
    return _render_page("Solution Agent Dashboard", body)


def _render_task(task_id: str) -> bytes:
    try:
        record = lab.status(task_id)
    except Exception as exc:
        return _render_page("Task error", f"<pre>{_esc(exc)}</pre>", refresh=False)
    status = str(record.get("status") or "unknown")
    paths = _latest_report_paths(task_id)
    report = _read_text(paths["report.md"], limit=50000)
    notes = _read_text(paths["worker_notes.md"], limit=30000)
    result = _read_text(paths["result.yaml"], limit=20000)
    stdout = _tail(paths["stdout.log"])
    stderr = _tail(paths["stderr.log"])
    supervision = json.dumps(record.get("last_supervision") or {}, indent=2, ensure_ascii=False)
    partial_action = _requeue_partial_form(task_id) if status == "partial" else ""
    task_snapshot = _task_book_snapshot(task_id, status)
    result_ai = _read_result_ai(task_id)
    result_ai_panel = (
        '<div class="panel"><h2>AI Task + Result Summary</h2><div class="inner">'
        '<p class="muted">Reads task.md first, then compares it with report/result/metrics. Cached locally; press the button to regenerate.</p>'
        f'<pre>{_esc(result_ai or "No AI result summary yet. Press the button to generate one from the task book and current reports.")}</pre>'
        '<form method="post" action="/action">'
        '<input type="hidden" name="action" value="summarize_result_ai">'
        f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
        '<button>Summarize Task + Result With AI</button>'
        '</form></div></div>'
    )
    body = f"""
<div class="panel"><h2>{_esc(task_id)}</h2><div class="inner">
  <p>Status: <strong class="{_esc(status)}">{_esc(status)}</strong></p>
  <p class="muted">Title: {_esc(record.get('title'))}</p>
  <p class="muted">Run: {_esc(record.get('latest_run_id'))}</p>
  <div class="actions">
    <form method="post" action="/action"><input type="hidden" name="action" value="supervise_once"><button>Supervise/Collect Once</button></form>
    {partial_action}
  </div>
  <div class="danger-zone">
    <h3>Danger Zone</h3>
    <form method="post" action="/action" onsubmit="return confirm('Stop remote task { _esc(task_id) }?')"><input type="hidden" name="action" value="remote_stop"><input type="hidden" name="task_id" value="{_esc(task_id)}"><button class="danger">Remote Stop This Task</button></form>
  </div>
</div></div>
<div class="panel"><h2>Task Book Snapshot</h2><div class="inner"><pre>{_esc(task_snapshot)}</pre></div></div>
{result_ai_panel}
<div class="grid">
  <div class="panel"><h2>Current Diagnosis</h2><div class="inner"><pre>{_esc(supervision)}</pre></div></div>
  <div class="panel"><h2>Result YAML</h2><div class="inner"><pre>{_esc(result or 'No result.yaml yet.')}</pre></div></div>
</div>
<div class="grid">
  <div class="panel"><h2>Worker Notes</h2><div class="inner"><pre>{_esc(notes or 'No worker notes yet.')}</pre></div></div>
  <div class="panel"><h2>Report</h2><div class="inner"><pre>{_esc(report or 'No report yet.')}</pre></div></div>
</div>
<div class="grid">
  <div class="panel"><h2>stderr tail</h2><div class="inner"><pre>{_esc(stderr or 'No local stderr yet.')}</pre></div></div>
  <div class="panel"><h2>stdout tail</h2><div class="inner"><pre>{_esc(stdout or 'No local stdout yet.')}</pre></div></div>
</div>
"""
    return _render_page(task_id, body)


def _why_diagnosis(
    task_id: str,
    record: Dict[str, object],
    queue_item: Optional[Dict[str, object]],
    deps: Dict[str, object],
    skipped: Optional[Dict[str, object]],
    plan: Dict[str, object],
) -> Dict[str, object]:
    status = str(record.get("status") or "not registered")
    ready = queue_item.get("ready") if isinstance(queue_item, dict) else None
    scheduler_reason = _human_skip_reason(skipped or {}) if skipped else "currently not skipped by the dry-run scheduler"
    incomplete = deps.get("incomplete", []) or []
    missing = deps.get("missing", []) or []
    missing_artifacts = deps.get("missing_artifacts", []) or []
    allow_partial = set(str(item) for item in (deps.get("allow_partial_dependencies", []) or []))
    allow_partial_artifacts = set(str(item) for item in (deps.get("allow_partial_artifacts_from", []) or []))
    next_plan = plan.get("plan", []) or []

    reasons: List[str] = []
    advice: List[str] = []
    severity = "info"

    if status == "partial":
        severity = "warning"
        summary = "這個任務目前是 partial：代表它有某些結果或狀態已經回來，但還不能被系統當成完整成功。"
        short = _short_summary(record, limit=260)
        if short:
            reasons.append(f"任務自己的摘要是：{short}")
        reasons.append("partial 可能是中途停止、結果合約不完整、只跑出部分 artifact，或人工暫停後留下的狀態。")
        advice.append("如果你想要把這一條重新完整跑完，優先看它是不是 pipeline 的一環；不是單獨任務時，不要只重排單個節點，應該重排/接續整條相關任務。")
        advice.append("如果 partial 的 metrics/worker_notes 已足夠給後續 analysis 使用，可以先讓下游 analysis 吃 partial artifact，再決定是否 GPU rerun。")
    elif ready is True:
        summary = "這個任務已經 ready；如果它還沒跑，通常是排程容量、GPU、source-code lock 或 scheduler 尚未啟動造成。"
        if next_plan:
            planned_ids = [str(item.get("task_id") or "") for item in next_plan if isinstance(item, dict)]
            if task_id in planned_ids:
                reasons.append("目前 dry-run 已把它放入下一批可啟動任務。")
            else:
                reasons.append("它 ready，但不在下一批 plan 裡，可能被優先級或本輪 max starts 限制擋住。")
        advice.append("可以按 Schedule Once，或等待 background scheduler 下一輪處理。")
    elif status == "running":
        summary = "這個任務已經在跑；不是未啟動。Why 頁面只是在解釋它為什麼不能被當成完成依賴。"
        reasons.append("running 任務不能作為 completed dependency；下游要等 result.yaml/artifacts 被 collect 並驗證。")
        advice.append("繼續監控 heartbeat/log；完成後讓 collector 回收，再解開下游任務。")
    elif missing or incomplete or missing_artifacts:
        severity = "warning"
        summary = "這個任務被 dependency 或 artifact 擋住了；不是 GPU 空不空的問題。"
        if missing:
            reasons.append("缺少前置任務：" + ", ".join(str(item) for item in missing))
        if incomplete:
            labels = []
            for item in incomplete:
                if isinstance(item, dict):
                    labels.append(f"{item.get('task_id')}({item.get('status')})")
                else:
                    labels.append(str(item))
            reasons.append("前置任務還沒完整完成：" + ", ".join(labels))
        if missing_artifacts:
            reasons.append("缺少必要 artifact：" + ", ".join(str(item) for item in missing_artifacts))
        if allow_partial or allow_partial_artifacts:
            reasons.append("任務書允許部分 partial input，但目前仍有狀態或 artifact 不滿足條件。")
        advice.append("先處理 blocked by 裡最上游的任務：collect 已完成的、修復 failed/partial、或明確允許下游使用 partial artifact。")
    else:
        summary = "目前沒有明確 dependency blocker；主要看 scheduler dry-run reason。"
        reasons.append(f"Scheduler reason: {scheduler_reason}")
        advice.append("如果這裡看起來不合理，可以先按 Refresh GPU Now 或 Supervise/Collect Once，再重新打開 Why。")

    if skipped:
        reason_code = str(skipped.get("reason") or "")
        if reason_code == "dependencies_not_ready":
            reasons.append("排程器判斷 dependencies_not_ready：依賴或 artifact 還沒滿足。")
        elif reason_code == "no_gpu_with_required_free_memory":
            reasons.append("排程器沒有找到符合門檻的 GPU。")
            advice.append("這種情況通常等 GPU watcher 更新或 GPU 釋放後，下一輪 scheduler 會再試。")
        elif reason_code:
            reasons.append(f"排程器理由碼：{reason_code}")

    return {
        "task_id": task_id,
        "severity": severity,
        "summary": summary,
        "reasons": reasons or ["目前沒有更多具體原因。"],
        "advice": advice or ["先看 raw dry-run；如果還是不清楚，可以點 AI Explain 讓小模型重寫成人話。"],
        "status": status,
        "ready": ready,
        "scheduler_reason": scheduler_reason,
    }


def _render_why_plain(diagnosis: Dict[str, object]) -> str:
    reasons = "".join(f"<li>{_esc(item)}</li>" for item in diagnosis.get("reasons", []) or [])
    advice = "".join(f"<li>{_esc(item)}</li>" for item in diagnosis.get("advice", []) or [])
    severity = str(diagnosis.get("severity") or "info")
    return (
        f'<div class="panel"><h2>白話解釋 <span class="badge">{_esc(severity)}</span></h2><div class="inner">'
        f'<p class="headline">{_esc(diagnosis.get("summary"))}</p>'
        '<h3>原因</h3><ul>'
        + reasons
        + '</ul><h3>下一步建議</h3><ul>'
        + advice
        + "</ul></div></div>"
    )


def _dependent_tasks(task_id: str) -> List[Dict[str, object]]:
    dependents: List[Dict[str, object]] = []
    for status, records in _records_by_status().items():
        for record in records:
            child_id = str(record.get("task_id") or "")
            if not child_id or child_id == task_id:
                continue
            meta = _task_meta(child_id, status)
            depends_on = [str(item) for item in (meta.get("depends_on", []) or [])]
            artifacts_from = [str(item) for item in (meta.get("requires_artifacts_from", []) or [])]
            allow_partial = [str(item) for item in (meta.get("allow_partial_dependencies", []) or [])]
            allow_partial_artifacts = [str(item) for item in (meta.get("allow_partial_artifacts_from", []) or [])]
            if task_id not in depends_on and task_id not in artifacts_from:
                continue
            relation = []
            if task_id in depends_on:
                relation.append("depends_on")
            if task_id in artifacts_from:
                relation.append("requires_artifacts")
            dependents.append(
                {
                    "task_id": child_id,
                    "title": record.get("title") or "",
                    "status": status,
                    "relation": ", ".join(relation),
                    "accepts_partial": task_id in allow_partial or task_id in allow_partial_artifacts,
                }
            )
    dependents.sort(key=lambda item: (str(item.get("status")), str(item.get("task_id"))))
    return dependents


def _dependent_chain(task_id: str, max_depth: int = 5) -> List[Dict[str, object]]:
    chain: List[Dict[str, object]] = []
    queue: List[tuple[str, int]] = [(task_id, 0)]
    seen = {task_id}
    while queue:
        parent_id, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for item in _dependent_tasks(parent_id):
            child_id = str(item.get("task_id") or "")
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            entry = dict(item)
            entry["parent_task_id"] = parent_id
            entry["depth"] = depth + 1
            chain.append(entry)
            queue.append((child_id, depth + 1))
    return chain


def _requeue_guidance(task_id: str, record: Dict[str, object]) -> Dict[str, object]:
    status = str(record.get("status") or "not registered")
    dependents = _dependent_tasks(task_id)
    active_dependents = [item for item in dependents if item.get("status") in {"pending", "running", "launching", "partial"}]
    partial_consumers = [item for item in active_dependents if item.get("accepts_partial")]
    reasons: List[str] = []
    recommendation = "hold"
    can_single_requeue = False
    partial_scope = "not_partial"
    badge = "hold"

    if status in {"running", "launching"}:
        summary = "現在不建議 requeue。這條任務還在遠端執行或啟動中，直接重排可能造成雙啟動。"
        reasons.append("先用 Supervise/Collect 確認遠端狀態；如果真的要重跑，先停止或等它結束。")
        recommendation = "do_not_requeue_running"
        badge = "not now"
    elif status == "completed":
        summary = "通常不需要 requeue。這條已經 completed，除非你想做新的 attempt 或新的任務版本。"
        reasons.append("completed 結果應該先被下游使用；重跑最好建立新 task 或新 attempt，避免覆蓋解讀。")
        recommendation = "prefer_new_attempt"
        badge = "usually no"
    elif status == "pending":
        summary = "它已經在 pending queue 裡，不需要 requeue。"
        reasons.append("如果它沒跑，問題通常是 dependencies、GPU capacity、source lock 或 scheduler policy。")
        recommendation = "already_pending"
        badge = "already queued"
    elif status == "partial":
        if active_dependents:
            can_single_requeue = True
            partial_scope = "chain_partial"
            badge = "chain partial"
            recommendation = "prefer_pipeline_or_use_partial"
            summary = "這是鏈路 partial：它不是孤立失敗，後面還有任務依賴它。"
            reasons.append(f"目前找到 {len(active_dependents)} 個還活躍的下游依賴，所以它應該被當成一整條 pipeline 的斷點來處理。")
            if partial_consumers:
                reasons.append(f"其中 {len(partial_consumers)} 個下游明確允許吃 partial artifact，可以先跑分析再決定是否重跑 GPU。")
            else:
                reasons.append("下游沒有明確標成可吃 partial；單條 requeue 後仍可能需要重排或修正下游。")
            reasons.append("建議優先選：接續下游 analysis / pipeline replan / 整條 rerun，而不是只按單任務 requeue。")
        else:
            can_single_requeue = True
            partial_scope = "single_partial"
            badge = "yes"
            recommendation = "single_task_requeue_ok"
            summary = "這是單點 partial：目前沒有活躍下游依賴，適合單條 requeue。"
            reasons.append("適合用在：這條任務本身沒跑完整、沒有需要保留 partial 給下游分析、你想補一個完整結果。")
    elif status == "failed":
        badge = "maybe"
        recommendation = "rerun_after_review"
        summary = "可以考慮重跑，但最好先看 failed reason。"
        reasons.append("如果是 GPU/SSH/暫時性資源問題，可以新 attempt；如果是任務書或 code 問題，先修任務書/程式再重排。")
    else:
        summary = "目前無法明確判斷是否應該 requeue。"
        reasons.append("請先看 status、result.yaml、worker_notes 或用 AI 解釋補充判斷。")
        recommendation = "unknown"
        badge = "unknown"

    if active_dependents:
        preview = ", ".join(str(item.get("task_id")) for item in active_dependents[:5])
        if len(active_dependents) > 5:
            preview += f", +{len(active_dependents) - 5} more"
        reasons.append(f"活躍下游：{preview}")

    return {
        "task_id": task_id,
        "status": status,
        "badge": badge,
        "summary": summary,
        "reasons": reasons,
        "recommendation": recommendation,
        "can_single_requeue": can_single_requeue,
        "partial_scope": partial_scope,
        "dependents": dependents,
        "active_dependents": active_dependents,
    }


def _requeue_or_skip(task_id: str, reason: str) -> Dict[str, object]:
    record = lab.status(task_id)
    status = str(record.get("status") or "")
    if status in {"running", "launching"}:
        return {"task_id": task_id, "status": status, "changed": False, "reason": "running_or_launching"}
    if status == "completed":
        return {"task_id": task_id, "status": status, "changed": False, "reason": "completed"}
    if status == "pending":
        return {"task_id": task_id, "status": status, "changed": False, "reason": "already_pending"}
    result = _dashboard_action_request(
        "requeue_partial",
        task_id=task_id,
        reason=reason,
        safe_level="dangerous_requires_explicit_approval",
        expected_task_status=status,
    )
    return {"task_id": task_id, "status": status, "changed": bool(result.get("created")), "result": result}


def _use_partial_continue_downstream(task_id: str) -> Dict[str, object]:
    record = lab.status(task_id)
    guidance = _requeue_guidance(task_id, record)
    downstream = [
        item
        for item in guidance.get("active_dependents", []) or []
        if item.get("accepts_partial")
    ]
    changed: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    for item in downstream:
        child_id = str(item.get("task_id") or "")
        if not child_id:
            continue
        outcome = _requeue_or_skip(
            child_id,
            f"Continue downstream using partial artifacts from {task_id}",
        )
        if outcome.get("changed") or outcome.get("reason") == "already_pending":
            changed.append(outcome)
        else:
            skipped.append(outcome)
    return {
        "ok": True,
        "task_id": task_id,
        "parent_status": record.get("status"),
        "changed": changed,
        "skipped": skipped,
        "note": "Queued downstream tasks that explicitly accept this partial artifact.",
    }


def _requeue_partial_chain(task_id: str) -> Dict[str, object]:
    record = lab.status(task_id)
    targets: List[str] = [task_id]
    for item in _dependent_chain(task_id):
        child_id = str(item.get("task_id") or "")
        if child_id and child_id not in targets:
            targets.append(child_id)

    changed: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    for target_id in targets:
        outcome = _requeue_or_skip(
            target_id,
            f"Requeued partial chain from dashboard root {task_id}",
        )
        if outcome.get("changed") or outcome.get("reason") == "already_pending":
            changed.append(outcome)
        else:
            skipped.append(outcome)
    return {
        "ok": True,
        "task_id": task_id,
        "parent_status": record.get("status"),
        "target_count": len(targets),
        "changed": changed,
        "skipped": skipped,
        "note": "Requeued the partial dependency chain where safe. Running or completed tasks were skipped.",
    }


def _render_requeue_guidance(guidance: Dict[str, object]) -> str:
    task_id = str(guidance.get("task_id") or "")
    reasons = "".join(f"<li>{_esc(item)}</li>" for item in guidance.get("reasons", []) or [])
    dependents = guidance.get("active_dependents", []) or []
    rows = []
    for item in dependents[:8]:
        rows.append(
            "<tr>"
            f"<td><a href=\"/why?id={urllib.parse.quote(str(item.get('task_id') or ''))}\">{_esc(item.get('task_id'))}</a></td>"
            f"<td>{_esc(item.get('status'))}</td>"
            f"<td>{_esc(item.get('relation'))}</td>"
            f"<td>{_esc('yes' if item.get('accepts_partial') else 'no')}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="muted">No active downstream dependencies found.</td></tr>')
    requeue_button = ""
    if guidance.get("can_single_requeue"):
        label = "Requeue Single Task"
        extra_warning = ""
        chain_actions = ""
        if guidance.get("partial_scope") == "chain_partial":
            label = "Still Requeue Single Task"
            chain_actions = (
                '<form method="post" action="/action" '
                f'onsubmit="return confirm(\'Queue downstream tasks that accept partial artifacts from { _esc(task_id) }?\')">'
                '<input type="hidden" name="action" value="use_partial_continue_downstream">'
                f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
                '<button>Use Partial And Continue Downstream</button>'
                "</form>"
                '<form method="post" action="/action" '
                f'onsubmit="return confirm(\'Requeue this partial chain from { _esc(task_id) }? Running and completed tasks will be skipped.\')">'
                '<input type="hidden" name="action" value="requeue_partial_chain">'
                f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
                '<button class="warn">Requeue Chain</button>'
                "</form>"
            )
            extra_warning = '<p class="muted">這是鏈路 partial。這個按鈕只重排單條任務，不會自動修復或重排下游。</p>'
        requeue_button = (
            extra_warning
            +
            chain_actions
            +
            '<form method="post" action="/action" '
            f'onsubmit="return confirm(\'Single-task requeue { _esc(task_id) }? If this is part of a broken chain, prefer pipeline replan instead.\')">'
            '<input type="hidden" name="action" value="requeue_partial">'
            f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
            f'<button class="warn">{_esc(label)}</button>'
            "</form>"
        )
    return (
        f'<div class="panel"><h2>Can Requeue? <span class="badge">{_esc(guidance.get("badge"))}</span></h2><div class="inner">'
        f'<p class="muted">Partial type: {_esc(guidance.get("partial_scope"))}</p>'
        f'<p class="headline">{_esc(guidance.get("summary"))}</p>'
        '<ul>'
        + reasons
        + '</ul><div class="actions">'
        + requeue_button
        + '</div><details><summary>Active downstream dependencies</summary>'
        '<table><tr><th>task</th><th>status</th><th>relation</th><th>accepts partial</th></tr>'
        + "".join(rows)
        + "</table></details></div></div>"
    )


def _build_why_ai_prompt(task_id: str, context: Dict[str, object]) -> str:
    compact = json.dumps(context, ensure_ascii=False, indent=2)[:9000]
    return f"""你是本地實驗排程 dashboard 的中文解釋助手。

請把下面這個 task 的 why/partial 診斷翻譯成人能快速決策的說明。

要求：
- 使用繁體中文。
- 不要照抄 raw JSON。
- 分成三段：目前狀態、為什麼卡住/partial、下一步建議。
- 語氣簡短明確。
- 如果是 partial，要提醒：單任務 rerun 和整條 pipeline 接續/重排是不一樣的。
- 請明確區分 single_partial 和 chain_partial：single_partial 可以偏向單條 requeue；chain_partial 要提醒先考慮整條 pipeline 接續/重排或先吃 partial artifact 做 analysis。
- 如果資料不足，請明確說「目前只能判斷到...」。

task_id: {task_id}

診斷 context:
{compact}
"""


def _generate_why_ai(task_id: str, context: Dict[str, object], model: str) -> Dict[str, object]:
    prompt = _build_why_ai_prompt(task_id, context)
    text = call_ollama(prompt, model=model, timeout_seconds=90)
    path = _save_why_ai(task_id, text)
    return {"ok": True, "task_id": task_id, "saved": str(path), "explanation": text}


def _task_book_text(task_id: str) -> str:
    try:
        _, path = lab.find_task(task_id)
    except Exception:
        return ""
    return _read_text(path, limit=30000)


def _task_book_snapshot(task_id: str, status: object = None) -> str:
    meta = _task_meta(task_id, status)
    task_md = _task_book_text(task_id)
    body = task_md
    if task_md.startswith("---"):
        parts = task_md.split("---", 2)
        if len(parts) == 3:
            body = parts[2].strip()
    body_lines: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        body_lines.append(stripped)
        if len(body_lines) >= 14:
            break
    fields = [
        ("title", meta.get("title")),
        ("objective", meta.get("objective")),
        ("depends_on", meta.get("depends_on")),
        ("requires_artifacts_from", meta.get("requires_artifacts_from")),
        ("success_criteria", meta.get("success_criteria")),
        ("expected_outputs", meta.get("expected_outputs")),
    ]
    lines = [f"{key}: {value}" for key, value in fields if value not in (None, "", [], {})]
    if body_lines:
        lines.append("\nTask body preview:")
        lines.extend(body_lines)
    return "\n".join(lines) or "No task.md snapshot available."


def _build_result_ai_prompt(task_id: str, context: Dict[str, object]) -> str:
    task_md = str(context.get("task_md") or "")[:9000]
    report_md = str(context.get("report_md") or "")[:7000]
    worker_notes = str(context.get("worker_notes_md") or "")[:5000]
    result_yaml = str(context.get("result_yaml") or "")[:5000]
    metrics_json = str(context.get("metrics_json") or "")[:4000]
    registry = json.dumps(context.get("registry") or {}, ensure_ascii=False, indent=2)
    logs = json.dumps(
        {
            "stderr_tail": context.get("stderr_tail") or "",
            "stdout_tail": context.get("stdout_tail") or "",
        },
        ensure_ascii=False,
        indent=2,
    )[:3000]
    return f"""你是本地實驗 dashboard 的結果整理助手。
請優先根據「任務書 task.md」整理任務要求，再對照目前已回收的 report/result/metrics/worker_notes，回答這個任務實際完成到什麼程度。

要求：
- 使用繁體中文。
- 不要編造；沒有資料就明確說「目前 task/report 裡沒有」。
- 第一優先是任務書對照，不要只摘要 log。
- 明確列出 task.md 裡的 objective / success criteria / expected outputs，如果 task.md 沒寫就說沒有。
- 用「任務書要求 -> 目前證據 -> 完成度」的方式回答。
- 如果是 partial/failed/running，要說明卡在哪裡、目前可用 artifact 是什麼。
- log 只能當輔助證據，不要把 log 當主體。
- 請固定使用以下段落：
  1. 任務書重點
  2. 任務書要求對照表
  3. 實際執行/修改了什麼
  4. 目前結果與指標
  5. 完成度判斷
  6. 可用 artifacts
  7. 風險與下一步

task_id: {task_id}

registry:
{registry}

task.md:
{task_md}

report.md:
{report_md}

worker_notes.md:
{worker_notes}

result.yaml:
{result_yaml}

metrics.json:
{metrics_json}

log tails, evidence only:
{logs}
"""


def _generate_result_ai(task_id: str, model: str) -> Dict[str, object]:
    record = lab.status(task_id)
    paths = _latest_report_paths(task_id)
    context = {
        "task_id": task_id,
        "registry": {
            key: record.get(key)
            for key in (
                "task_id",
                "title",
                "status",
                "summary_short",
                "latest_run_id",
                "result_path",
                "report_path",
                "remote_state",
                "last_remote_seen_at",
            )
        },
        "task_md": _task_book_text(task_id),
        "report_md": _read_text(paths["report.md"], limit=50000),
        "worker_notes_md": _read_text(paths["worker_notes.md"], limit=30000),
        "result_yaml": _read_text(paths["result.yaml"], limit=22000),
        "metrics_json": _read_text(paths["metrics.json"], limit=22000),
        "stderr_tail": _tail(paths["stderr.log"], limit=5000),
        "stdout_tail": _tail(paths["stdout.log"], limit=5000),
    }
    prompt = _build_result_ai_prompt(task_id, context)
    text = call_ollama(prompt, model=model, timeout_seconds=120)
    path = _save_result_ai(task_id, text)
    return {"ok": True, "task_id": task_id, "saved": str(path), "summary": text}


def _render_why(task_id: str, config: Dict[str, object]) -> bytes:
    queue_item: Optional[Dict[str, object]] = None
    try:
        for item in lab.queue():
            if str(item.get("task_id") or "") == task_id:
                queue_item = item
                break
    except Exception:
        queue_item = None
    try:
        record = lab.status(task_id)
    except Exception:
        record = {}
    try:
        plan = _schedule_dry_run(config)
    except Exception as exc:
        plan = {"plan_error": f"{type(exc).__name__}: {exc}", "skipped": []}

    dep = queue_item.get("dependency_state") if isinstance(queue_item, dict) else {}
    deps = dep if isinstance(dep, dict) else {}
    skipped = None
    for item in plan.get("skipped", []) or []:
        if str(item.get("task_id") or "") == task_id:
            skipped = item
            break

    dep_rows = []
    for dep_item in deps.get("depends_on", []) or []:
        dep_id = str(dep_item)
        dep_status = "missing"
        try:
            dep_status = str(lab.status(dep_id).get("status") or "missing")
        except Exception:
            pass
        dep_rows.append(f"<tr><td>{_esc(dep_id)}</td><td>{_esc(dep_status)}</td></tr>")
    if not dep_rows:
        dep_rows.append('<tr><td colspan="2" class="muted">No depends_on blockers.</td></tr>')

    artifact_rows = []
    for dep_item in deps.get("requires_artifacts_from", []) or []:
        dep_id = str(dep_item)
        missing = dep_id in [str(x) for x in deps.get("missing_artifacts", []) or []]
        artifact_rows.append(f"<tr><td>{_esc(dep_id)}</td><td>{_esc('missing' if missing else 'available or unchecked')}</td></tr>")
    if not artifact_rows:
        artifact_rows.append('<tr><td colspan="2" class="muted">No required artifact blockers.</td></tr>')

    scheduler_reason = _human_skip_reason(skipped or {}) if skipped else "not skipped by current dry-run"
    next_plan = plan.get("plan", []) or []
    diagnosis = _why_diagnosis(task_id, record, queue_item, deps, skipped, plan)
    requeue_guidance = _requeue_guidance(task_id, record)
    ai_text = _read_why_ai(task_id)
    ai_panel = (
        '<div class="panel"><h2>AI 翻譯解釋</h2><div class="inner">'
        '<p class="muted">AI 解釋會快取；只有按按鈕時才重新生成，不會每次打開都吃 VRAM。</p>'
        f'<pre>{_esc(ai_text or "目前還沒有 AI 解釋。如果上面的白話解釋仍然不清楚，可以點下面按鈕。")}</pre>'
        '<form method="post" action="/action">'
        '<input type="hidden" name="action" value="explain_why_ai">'
        f'<input type="hidden" name="task_id" value="{_esc(task_id)}">'
        '<button>AI 翻譯/解釋</button>'
        '</form></div></div>'
    )
    why_context = {
        "diagnosis": diagnosis,
        "requeue_guidance": requeue_guidance,
        "record": {key: record.get(key) for key in ("task_id", "title", "status", "summary_short", "latest_run_id", "remote_state", "last_remote_seen_at")},
        "dependency_state": deps,
        "scheduler_skip": skipped,
        "capacity": plan.get("capacity"),
        "limits": plan.get("limits"),
        "next_to_start": next_plan,
    }
    body = (
        f'<div class="panel"><h2>Why / Partial Diagnosis: {_esc(task_id)}</h2><div class="inner">'
        f'<p>Status: <strong>{_esc(record.get("status") or "not registered")}</strong></p>'
        f'<p>Ready: <strong>{_esc(queue_item.get("ready") if isinstance(queue_item, dict) else "not in queue")}</strong></p>'
        f'<p>Scheduler reason: <strong>{_esc(scheduler_reason)}</strong></p>'
        f'<p class="muted">Resource: {_esc((_task_meta(task_id, record.get("status")).get("resource_hint") or "-"))} | Stage: {_esc((_task_meta(task_id, record.get("status")).get("execution_stage") or "-"))}</p>'
        '</div></div>'
        + _render_why_plain(diagnosis)
        + _render_requeue_guidance(requeue_guidance)
        + ai_panel
        + '<div class="grid">'
        '<div class="panel"><h2>Dependency Status</h2><div class="inner"><table><tr><th>depends_on</th><th>status</th></tr>'
        + "".join(dep_rows)
        + '</table></div></div>'
        + '<div class="panel"><h2>Artifact Requirements</h2><div class="inner"><table><tr><th>requires_artifacts_from</th><th>state</th></tr>'
        + "".join(artifact_rows)
        + '</table></div></div></div>'
        + '<div class="panel"><h2>Current Scheduler Dry Run</h2><div class="inner"><pre>'
        + _esc(json.dumps({"next_to_start": next_plan, "this_task_skipped": skipped, "capacity": plan.get("capacity"), "limits": plan.get("limits"), "plan_error": plan.get("plan_error")}, indent=2, ensure_ascii=False))
        + '</pre><p><a href="/">Back</a></p></div></div>'
        + '<details class="section"><summary>Raw Why Context</summary><div class="section-body"><pre>'
        + _esc(json.dumps(why_context, indent=2, ensure_ascii=False))
        + '</pre></div></details>'
    )
    return _render_page(f"Why {task_id}", body, refresh=False)


def _switch_workplace_form(workplace: Dict[str, object], config: Dict[str, object], label: str = "Switch Active") -> str:
    return (
        '<form method="post" action="/action">'
        '<input type="hidden" name="action" value="save_remote_config">'
        f'<input type="hidden" name="host" value="{_esc(workplace.get("host"))}">'
        f'<input type="hidden" name="remote_workdir" value="{_esc(workplace.get("remote_workdir"))}">'
        f'<input type="hidden" name="remote_root" value="{_esc(workplace.get("remote_root"))}">'
        f'<input type="hidden" name="allowed_gpu_ids" value="{_esc(workplace.get("allowed_gpu_ids") or config.get("allowed_gpu_ids"))}">'
        f'<input type="hidden" name="cuda_mapping_note" value="{_esc(workplace.get("cuda_mapping_note") or "")}">'
        f'<input type="hidden" name="remote_worker" value="{_esc(config.get("remote_worker") or DEFAULT_REMOTE_WORKER)}">'
        f'<button>{_esc(label)}</button>'
        "</form>"
    )


def _render_manage(config: Dict[str, object]) -> bytes:
    active_key = _remote_workplace_key(_current_remote_preset(config))
    active = _current_remote_preset(config, label="Active workplace")
    active_counts = next((item.get("counts") for item in _remote_workplaces(config) if _remote_workplace_key(item) == active_key), {"total": 0, "active": 0})
    saved_cards: List[str] = []
    for preset in _remote_presets(config):
        key = _remote_workplace_key(preset)
        is_active = key == active_key
        custom_index = preset.get("custom_index")
        delete_form = ""
        if custom_index is not None:
            delete_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Delete this saved workplace? Existing runs are not deleted.\') && confirm(\'Second confirmation: remove this saved workplace from Manage?\')">'
                '<input type="hidden" name="action" value="delete_remote_preset">'
                f'<input type="hidden" name="preset_index" value="{_esc(custom_index)}">'
                '<button class="warn">Delete Saved</button>'
                "</form>"
            )
        elif preset.get("builtin"):
            delete_form = '<span class="badge">built-in</span>'
        saved_cards.append(
            f'<div class="preset-card {"active" if is_active else ""}">'
            f'<div class="run-head"><strong>{_esc(preset.get("label"))}</strong><span class="badge {"running" if is_active else ""}">{_esc("active" if is_active else "saved")}</span></div>'
            f'<p class="muted">host: {_esc(preset.get("host"))}<br>workdir: {_esc(preset.get("remote_workdir"))}<br>root: {_esc(preset.get("remote_root"))}</p>'
            '<div class="actions">'
            + ("" if is_active else _switch_workplace_form(preset, config, "Use"))
            + delete_form
            + "</div></div>"
        )

    history_cards: List[str] = []
    hidden_count = 0
    for workplace in _remote_history_workplaces(config, include_hidden=True):
        key = _remote_workplace_key(workplace)
        encoded_key = _workplace_config_key(key[0], key[1])
        is_active = key == active_key
        hidden = bool(workplace.get("hidden"))
        hidden_count += 1 if hidden else 0
        counts = workplace.get("counts") if isinstance(workplace.get("counts"), dict) else {}
        if hidden:
            action_form = (
                '<form method="post" action="/action">'
                '<input type="hidden" name="action" value="show_history_workplace">'
                f'<input type="hidden" name="host" value="{_esc(workplace.get("host"))}">'
                f'<input type="hidden" name="remote_root" value="{_esc(workplace.get("remote_root"))}">'
                '<button>Restore</button>'
                "</form>"
            )
        elif is_active:
            action_form = '<span class="badge">active</span>'
        else:
            action_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Forget this history workplace from the UI? Remote files, local task history, and results are not deleted.\') && confirm(\'Second confirmation: hide this history workplace from Manage/Workplaces?\')">'
                '<input type="hidden" name="action" value="hide_history_workplace">'
                f'<input type="hidden" name="host" value="{_esc(workplace.get("host"))}">'
                f'<input type="hidden" name="remote_root" value="{_esc(workplace.get("remote_root"))}">'
                '<button class="warn">Forget</button>'
                "</form>"
            )
        history_cards.append(
            f'<div class="preset-card {"active" if is_active else ""}">'
            '<label class="check-row">'
            f'<input type="checkbox" name="workplace_key" value="{_esc(encoded_key)}" form="history-bulk-form">'
            f'<span>{_esc(workplace.get("label"))}</span>'
            f'<span class="badge">{_esc("hidden" if hidden else "history")}</span>'
            "</label>"
            f'<p class="muted">host: {_esc(workplace.get("host"))}<br>workdir: {_esc(workplace.get("remote_workdir"))}<br>root: {_esc(workplace.get("remote_root"))}</p>'
            f'<p class="muted">runs: {_esc(counts.get("total", 0))} · active workers: {_esc(counts.get("active", 0))}</p>'
            '<div class="actions">'
            + ("" if is_active or hidden else _switch_workplace_form(workplace, config, "Use"))
            + action_form
            + "</div></div>"
        )

    bulk_history = ""
    if history_cards:
        bulk_history = (
            '<form id="history-bulk-form" method="post" action="/action" '
            'onsubmit="return confirm(\'Apply this bulk history workplace action? Remote files, local task history, and results are not deleted.\') && confirm(\'Second confirmation: apply the selected bulk action?\')">'
            '<input type="hidden" name="action" value="bulk_history_workplaces">'
            '<div class="actions">'
            '<button class="warn" name="bulk_operation" value="forget">Forget Selected</button>'
            '<button name="bulk_operation" value="restore">Restore Selected</button>'
            "</div></form>"
        )
    restore_all = ""
    if hidden_count:
        restore_all = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="show_hidden_history_workplaces">'
            f'<button>Restore All Hidden ({_esc(hidden_count)})</button>'
            "</form>"
        )
    cleanup_checks = "".join(
        '<label class="check-row">'
        f'<input type="checkbox" name="cleanup_category" value="{_esc(key)}" form="local-cleanup-form" {"checked" if key in {"temp", "logs", "dispatch", "worktrees"} else ""}>'
        f'<span>{_esc(meta["label"])}</span>'
        f'<small class="muted">{_esc(meta["description"])}</small>'
        "</label>"
        for key, meta in LOCAL_CLEANUP_CATEGORIES.items()
    )
    body = (
        '<div class="panel"><h2>Manage</h2><div class="inner">'
        '<p class="muted">Manage only changes local dashboard settings and what the UI remembers. It does not delete remote folders, local task history, or collected results.</p>'
        '<div class="actions"><a href="/"><button>Back to Dashboard</button></a><a href="/settings"><button>Add / Edit Workplace</button></a>'
        + restore_all
        + "</div>"
        + '<h3>Active Workplace</h3>'
        '<div class="preset-grid"><div class="preset-card active">'
        f'<strong>{_esc(active.get("label"))}</strong>'
        f'<p class="muted">host: {_esc(active.get("host"))}<br>workdir: {_esc(active.get("remote_workdir"))}<br>root: {_esc(active.get("remote_root"))}</p>'
        f'<p class="muted">runs: {_esc((active_counts or {}).get("total", 0))} · active workers: {_esc((active_counts or {}).get("active", 0))}</p>'
        "</div></div>"
        f'<h3>Saved Workplaces <span class="badge">{_esc(len(_custom_remote_presets(config)))}/{MAX_CUSTOM_REMOTE_PRESETS} custom</span></h3>'
        '<div class="preset-grid">' + ("".join(saved_cards) or '<p class="muted">No saved workplaces.</p>') + "</div>"
        f'<h3>History Workplaces <span class="badge">{_esc(len(history_cards))} total · {_esc(hidden_count)} hidden</span></h3>'
        + bulk_history
        + '<div class="preset-grid">' + ("".join(history_cards) or '<p class="muted">No recorded remote workplaces.</p>') + "</div>"
        '<div class="danger-zone"><h3>Danger Zone: Local Files</h3>'
        '<p class="muted">Workplace Forget only hides UI entries. Physical local cleanup is explicit, category-based, and never deletes remote folders.</p>'
        '<form id="local-cleanup-form" method="post" action="/action"></form>'
        '<div class="preset-grid">'
        + cleanup_checks
        + "</div>"
        '<div class="actions">'
        '<form method="post" action="/action" onsubmit="return confirm(\'Hide completed tasks from the board? Results will not be deleted.\') && confirm(\'Second confirmation: this only changes the local board view, not files.\')">'
        '<input type="hidden" name="action" value="clear_completed"><button class="warn">Clear Completed From Board</button></form>'
        '<button form="local-cleanup-form" name="action" value="local_cleanup_plan">Dry Run Local Cleanup</button>'
        '<button class="danger" form="local-cleanup-form" name="action" value="local_cleanup_delete" '
        'onclick="return confirm(\'Delete selected local generated files? Remote folders are not touched.\') && confirm(\'Second confirmation: this physically deletes selected local files inside the solution-agent workspace.\')">Delete Selected Local Files</button>'
        '</div>'
        '<p class="muted">Inactive means the registry does not mark the task as running, launching, or launch_unknown. Hidden completed results are only deleted when that category is checked.</p>'
        "</div>"
        "</div></div>"
    )
    return _render_page("Manage", body, refresh=False)


def _render_settings(config: Dict[str, object]) -> bytes:
    aliases = _ssh_host_aliases()
    alias_options = "".join(f'<option value="{_esc(alias)}">' for alias in aliases)
    presets = _remote_presets(config)
    preset_forms: List[str] = []
    for preset in presets:
        custom_index = preset.get("custom_index")
        profile = _execution_profile_for_target(preset.get("host"), preset.get("remote_workdir"))
        delete_form = ""
        if custom_index is not None:
            delete_form = (
                '<form method="post" action="/action" onsubmit="return confirm(\'Delete this quick target?\') && confirm(\'Second confirmation: remove this saved target from the UI?\')">'
                '<input type="hidden" name="action" value="delete_remote_preset">'
                f'<input type="hidden" name="preset_index" value="{_esc(custom_index)}">'
                '<button class="warn">Delete</button>'
                "</form>"
            )
        elif preset.get("builtin"):
            delete_form = '<span class="badge">built-in</span>'
        preset_forms.append(
            '<div class="preset-card">'
            f'<strong>{_esc(preset.get("label"))}</strong>'
            f'<p class="muted">{_esc(preset.get("host"))}<br>{_esc(preset.get("remote_workdir"))}<br>{_esc(preset.get("remote_root"))}</p>'
            f'<p>{_profile_execution_summary(profile)}</p>'
            '<div class="actions"><form method="post" action="/action">'
            '<input type="hidden" name="action" value="save_remote_config">'
            f'<input type="hidden" name="host" value="{_esc(preset.get("host"))}">'
            f'<input type="hidden" name="remote_workdir" value="{_esc(preset.get("remote_workdir"))}">'
            f'<input type="hidden" name="remote_root" value="{_esc(preset.get("remote_root"))}">'
            f'<input type="hidden" name="allowed_gpu_ids" value="{_esc(preset.get("allowed_gpu_ids") or config.get("allowed_gpu_ids"))}">'
            f'<input type="hidden" name="cuda_mapping_note" value="{_esc(preset.get("cuda_mapping_note") or "")}">'
            '<button>Use This</button>'
            "</form>"
            + delete_form
            + "</div></div>"
        )
    active_profile = _execution_profile_for_target(config.get("host"), config.get("remote_workdir"))
    profile_conda = active_profile.get("conda") if isinstance(active_profile.get("conda"), dict) else {}
    profile_root = active_profile.get("remote_output_root") or active_profile.get("remote_root")
    profile_warning = ""
    if not active_profile:
        profile_warning = '<p class="muted">No matching config/remote_execution_profiles.yaml entry. workplace-init will still probe the host, but materialized_execution may fall back to python3 instead of your intended conda env.</p>'
    elif profile_root and str(profile_root).rstrip("/") != str(config.get("remote_root") or "").rstrip("/"):
        profile_warning = f'<p class="muted">Profile output root differs from active target: {_esc(profile_root)}. Save/switch the correct workplace before scheduling.</p>'
    body = (
        '<div class="panel"><h2>Remote Target Settings</h2><div class="inner">'
        '<p class="muted">This reads local SSH aliases from ~/.ssh/config. It does not store passwords; SSH authentication still uses your existing local config/key.</p>'
        '<form method="post" action="/action">'
        '<input type="hidden" name="action" value="save_remote_config">'
        '<div class="form-grid">'
        f'<label>SSH host alias<input list="ssh-hosts" name="host" value="{_esc(config.get("host"))}"></label>'
        f'<datalist id="ssh-hosts">{alias_options}</datalist>'
        f'<label>Remote workdir<input name="remote_workdir" value="{_esc(config.get("remote_workdir"))}"></label>'
        f'<label>Remote output root<input name="remote_root" value="{_esc(config.get("remote_root"))}"></label>'
        f'<label>Allowed nvidia-smi GPU ids<input name="allowed_gpu_ids" value="{_esc(config.get("allowed_gpu_ids"))}"></label>'
        f'<label>Default worker{_remote_worker_select("remote_worker", config.get("remote_worker") or DEFAULT_REMOTE_WORKER)}</label>'
        f'<label>Optional CUDA mapping note<input name="cuda_mapping_note" value="{_esc(config.get("cuda_mapping_note") or "")}" placeholder="Leave blank unless this host has special CUDA/nvidia-smi mapping"></label>'
        f'<label>Min free GPU memory GB<input name="min_free_gpu_mem_gb" value="{_esc(config.get("min_free_gpu_mem_gb"))}"></label>'
        f'<label>Max starts per cycle<input name="max_starts_per_cycle" value="{_esc(config.get("max_starts_per_cycle"))}"></label>'
        f'<label>Max running total<input name="max_running_total" value="{_esc(config.get("max_running_total"))}"></label>'
        f'<label>Max running GPU tasks<input name="max_running_gpu" value="{_esc(config.get("max_running_gpu"))}"></label>'
        f'<label>Max running CPU tasks<input name="max_running_cpu" value="{_esc(config.get("max_running_cpu"))}"></label>'
        f'<label>Max tasks per GPU<input name="max_tasks_per_gpu" value="{_esc(config.get("max_tasks_per_gpu"))}"></label>'
        f'<label>Scheduler interval minutes<input name="interval_minutes" value="{_esc(config.get("interval_minutes"))}"></label>'
        f'<label>GPU cache seconds<input name="gpu_snapshot_cache_seconds" value="{_esc(config.get("gpu_snapshot_cache_seconds"))}"></label>'
        f'<label>GPU watcher interval seconds<input name="remote_watch_interval_seconds" value="{_esc(config.get("remote_watch_interval_seconds"))}"></label>'
        "</div><p><button>Save Target</button></p></form>"
        '<div class="subpanel"><h3>Init / Execution Profile</h3>'
        f'<p>Active profile: {_profile_execution_summary(active_profile)}</p>'
        f'<p class="muted">Conda executable: {_esc(profile_conda.get("executable") or "-")}<br>'
        f'Conda env: {_esc(profile_conda.get("env") or "-")}<br>'
        f'Conda prefix: {_esc(profile_conda.get("prefix") or "-")}<br>'
        f'Artifact root used by execution_port: {_esc(config.get("remote_root"))}</p>'
        f'{profile_warning}'
        '<p class="muted">For a new host, add or edit config/remote_execution_profiles.yaml, then run Workplace Init. The remote ~/.remote_materializer/.env must be filled by you; init only verifies auth readiness.</p>'
        '<div class="actions">'
        '<form method="post" action="/action"><input type="hidden" name="action" value="remote_preflight"><button>Preflight</button></form>'
        '<form method="post" action="/action"><input type="hidden" name="action" value="workplace_init"><button>Workplace Init</button></form>'
        '</div></div>'
        f'<h3>Quick Targets <span class="badge">{len(_custom_remote_presets(config))}/{MAX_CUSTOM_REMOTE_PRESETS} custom</span></h3><div class="preset-grid">'
        + "".join(preset_forms)
        + '</div><p><a href="/">Back to Dashboard</a></p></div></div>'
    )
    return _render_page("Remote Target Settings", body, refresh=False)


def _action_result_model(action: str, message: Dict[str, object]) -> Dict[str, object]:
    is_error = bool(message.get("error"))
    task_id = str(message.get("task_id") or "")
    status = str(message.get("status") or "")
    headline = "Action failed" if is_error else "Action completed"
    details: List[str] = []
    links = ['<a href="/">Back to Dashboard</a>']

    if action == "requeue_partial" and not is_error:
        headline = "Requeue request created" if message.get("created") else "Requeue request not created"
        details.append(f"Action request: {_esc(message.get('action_id'))}")
        details.append(f"Task: {_esc(task_id or ((message.get('request') or {}).get('task_id') if isinstance(message.get('request'), dict) else ''))}")
        details.append("CLI processor must apply this with --allow-dangerous.")
        if task_id:
            links.insert(0, f'<a href="/task?id={urllib.parse.quote(task_id)}">Open Task</a>')
    elif action == "use_partial_continue_downstream" and not is_error:
        headline = "Downstream queued from partial"
        changed = message.get("changed") if isinstance(message.get("changed"), list) else []
        skipped = message.get("skipped") if isinstance(message.get("skipped"), list) else []
        details.append(f"Parent: {_esc(task_id)}")
        details.append(f"Queued/already pending downstream: {_esc(len(changed))}")
        details.append(f"Skipped: {_esc(len(skipped))}")
        if changed:
            details.append("Targets: " + _esc(", ".join(str(item.get("task_id")) for item in changed[:8] if isinstance(item, dict))))
        if task_id:
            links.insert(0, f'<a href="/why?id={urllib.parse.quote(task_id)}">Open Why</a>')
    elif action == "requeue_partial_chain" and not is_error:
        headline = "Partial chain requeued"
        changed = message.get("changed") if isinstance(message.get("changed"), list) else []
        skipped = message.get("skipped") if isinstance(message.get("skipped"), list) else []
        details.append(f"Root: {_esc(task_id)}")
        details.append(f"Chain targets checked: {_esc(message.get('target_count', 0))}")
        details.append(f"Queued/already pending: {_esc(len(changed))}")
        details.append(f"Skipped: {_esc(len(skipped))}")
        if changed:
            details.append("Targets: " + _esc(", ".join(str(item.get("task_id")) for item in changed[:8] if isinstance(item, dict))))
        if task_id:
            links.insert(0, f'<a href="/why?id={urllib.parse.quote(task_id)}">Open Why</a>')
    elif action == "advisor_feedback" and not is_error:
        headline = "Advisor feedback saved"
        details.append("The next Replan Advisor rewrite can use this feedback.")
        details.append(f"Saved: {_esc(message.get('saved'))}")
    elif action == "explain_why_ai" and not is_error:
        headline = "AI explanation saved"
        details.append(f"Task: {_esc(message.get('task_id'))}")
        details.append(f"Saved: {_esc(message.get('saved'))}")
        if message.get("task_id"):
            links.insert(0, f'<a href="/why?id={urllib.parse.quote(str(message.get("task_id")))}">Open Why</a>')
    elif action == "summarize_result_ai" and not is_error:
        headline = "AI result summary saved"
        details.append(f"Task: {_esc(message.get('task_id'))}")
        details.append(f"Saved: {_esc(message.get('saved'))}")
        if message.get("task_id"):
            links.insert(0, f'<a href="/task?id={urllib.parse.quote(str(message.get("task_id")))}">Open Task</a>')
    elif action == "clear_completed" and not is_error:
        headline = "Completed tasks hidden from board"
        details.append(f"Hidden count: {_esc(message.get('hidden_completed_count', 0))}")
        details.append("Results and registry were not deleted.")
    elif action == "show_completed" and not is_error:
        headline = "Completed tasks are visible again"
    elif action == "hide_attention_task" and not is_error:
        headline = "Task hidden from Need Attention"
        details.append(f"Task: {_esc(message.get('task_id'))}")
        details.append("Files, results, and registry were not deleted.")
    elif action == "hide_attention_group" and not is_error:
        headline = "Partial group hidden from Need Attention"
        details.append(f"Hidden count: {_esc(message.get('hidden_group_count', 0))}")
        hidden_tasks = message.get("task_ids") if isinstance(message.get("task_ids"), list) else []
        if hidden_tasks:
            details.append("Tasks: " + _esc(", ".join(str(item) for item in hidden_tasks[:8])))
        details.append("Files, results, and registry were not deleted.")
    elif action == "bulk_hide_attention" and not is_error:
        headline = "Selected attention items hidden"
        details.append(f"Hidden count: {_esc(message.get('hidden_count', 0))}")
        details.append("Files, results, and registry were not deleted.")
    elif action == "bulk_hide_completed" and not is_error:
        headline = "Selected completed tasks hidden"
        details.append(f"Hidden count: {_esc(message.get('hidden_count', 0))}")
        details.append("Results and registry were not deleted.")
    elif action == "show_attention" and not is_error:
        headline = "Hidden attention tasks are visible again"
        details.append(f"Restored count: {_esc(message.get('restored_count', 0))}")
    elif action == "schedule_once" and not is_error:
        headline = "Schedule request created" if message.get("created") else "Schedule request not created"
        details.append(f"Action request: {_esc(message.get('action_id'))}")
        details.append("CLI processor must apply this with --allow-launch.")
    elif action == "dependency_reconcile" and not is_error:
        headline = "Dependency reconcile request created" if message.get("created") else "Dependency reconcile request not created"
        details.append(f"Action request: {_esc(message.get('action_id'))}")
        details.append("CLI processor can apply this with --apply-safe.")
    elif action == "remote_stop" and not is_error:
        headline = "Remote stop request created" if message.get("created") else "Remote stop request not created"
        details.append(f"Action request: {_esc(message.get('action_id'))}")
        details.append("CLI processor must apply this with --allow-dangerous.")
    elif action == "remote_monitor_task" and not is_error:
        headline = "Remote task monitored"
        details.append(f"Task: {_esc(message.get('task_id'))}")
        details.append(f"Decision: {_esc(message.get('decision') or message.get('state'))}")
        if message.get("collected"):
            details.append("Collected finished artifacts into local result folders.")
        task_value = str(message.get("task_id") or "")
        if task_value:
            links.insert(0, f'<a href="/task?id={urllib.parse.quote(task_value)}">Open Task</a>')
    elif action == "supervise_once" and not is_error:
        headline = "Supervise / collect pass completed"
    elif action == "scheduler_start" and not is_error:
        if message.get("started"):
            headline = "Scheduler started"
        elif message.get("reason") == "remote preflight failed":
            headline = "Scheduler not started: remote preflight failed"
        else:
            headline = "Scheduler already running"
        details.append(f"PID: {_esc(message.get('pid') or ((message.get('status') or {}).get('lock') or {}).get('pid'))}")
        if message.get("reason"):
            details.append(f"Reason: {_esc(message.get('reason'))}")
        preflight = message.get("preflight") if isinstance(message.get("preflight"), dict) else {}
        if preflight:
            details.append(f"Preflight: {_esc('ok' if preflight.get('ok') else 'failed')}")
    elif action == "scheduler_stop" and not is_error:
        headline = "Scheduler stopped"
        details.append(f"PID: {_esc(message.get('pid'))}")
    elif action == "remote_watch_start" and not is_error:
        headline = "GPU watcher started" if message.get("started") else "GPU watcher already running"
        details.append(f"PID: {_esc(message.get('pid') or ((message.get('status') or {}).get('lock') or {}).get('pid'))}")
        details.append("Dashboard GPU panels now read the local cache written by the watcher.")
    elif action == "remote_watch_stop" and not is_error:
        headline = "GPU watcher stopped"
        details.append(f"PID: {_esc(message.get('pid'))}")
    elif action == "save_remote_config" and not is_error:
        headline = "Remote target saved"
        details.append(f"Host: {_esc(message.get('host'))}")
        details.append(f"Workdir: {_esc(message.get('remote_workdir'))}")
        details.append(f"Output root: {_esc(message.get('remote_root'))}")
        details.append(f"Worker: {_esc(message.get('remote_worker'))}")
    elif action == "remote_preflight" and not is_error:
        headline = "Remote preflight passed" if message.get("ok") else "Remote preflight failed"
        details.append(f"Profile: {_esc(message.get('profile_id'))}")
        errors = message.get("errors") if isinstance(message.get("errors"), list) else []
        warnings = message.get("warnings") if isinstance(message.get("warnings"), list) else []
        if errors:
            details.append(f"Errors: {_esc('; '.join(str(item) for item in errors[:3]))}")
        if warnings:
            details.append(f"Warnings: {_esc('; '.join(str(item) for item in warnings[:3]))}")
    elif action == "local_init" and not is_error:
        headline = "Local init passed" if message.get("ok") else "Local init found missing items"
        details.append(f"Profile: {_esc(message.get('profile_id'))}")
        missing_required = message.get("missing_required") if isinstance(message.get("missing_required"), list) else []
        missing_recommended = message.get("missing_recommended") if isinstance(message.get("missing_recommended"), list) else []
        local_paths = message.get("local_paths") if isinstance(message.get("local_paths"), dict) else {}
        if missing_required:
            details.append(f"Missing required: {_esc(', '.join(str(item) for item in missing_required))}")
        if missing_recommended:
            details.append(f"Missing recommended: {_esc(', '.join(str(item) for item in missing_recommended[:8]))}")
        if local_paths.get("report"):
            details.append(f"Report: {_esc(local_paths.get('report'))}")
    elif action == "workplace_init" and not is_error:
        headline = "Workplace initialized" if message.get("ok") else "Workplace init found missing items"
        details.append(f"Profile: {_esc(message.get('profile_id'))}")
        missing_required = message.get("missing_required") if isinstance(message.get("missing_required"), list) else []
        missing_recommended = message.get("missing_recommended") if isinstance(message.get("missing_recommended"), list) else []
        local_paths = message.get("local_paths") if isinstance(message.get("local_paths"), dict) else {}
        if missing_required:
            details.append(f"Missing required: {_esc(', '.join(str(item) for item in missing_required))}")
        if missing_recommended:
            details.append(f"Missing recommended: {_esc(', '.join(str(item) for item in missing_recommended))}")
        if local_paths.get("report"):
            details.append(f"Report: {_esc(local_paths.get('report'))}")
        remote_sync = message.get("remote_sync") if isinstance(message.get("remote_sync"), dict) else {}
        if remote_sync.get("remote_profile"):
            details.append(f"Remote profile: {_esc(remote_sync.get('remote_profile'))}")
    elif action == "delete_remote_preset" and not is_error:
        headline = "Quick target deleted"
        details.append(f"Remaining custom targets: {_esc(message.get('custom_preset_count'))}/{MAX_CUSTOM_REMOTE_PRESETS}")
        details.append("Remote files, local task history, and results were not deleted.")
    elif action == "hide_history_workplace" and not is_error:
        headline = "History workplace forgotten from UI"
        details.append(f"Host: {_esc(message.get('host'))}")
        details.append(f"Root: {_esc(message.get('remote_root'))}")
        details.append("Only the Workplaces UI entry was hidden. Remote files, local task history, and results were not deleted.")
    elif action == "show_history_workplace" and not is_error:
        headline = "History workplace restored"
        details.append(f"Host: {_esc(message.get('host'))}")
        details.append(f"Root: {_esc(message.get('remote_root'))}")
    elif action == "bulk_history_workplaces" and not is_error:
        operation = str(message.get("operation") or "")
        headline = "History workplaces updated"
        details.append(f"Operation: {_esc(operation)}")
        details.append(f"Selected: {_esc(message.get('selected_count', 0))}")
        details.append(f"Changed: {_esc(message.get('changed_count', 0))}")
        details.append("Only local dashboard visibility changed. Remote files, task history, and results were not deleted.")
    elif action == "show_hidden_history_workplaces" and not is_error:
        headline = "Hidden history workplaces restored"
        details.append(f"Restored count: {_esc(message.get('restored_count', 0))}")
    elif action == "local_cleanup_plan" and not is_error:
        headline = "Local cleanup dry run ready"
        details.append(f"Candidates: {_esc(message.get('candidate_count', 0))}")
        details.append(f"Estimated size: {_esc(message.get('total_size'))}")
        categories = message.get("categories") if isinstance(message.get("categories"), list) else []
        if categories:
            details.append("Categories: " + _esc(", ".join(str(item) for item in categories)))
        sample = message.get("candidates") if isinstance(message.get("candidates"), list) else []
        if sample:
            details.append("First target: " + _esc((sample[0] or {}).get("path") if isinstance(sample[0], dict) else sample[0]))
        details.append("No files were deleted.")
    elif action == "local_cleanup_delete" and not is_error:
        headline = "Local cleanup completed" if not message.get("error_count") else "Local cleanup completed with errors"
        details.append(f"Deleted: {_esc(message.get('deleted_count', 0))}")
        details.append(f"Freed: {_esc(message.get('total_size'))}")
        if message.get("error_count"):
            details.append(f"Errors: {_esc(message.get('error_count'))}")
        details.append("Remote folders were not touched.")
    elif action == "sync_profile" and not is_error:
        headline = "Worker profile synced"
        details.append(f"Host: {_esc(message.get('host'))}")
        details.append(f"Remote workdir: {_esc(message.get('remote_workdir'))}")

    if not details and message.get("note"):
        details.append(str(message.get("note")))
    if not details and is_error:
        details.append("See raw details below.")

    return {
        "action": action,
        "is_error": is_error,
        "headline": headline,
        "details": details,
        "links": links,
        "reload": True,
        "raw": message,
    }


def _render_action_result(action: str, message: Dict[str, object]) -> bytes:
    result = _action_result_model(action, message)
    is_error = bool(result.get("is_error"))
    headline = str(result.get("headline") or "Action completed")
    details = [str(item) for item in (result.get("details") or [])]
    links = [str(item) for item in (result.get("links") or [])]
    detail_html = "".join(f"<li>{line}</li>" for line in details)
    body = (
        '<div class="panel"><h2>Action Result</h2><div class="inner">'
        f'<div class="notice {"error" if is_error else "ok"}">'
        f'<div class="headline">{_esc(headline)}</div>'
        f'<ul>{detail_html}</ul>'
        f'<p>{" | ".join(links)}</p>'
        "</div>"
        "<details class=\"section\"><summary>Raw details</summary><div class=\"section-body\"><pre>"
        + _esc(json.dumps(message, indent=2, ensure_ascii=False))
        + "</pre></div></details>"
        "</div></div>"
    )
    return _render_page("Action Result", body, refresh=False)


class DashboardHandler(BaseHTTPRequestHandler):
    config: Dict[str, object] = {}
    last_client_ping: float = time.time()
    shutdown_on_idle_seconds: int = 0
    server_ref: Optional[ThreadingHTTPServer] = None

    def _client_disconnected(self, exc: BaseException) -> bool:
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return True
        return isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10053, 10054, 10058}

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except Exception as exc:
            if self._client_disconnected(exc):
                return
            raise

    def _send(self, payload: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            if self._client_disconnected(exc):
                return
            raise

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/client-ping":
                DashboardHandler.last_client_ping = time.time()
                self._send(b"ok", content_type="text/plain; charset=utf-8")
            elif parsed.path == "/api/gpu":
                DashboardHandler.last_client_ping = time.time()
                self._send(_render_gpu_inner(self.config).encode("utf-8"))
            elif parsed.path == "/":
                DashboardHandler.last_client_ping = time.time()
                params = urllib.parse.parse_qs(parsed.query)
                self._send(_render_dashboard(self.config, params))
            elif parsed.path == "/settings":
                DashboardHandler.last_client_ping = time.time()
                self._send(_render_settings(self.config))
            elif parsed.path == "/manage":
                DashboardHandler.last_client_ping = time.time()
                self._send(_render_manage(self.config))
            elif parsed.path == "/task":
                DashboardHandler.last_client_ping = time.time()
                params = urllib.parse.parse_qs(parsed.query)
                task_id = (params.get("id") or [""])[0]
                self._send(_render_task(task_id))
            elif parsed.path == "/why":
                DashboardHandler.last_client_ping = time.time()
                params = urllib.parse.parse_qs(parsed.query)
                task_id = (params.get("id") or [""])[0]
                self._send(_render_why(task_id, self.config))
            else:
                self._send(b"not found", status=404, content_type="text/plain; charset=utf-8")
        except Exception as exc:
            if self._client_disconnected(exc):
                return
            payload = ("<pre>" + _esc(traceback.format_exc()) + "</pre>").encode("utf-8")
            self._send(payload, status=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/client-ping":
            DashboardHandler.last_client_ping = time.time()
            self._send(b"ok", content_type="text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or "0")
        data = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(data)
        action = (form.get("action") or [""])[0]
        message: Dict[str, object]
        try:
            maintenance = lab.load_maintenance_mode()
            if maintenance.get("enabled") and not maintenance.get("allow_action_requests"):
                message = {
                    "error": "maintenance mode is enabled; dashboard actions are read-only",
                    "maintenance_mode": maintenance,
                    "action": action,
                }
            elif action == "scheduler_start":
                message = lab.scheduler_start(
                    host=str(self.config["host"]),
                    remote_root=str(self.config["remote_root"]),
                    remote_workdir=str(self.config["remote_workdir"]),
                    max_starts_per_cycle=int(self.config["max_starts_per_cycle"]),
                    max_running_total=int(self.config["max_running_total"]),
                    max_running_gpu=int(self.config["max_running_gpu"]),
                    max_running_cpu=int(self.config["max_running_cpu"]),
                    min_free_gpu_mem_gb=float(self.config["min_free_gpu_mem_gb"]),
                    allowed_gpu_ids=str(self.config["allowed_gpu_ids"]),
                    max_tasks_per_gpu=int(self.config["max_tasks_per_gpu"]),
                    interval_minutes=int(self.config["interval_minutes"]),
                    model=str(self.config["model"]),
                    gpu_snapshot_cache_seconds=int(self.config.get("gpu_snapshot_cache_seconds") or DEFAULT_GPU_SNAPSHOT_CACHE_SECONDS),
                    worker=str(self.config.get("remote_worker") or DEFAULT_REMOTE_WORKER),
                )
            elif action == "scheduler_stop":
                message = lab.scheduler_stop()
            elif action == "remote_watch_start":
                message = lab.remote_watch_start(
                    host=str(self.config["host"]),
                    allowed_gpu_ids=str(self.config["allowed_gpu_ids"]),
                    interval_seconds=int(self.config.get("remote_watch_interval_seconds") or DEFAULT_REMOTE_WATCH_INTERVAL_SECONDS),
                    cooldown_seconds=600,
                    max_failures=3,
                )
            elif action == "remote_watch_stop":
                message = lab.remote_watch_stop()
            elif action == "supervise_once":
                message = lab.supervise_running_remote_tasks(model=str(self.config["model"]), collect_finished=True)
            elif action == "dependency_reconcile":
                message = _dashboard_action_request(
                    "dependency_reconcile_apply_safe",
                    reason="Reconcile dependencies from dashboard",
                    safe_level="safe_after_revalidation",
                )
            elif action == "refresh_gpu":
                message = lab.remote_gpu_snapshot(
                    str(self.config["host"]),
                    allowed_gpu_ids=str(self.config["allowed_gpu_ids"]).split(","),
                    force_refresh=True,
                )
            elif action == "local_init":
                message = lab.local_environment_init(create_dirs=True)
            elif action == "remote_preflight":
                message = lab.remote_target_preflight(
                    host=str(self.config["host"]),
                    remote_workdir=str(self.config["remote_workdir"]),
                    remote_root=str(self.config["remote_root"]),
                    allowed_gpu_ids=str(self.config["allowed_gpu_ids"]).split(","),
                )
            elif action == "workplace_init":
                message = lab.remote_workplace_init(
                    host=str(self.config["host"]),
                    remote_workdir=str(self.config["remote_workdir"]),
                    remote_root=str(self.config["remote_root"]),
                    allowed_gpu_ids=str(self.config["allowed_gpu_ids"]).split(","),
                    write_marker=True,
                    sync_profile=True,
                    codex_smoke=False,
                )
            elif action == "schedule_once":
                message = _dashboard_action_request(
                    "schedule_once",
                    payload={
                        "host": str(self.config["host"]),
                        "remote_root": str(self.config["remote_root"]),
                        "remote_workdir": str(self.config["remote_workdir"]),
                        "max_starts": int(self.config["max_starts_per_cycle"]),
                        "max_running_total": int(self.config["max_running_total"]),
                        "max_running_gpu": int(self.config["max_running_gpu"]),
                        "max_running_cpu": int(self.config["max_running_cpu"]),
                        "min_free_gpu_mem_gb": float(self.config["min_free_gpu_mem_gb"]),
                        "allowed_gpu_ids": [item.strip() for item in str(self.config["allowed_gpu_ids"]).split(",") if item.strip()],
                        "max_tasks_per_gpu": int(self.config["max_tasks_per_gpu"]),
                        "worker": str(self.config.get("remote_worker") or DEFAULT_REMOTE_WORKER),
                    },
                    reason="Schedule once requested from dashboard",
                    safe_level="requires_launch_approval",
                )
            elif action == "remote_monitor_task":
                task_id = (form.get("task_id") or [""])[0]
                monitor_host = (form.get("host") or [str(self.config["host"])])[0]
                monitor_root = (form.get("remote_root") or [str(self.config["remote_root"])])[0]
                message = lab.remote_monitor_once(
                    task_id,
                    host=str(monitor_host or self.config["host"]),
                    remote_root=str(monitor_root or self.config["remote_root"]),
                )
            elif action == "remote_stop":
                task_id = (form.get("task_id") or [""])[0]
                message = _dashboard_action_request(
                    "remote_stop",
                    task_id=task_id,
                    payload={"host": str(self.config["host"]), "remote_root": str(self.config["remote_root"])},
                    reason="Remote stop requested from dashboard",
                    safe_level="dangerous_requires_explicit_approval",
                )
            elif action == "requeue_partial":
                task_id = (form.get("task_id") or [""])[0]
                message = _dashboard_action_request(
                    "requeue_partial",
                    task_id=task_id,
                    reason="Requeued partial task from dashboard",
                    safe_level="dangerous_requires_explicit_approval",
                    expected_task_status="partial",
                )
            elif action == "use_partial_continue_downstream":
                task_id = (form.get("task_id") or [""])[0]
                message = _use_partial_continue_downstream(task_id)
            elif action == "requeue_partial_chain":
                task_id = (form.get("task_id") or [""])[0]
                message = _requeue_partial_chain(task_id)
            elif action == "advisor_feedback":
                feedback = (form.get("feedback") or [""])[0]
                _save_advisor_feedback(feedback)
                message = {"ok": True, "saved": str(_advisor_feedback_path()), "note": "Advisor feedback saved for the next rewrite/proposal."}
            elif action == "explain_why_ai":
                task_id = (form.get("task_id") or [""])[0]
                queue_item: Optional[Dict[str, object]] = None
                for item in lab.queue():
                    if str(item.get("task_id") or "") == task_id:
                        queue_item = item
                        break
                record = lab.status(task_id)
                plan = _schedule_dry_run(self.config)
                dep = queue_item.get("dependency_state") if isinstance(queue_item, dict) else {}
                deps = dep if isinstance(dep, dict) else {}
                skipped = None
                for item in plan.get("skipped", []) or []:
                    if str(item.get("task_id") or "") == task_id:
                        skipped = item
                        break
                diagnosis = _why_diagnosis(task_id, record, queue_item, deps, skipped, plan)
                requeue_guidance = _requeue_guidance(task_id, record)
                context = {
                    "diagnosis": diagnosis,
                    "requeue_guidance": requeue_guidance,
                    "record": {key: record.get(key) for key in ("task_id", "title", "status", "summary_short", "latest_run_id", "remote_state", "last_remote_seen_at")},
                    "dependency_state": deps,
                    "scheduler_skip": skipped,
                    "capacity": plan.get("capacity"),
                    "limits": plan.get("limits"),
                    "next_to_start": plan.get("plan", []) or [],
                }
                message = _generate_why_ai(task_id, context, model=str(self.config["model"]))
            elif action == "summarize_result_ai":
                task_id = (form.get("task_id") or [""])[0]
                message = _generate_result_ai(task_id, model=str(self.config["model"]))
            elif action == "clear_completed":
                completed = [str(record.get("task_id")) for record in _records_by_status().get("completed", []) if record.get("task_id")]
                _write_hidden_completed([*_hidden_completed(), *completed])
                message = {"ok": True, "hidden_completed_count": len(completed), "note": "Completed tasks are hidden from the board only; results and registry were not deleted."}
            elif action == "show_completed":
                _write_hidden_completed([])
                message = {"ok": True, "note": "Completed tasks are visible again."}
            elif action == "hide_attention_task":
                task_id = (form.get("task_id") or [""])[0]
                hidden = _hidden_attention()
                if task_id:
                    hidden.add(task_id)
                _write_hidden_attention(hidden)
                message = {"ok": True, "task_id": task_id, "hidden_attention_count": len(hidden), "note": "Hidden from dashboard attention panels only; no task files or results were deleted."}
            elif action == "hide_attention_group":
                task_ids = [str(item) for item in form.get("task_id", []) if str(item)]
                hidden = _hidden_attention()
                for item in task_ids:
                    hidden.add(item)
                _write_hidden_attention(hidden)
                message = {
                    "ok": True,
                    "task_ids": task_ids,
                    "hidden_group_count": len(task_ids),
                    "hidden_attention_count": len(hidden),
                    "note": "Hidden from dashboard attention panels only; no task files or results were deleted.",
                }
            elif action == "bulk_hide_attention":
                task_ids = [str(item) for item in form.get("task_id", []) if str(item)]
                hidden = _hidden_attention()
                for item in task_ids:
                    hidden.add(item)
                _write_hidden_attention(hidden)
                message = {
                    "ok": True,
                    "task_ids": task_ids,
                    "hidden_count": len(task_ids),
                    "hidden_attention_count": len(hidden),
                    "note": "Hidden from dashboard attention panels only; no task files or results were deleted.",
                }
            elif action == "bulk_hide_completed":
                task_ids = [str(item) for item in form.get("task_id", []) if str(item)]
                hidden = _hidden_completed()
                for item in task_ids:
                    hidden.add(item)
                _write_hidden_completed(hidden)
                message = {
                    "ok": True,
                    "task_ids": task_ids,
                    "hidden_count": len(task_ids),
                    "hidden_completed_count": len(hidden),
                    "note": "Completed tasks are hidden from the board only; results and registry were not deleted.",
                }
            elif action == "show_attention":
                restored_count = len(_hidden_attention())
                _write_hidden_attention([])
                message = {"ok": True, "restored_count": restored_count, "note": "Hidden attention tasks are visible again."}
            elif action == "save_remote_config":
                updated = dict(self.config)
                text_keys = ["host", "remote_workdir", "remote_root", "allowed_gpu_ids", "cuda_mapping_note", "remote_worker"]
                int_keys = [
                    "max_starts_per_cycle",
                    "max_running_total",
                    "max_running_gpu",
                    "max_running_cpu",
                    "max_tasks_per_gpu",
                    "interval_minutes",
                    "gpu_snapshot_cache_seconds",
                    "remote_watch_interval_seconds",
                ]
                float_keys = ["min_free_gpu_mem_gb"]
                for key in text_keys:
                    if key == "cuda_mapping_note":
                        updated[key] = (form.get(key) or [""])[0].strip()
                        continue
                    value = (form.get(key) or [updated.get(key) or ""])[0].strip()
                    if value:
                        updated[key] = value
                for key in int_keys:
                    raw = (form.get(key) or [updated.get(key)])[0]
                    if raw in (None, ""):
                        continue
                    updated[key] = int(raw)
                for key in float_keys:
                    raw = (form.get(key) or [updated.get(key)])[0]
                    if raw in (None, ""):
                        continue
                    updated[key] = float(raw)
                self.config.update(updated)
                _remember_remote_preset(self.config)
                _save_dashboard_config(self.config)
                message = {
                    "ok": True,
                    "host": self.config.get("host"),
                    "remote_workdir": self.config.get("remote_workdir"),
                    "remote_root": self.config.get("remote_root"),
                    "allowed_gpu_ids": self.config.get("allowed_gpu_ids"),
                    "cuda_mapping_note": self.config.get("cuda_mapping_note"),
                    "remote_worker": self.config.get("remote_worker"),
                    "note": "Future dashboard actions will use this target.",
                }
            elif action == "delete_remote_preset":
                raw_index = (form.get("preset_index") or ["-1"])[0]
                deleted = _delete_remote_preset(self.config, int(raw_index))
                _save_dashboard_config(self.config)
                message = {
                    "ok": deleted,
                    "deleted": deleted,
                    "custom_preset_count": len(_custom_remote_presets(self.config)),

                }
            elif action == "hide_history_workplace":
                host = (form.get("host") or [""])[0].strip()
                remote_root = (form.get("remote_root") or [""])[0].strip()
                hidden = _hide_history_workplace(self.config, host, remote_root)
                _save_dashboard_config(self.config)
                message = {
                    "ok": hidden,
                    "host": host,
                    "remote_root": remote_root,
                    "hidden_history_count": len(_hidden_history_workplaces(self.config)),
                    "note": "Hidden from Workplaces UI only; remote files, local task history, and results were not deleted.",
                }
            elif action == "show_hidden_history_workplaces":
                restored_count = _show_hidden_history_workplaces(self.config)
                _save_dashboard_config(self.config)
                message = {
                    "ok": True,
                    "restored_count": restored_count,
                    "note": "Hidden history workplaces are visible again.",
                }
            elif action == "show_history_workplace":
                host = (form.get("host") or [""])[0].strip()
                remote_root = (form.get("remote_root") or [""])[0].strip()
                restored = _show_history_workplace(self.config, host, remote_root)
                _save_dashboard_config(self.config)
                message = {
                    "ok": restored,
                    "restored": restored,
                    "host": host,
                    "remote_root": remote_root,
                    "hidden_history_count": len(_hidden_history_workplaces(self.config)),
                    "note": "History workplace is visible again.",
                }
            elif action == "bulk_history_workplaces":
                operation = (form.get("bulk_operation") or [""])[0].strip()
                if operation not in {"forget", "restore"}:
                    raise ValueError("bulk_operation must be forget or restore")
                keys = [str(item) for item in form.get("workplace_key", [])]
                message = _bulk_history_workplaces(self.config, keys, operation)
                _save_dashboard_config(self.config)
            elif action == "local_cleanup_plan":
                categories = [str(item) for item in form.get("cleanup_category", [])]
                message = _local_cleanup_plan(categories)
            elif action == "local_cleanup_delete":
                categories = [str(item) for item in form.get("cleanup_category", [])]
                message = _delete_local_cleanup_categories(categories)
            elif action == "sync_profile":
                message = lab.remote_bootstrap_context(host=str(self.config["host"]), remote_workdir=str(self.config["remote_workdir"]))
                message = {
                    "ok": True,
                    "host": self.config.get("host"),
                    "remote_workdir": self.config.get("remote_workdir"),
                    "bootstrap": message,
                }
            elif action == "inbox_promote_task":
                line_name = (form.get("line") or [""])[0]
                task_id = (form.get("task_id") or [""])[0]
                message = lab.promote_from_staging(line_name=line_name, task_ids=[task_id])
            elif action == "inbox_promote_line":
                line_name = (form.get("line") or [""])[0]
                message = lab.promote_from_staging(line_name=line_name)
            elif action == "inbox_discard_task":
                line_name = (form.get("line") or [""])[0]
                task_id = (form.get("task_id") or [""])[0]
                message = lab.discard_from_staging(line_name=line_name, task_ids=[task_id])
            elif action == "inbox_discard_line":
                line_name = (form.get("line") or [""])[0]
                message = lab.discard_from_staging(line_name=line_name)
            elif action == "inbox_discard_anomaly":
                line_name = (form.get("line") or [""])[0]
                message = lab.discard_from_staging(line_name=line_name)
            else:
                message = {"error": f"unknown action: {action}"}
        except Exception:
            message = {"error": traceback.format_exc()}
        if self.headers.get("X-Dashboard-Ajax") == "1":
            payload = json.dumps(_action_result_model(action, message), ensure_ascii=False, default=str).encode("utf-8")
            self._send(payload, status=500 if message.get("error") else 200, content_type="application/json; charset=utf-8")
            return
        _write_dashboard_flash(_action_result_model(action, message))
        redirect_to = "/"
        referer = self.headers.get("Referer") or ""
        try:
            parsed_referer = urllib.parse.urlparse(referer)
            if parsed_referer.path and parsed_referer.path != "/action":
                redirect_to = parsed_referer.path
                if parsed_referer.query:
                    redirect_to += "?" + parsed_referer.query
        except Exception:
            redirect_to = "/"
        self.send_response(303)
        self.send_header("Location", redirect_to)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        log_path = state_dir() / "dashboard_access.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("%s - %s\n" % (self.address_string(), format % args))


def serve_dashboard(
    bind: str = "127.0.0.1",
    port: int = 7860,
    host: str = DEFAULT_HOST,
    remote_root: str = DEFAULT_REMOTE_ROOT,
    remote_workdir: str = DEFAULT_REMOTE_WORKDIR,
    allowed_gpu_ids: str = DEFAULT_ALLOWED_GPUS,
    min_free_gpu_mem_gb: float = 10.0,
    max_starts_per_cycle: int = 1,
    max_running_total: int = 8,
    max_running_gpu: int = 4,
    max_running_cpu: int = 4,
    max_tasks_per_gpu: int = 2,
    interval_minutes: int = 5,
    model: str = DEFAULT_OLLAMA_MODEL,
    open_browser: bool = False,
    shutdown_on_idle_seconds: int = 0,
) -> None:
    config: Dict[str, object] = {
        "host": host,
        "remote_root": remote_root,
        "remote_workdir": remote_workdir,
        "allowed_gpu_ids": allowed_gpu_ids,
        "min_free_gpu_mem_gb": min_free_gpu_mem_gb,
        "max_starts_per_cycle": max_starts_per_cycle,
        "max_running_total": max_running_total,
        "max_running_gpu": max_running_gpu,
        "max_running_cpu": max_running_cpu,
        "max_tasks_per_gpu": max_tasks_per_gpu,
        "interval_minutes": interval_minutes,
        "model": model,
        "gpu_snapshot_cache_seconds": DEFAULT_GPU_SNAPSHOT_CACHE_SECONDS,
        "remote_watch_interval_seconds": DEFAULT_REMOTE_WATCH_INTERVAL_SECONDS,
        "remote_worker": DEFAULT_REMOTE_WORKER,
    }
    config.update(_load_dashboard_config())
    DashboardHandler.config = config
    DashboardHandler.last_client_ping = time.time()
    DashboardHandler.shutdown_on_idle_seconds = max(0, int(shutdown_on_idle_seconds or 0))
    server = ThreadingHTTPServer((bind, port), DashboardHandler)
    DashboardHandler.server_ref = server
    url = f"http://{bind}:{port}"
    if DashboardHandler.shutdown_on_idle_seconds:
        def _idle_shutdown() -> None:
            while True:
                time.sleep(min(5, max(1, DashboardHandler.shutdown_on_idle_seconds / 3)))
                idle_seconds = time.time() - DashboardHandler.last_client_ping
                if idle_seconds >= DashboardHandler.shutdown_on_idle_seconds:
                    print(f"Dashboard idle for {int(idle_seconds)}s; shutting down.", flush=True)
                    server.shutdown()
                    break

        threading.Thread(target=_idle_shutdown, daemon=True).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Dashboard listening on http://{bind}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

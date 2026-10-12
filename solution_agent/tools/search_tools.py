from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


DEFAULT_BACKEND_ORDER = ["rg", "git_grep", "powershell_select_string", "python_fallback"]
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp",
    ".test_tmp",
    "outputs",
    "runs",
    "results",
    "reports",
    "checkpoints",
    "logs",
    "worktrees",
}
DEFAULT_EXCLUDE_EXT = {
    ".pt",
    ".pth",
    ".ckpt",
    ".safetensors",
    ".onnx",
    ".bin",
    ".log",
    ".zip",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
}
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_RESULTS = 200
DEFAULT_MAX_FILE_SIZE_MB = 2.0


@dataclass(frozen=True)
class SearchPolicy:
    backend_order: list[str]
    max_files: int
    max_results: int
    max_file_size_mb: float
    exclude_dirs: set[str]
    exclude_ext: set[str]
    ignore_patterns: list[str]


def load_search_policy(repo_root: Path) -> SearchPolicy:
    repo_root = repo_root.resolve()
    policy_path = repo_root / ".solution" / "context" / "tool_policy.yaml"
    raw: dict[str, Any] = {}
    if policy_path.exists():
        try:
            loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8-sig")) or {}
            raw = loaded if isinstance(loaded, dict) else {}
        except Exception:
            raw = {}
    search = raw.get("search") if isinstance(raw.get("search"), dict) else {}
    ignore_patterns = _load_solutionignore(repo_root)
    return SearchPolicy(
        backend_order=[str(item) for item in search.get("backend_order", DEFAULT_BACKEND_ORDER) if str(item)],
        max_files=int(search.get("max_files") or DEFAULT_MAX_FILES),
        max_results=int(search.get("max_results") or DEFAULT_MAX_RESULTS),
        max_file_size_mb=float(search.get("max_file_size_mb") or DEFAULT_MAX_FILE_SIZE_MB),
        exclude_dirs={str(item).strip() for item in search.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS) if str(item).strip()},
        exclude_ext={_normalize_ext(str(item)) for item in search.get("exclude_ext", DEFAULT_EXCLUDE_EXT) if str(item).strip()},
        ignore_patterns=ignore_patterns,
    )


def probe_search_backends(repo_root: Path, *, write_state: bool = True) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    policy = load_search_policy(repo_root)
    backends = {
        "rg": _probe_command(["rg", "--version"], cwd=repo_root),
        "git_grep": _probe_git_grep(repo_root),
        "powershell_select_string": _probe_powershell(),
        "python_fallback": {"available": True, "error": ""},
    }
    selected = next((name for name in policy.backend_order if backends.get(name, {}).get("available")), "python_fallback")
    payload = {
        "schema_version": 1,
        "platform": platform.system(),
        "backend_order": policy.backend_order,
        "search_backends": backends,
        "selected_search_backend": selected,
    }
    if write_state:
        state_path = repo_root / "state" / "tool_probe.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def safe_search(
    repo_root: Path,
    pattern: str,
    *,
    root: str | Path = ".",
    backend: str = "auto",
    max_results: int | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    policy = load_search_policy(repo_root)
    search_root = _resolve_search_root(repo_root, root)
    limit = int(max_results or policy.max_results)
    probe = probe_search_backends(repo_root, write_state=True)
    selected = backend if backend != "auto" else str(probe.get("selected_search_backend") or "python_fallback")
    if selected not in DEFAULT_BACKEND_ORDER:
        return {"ok": False, "error": f"unknown search backend: {selected}", "results": []}

    runners = {
        "rg": _search_rg,
        "git_grep": _search_git_grep,
        "powershell_select_string": _search_powershell,
        "python_fallback": _search_python,
    }
    attempted: list[dict[str, Any]] = []
    for name in _backend_attempt_order(selected, policy.backend_order):
        if not probe["search_backends"].get(name, {}).get("available"):
            attempted.append({"backend": name, "ok": False, "skipped": True, "error": probe["search_backends"].get(name, {}).get("error")})
            continue
        result = runners[name](repo_root, search_root, pattern, policy, limit)
        attempted.append({"backend": name, "ok": bool(result.get("ok")), "error": result.get("error", "")})
        if result.get("ok"):
            result.update(
                {
                    "schema_version": 1,
                    "backend": name,
                    "selected_search_backend": selected,
                    "attempted_backends": attempted,
                    "root": _rel(search_root, repo_root),
                    "pattern": pattern,
                    "max_results": limit,
                }
            )
            return result
        if backend != "auto":
            break
    return {
        "schema_version": 1,
        "ok": False,
        "backend": selected,
        "pattern": pattern,
        "root": _rel(search_root, repo_root),
        "results": [],
        "result_count": 0,
        "attempted_backends": attempted,
        "error": "no search backend succeeded",
    }


def _backend_attempt_order(selected: str, policy_order: list[str]) -> list[str]:
    if selected != "auto" and selected in policy_order:
        ordered = policy_order[policy_order.index(selected) :]
    else:
        ordered = policy_order
    if "python_fallback" not in ordered:
        ordered.append("python_fallback")
    return ordered


def _probe_command(command: list[str], *, cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False)
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    return {
        "available": proc.returncode == 0,
        "error": "" if proc.returncode == 0 else (proc.stderr.strip() or proc.stdout.strip())[:500],
        "version": (proc.stdout.splitlines() or [""])[0],
    }


def _probe_git_grep(repo_root: Path) -> dict[str, Any]:
    try:
        rev = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(repo_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False)
        if rev.returncode != 0:
            return {"available": False, "error": rev.stderr.strip()[:500] or "not a git worktree"}
        proc = subprocess.run(["git", "grep", "-n", "--max-count=1", "definitely_unlikely_probe_string_7f38"], cwd=str(repo_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False)
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    return {"available": proc.returncode in {0, 1}, "error": "" if proc.returncode in {0, 1} else proc.stderr.strip()[:500]}


def _probe_powershell() -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"available": False, "error": "not windows"}
    for exe in ("powershell", "pwsh"):
        try:
            proc = subprocess.run([exe, "-NoProfile", "-Command", "Get-Command Select-String | Select-Object -ExpandProperty Name"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False)
        except Exception:
            continue
        if proc.returncode == 0 and "Select-String" in proc.stdout:
            return {"available": True, "error": "", "executable": exe}
    return {"available": False, "error": "Select-String unavailable"}


def _search_rg(repo_root: Path, search_root: Path, pattern: str, policy: SearchPolicy, limit: int) -> dict[str, Any]:
    command = ["rg", "--line-number", "--no-heading", "--color", "never"]
    for directory in sorted(policy.exclude_dirs):
        command.extend(["--glob", f"!{directory}/**"])
    for ext in sorted(policy.exclude_ext):
        command.extend(["--glob", f"!*{ext}"])
    for glob in policy.ignore_patterns:
        command.extend(["--glob", f"!{glob}"])
    command.extend([pattern, _rel(search_root, repo_root)])
    try:
        proc = subprocess.run(command, cwd=str(repo_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "results": []}
    if proc.returncode not in {0, 1}:
        return {"ok": False, "error": proc.stderr.strip()[:1000], "results": []}
    return _result_from_lines(repo_root, proc.stdout.splitlines(), limit)


def _search_git_grep(repo_root: Path, search_root: Path, pattern: str, policy: SearchPolicy, limit: int) -> dict[str, Any]:
    rel_root = _rel(search_root, repo_root)
    pathspecs = [rel_root if rel_root != "." else "."]
    command = ["git", "grep", "-n", "-I", "-e", pattern, "--", *pathspecs]
    try:
        proc = subprocess.run(command, cwd=str(repo_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "results": []}
    if proc.returncode not in {0, 1}:
        return {"ok": False, "error": proc.stderr.strip()[:1000], "results": []}
    lines = []
    for line in proc.stdout.splitlines():
        path = line.split(":", 1)[0]
        if _path_allowed(repo_root / path, repo_root, policy):
            lines.append(line)
    return _result_from_lines(repo_root, lines, limit)


def _search_powershell(repo_root: Path, search_root: Path, pattern: str, policy: SearchPolicy, limit: int) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"ok": False, "error": "not windows", "results": []}
    files = [path for path in _iter_candidate_files(repo_root, search_root, policy)]
    results: list[dict[str, Any]] = []
    regex = re.compile(pattern)
    for path in files:
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if regex.search(line):
                    results.append({"path": _rel(path, repo_root), "line": number, "text": line})
                    if len(results) >= limit:
                        return {"ok": True, "results": results, "result_count": len(results)}
        except OSError:
            continue
    return {"ok": True, "results": results, "result_count": len(results)}


def _search_python(repo_root: Path, search_root: Path, pattern: str, policy: SearchPolicy, limit: int) -> dict[str, Any]:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))
    results: list[dict[str, Any]] = []
    for path in _iter_candidate_files(repo_root, search_root, policy):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append({"path": _rel(path, repo_root), "line": number, "text": line})
                if len(results) >= limit:
                    return {"ok": True, "results": results, "result_count": len(results)}
    return {"ok": True, "results": results, "result_count": len(results)}


def _iter_candidate_files(repo_root: Path, search_root: Path, policy: SearchPolicy) -> Iterable[Path]:
    max_bytes = int(policy.max_file_size_mb * 1024 * 1024)
    count = 0
    for path in sorted(search_root.rglob("*")):
        if count >= policy.max_files:
            break
        if not path.is_file():
            continue
        if not _path_allowed(path, repo_root, policy):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        count += 1
        yield path


def _path_allowed(path: Path, repo_root: Path, policy: SearchPolicy) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    parts = set(Path(rel).parts)
    if parts & policy.exclude_dirs:
        return False
    if path.suffix.lower() in policy.exclude_ext:
        return False
    return not any(fnmatch.fnmatch(rel, pattern.replace("\\", "/")) for pattern in policy.ignore_patterns)


def _result_from_lines(repo_root: Path, lines: list[str], limit: int) -> dict[str, Any]:
    results = []
    for line in lines[:limit]:
        path, line_no, text = _parse_grep_line(line)
        results.append({"path": _rel(repo_root / path, repo_root), "line": line_no, "text": text})
    return {"ok": True, "results": results, "result_count": len(results)}


def _parse_grep_line(line: str) -> tuple[str, int, str]:
    first, sep, rest = line.partition(":")
    if not sep:
        return line, 0, ""
    second, sep, text = rest.partition(":")
    try:
        return first, int(second), text
    except ValueError:
        return first, 0, rest


def _resolve_search_root(repo_root: Path, root: str | Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"search root must be under repo root: {resolved}") from exc
    if not resolved.exists():
        raise FileNotFoundError(f"search root does not exist: {resolved}")
    return resolved


def _load_solutionignore(repo_root: Path) -> list[str]:
    path = repo_root / ".solutionignore"
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped.replace("\\", "/"))
    return patterns


def _normalize_ext(value: str) -> str:
    value = value.strip().lower()
    return value if value.startswith(".") else f".{value}"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()

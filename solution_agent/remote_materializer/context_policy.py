from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTEXT_LEVELS = {"materialize", "prepare_config", "patch_low_risk", "unknown_or_high_risk"}

POLICY: dict[str, dict[str, Any]] = {
    "materialize": {
        "source_context": "none",
        "allowed_paths": [],
        "forbidden_paths": ["models/**", "losses/**", "datasets/**", "checkpoints/**", "train.py"],
        "action": "materialize",
    },
    "prepare_config": {
        "source_context": "selected_snippets",
        "allowed_paths": ["configs/**", "templates/**", "scripts/build_*.py", "scripts/parse_*.py"],
        "forbidden_paths": ["models/**", "losses/**", "datasets/**", "checkpoints/**", "train.py"],
        "action": "prepare_config",
    },
    "patch_low_risk": {
        "source_context": "exact_allowed_files",
        "allowed_paths": [
            "tools/**",
            "validators/**",
            "templates/**",
            "scripts/parse_*.py",
            "scripts/build_*.py",
            "scripts/eval_*.py",
        ],
        "forbidden_paths": ["models/**", "losses/**", "datasets/**", "checkpoints/**", "train.py"],
        "action": "patch",
    },
    "unknown_or_high_risk": {
        "source_context": "none",
        "allowed_paths": [],
        "forbidden_paths": ["**"],
        "action": "codex_fallback",
    },
}


@dataclass
class PathDecision:
    path: str
    allowed: bool
    reason: str


def normalize_repo_path(value: str) -> str | None:
    if not value or "\x00" in value:
        return None
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    return "/".join(path.parts)


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def decide_path(context_level: str, path: str) -> PathDecision:
    normalized = normalize_repo_path(path)
    if normalized is None:
        return PathDecision(path=path, allowed=False, reason="path_escape_or_absolute")
    policy = POLICY.get(context_level)
    if policy is None:
        return PathDecision(path=normalized, allowed=False, reason=f"unknown_context_level:{context_level}")
    if matches_any(normalized, list(policy["forbidden_paths"])):
        return PathDecision(path=normalized, allowed=False, reason="forbidden_path")
    if context_level == "materialize":
        return PathDecision(path=normalized, allowed=False, reason="materialize_disallows_source_context")
    if not matches_any(normalized, list(policy["allowed_paths"])):
        return PathDecision(path=normalized, allowed=False, reason="not_allowed_by_context_level")
    return PathDecision(path=normalized, allowed=True, reason="allowed")


def allowed_paths_for_level(context_level: str) -> list[str]:
    return list(POLICY.get(context_level, {}).get("allowed_paths", []))


def forbidden_paths_for_level(context_level: str) -> list[str]:
    return list(POLICY.get(context_level, {}).get("forbidden_paths", []))


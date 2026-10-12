from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from ..utils.paths import project_root


@lru_cache(maxsize=1)
def _load_policy() -> Dict[str, Any]:
    path = project_root() / "policies" / "command_policy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def default_timeout_sec() -> int:
    return int(_load_policy()["limits"]["default_timeout_sec"])


def max_timeout_sec() -> int:
    return int(_load_policy()["limits"]["max_timeout_sec"])


def _prefix_matches(command: str, prefixes: List[List[str]]) -> bool:
    lowered = command.strip().lower()
    for prefix in prefixes:
        token = " ".join(prefix).lower()
        if lowered == token or lowered.startswith(token + " "):
            return True
    return False


def classify_command(command: str) -> Tuple[str, str]:
    policy = _load_policy()
    for pattern in policy["commands"].get("deny_patterns", []):
        if pattern.lower() in command.lower():
            return "deny", f"Denied by policy: {pattern}"

    if _prefix_matches(command, policy["commands"].get("allow_prefixes", [])):
        return "allow", "allowed"

    if _prefix_matches(command, policy["commands"].get("require_approval_prefixes", [])):
        return "approval", "Command requires approval by policy."

    return "deny", "Command prefix is not allowlisted."


def check_command_allowed(command: str) -> Tuple[bool, str]:
    decision, reason = classify_command(command)
    return decision == "allow", reason

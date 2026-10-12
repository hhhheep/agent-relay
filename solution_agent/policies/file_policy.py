from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

from ..utils.paths import project_root


@lru_cache(maxsize=1)
def _load_policy() -> Dict[str, Any]:
    path = project_root() / "policies" / "file_policy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _is_denied_path(resolved: Path) -> bool:
    policy = _load_policy()["filesystem"]
    deny_exact = {Path(value).resolve() for value in policy.get("deny_exact_paths", [])}
    if resolved in deny_exact:
        return True
    deny_suffixes = tuple(policy.get("deny_suffixes", []))
    return any(part.endswith(deny_suffixes) for part in resolved.parts)


def assert_can_write(path: Path) -> None:
    root = project_root().resolve()
    resolved = path.resolve()
    if _is_denied_path(resolved):
        raise PermissionError(f"Write denied by deny list: {resolved}")

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Write denied outside project root: {resolved}") from exc

    relative = resolved.relative_to(root)
    top_level = relative.parts[0] if relative.parts else ""
    allowed = set(_load_policy()["filesystem"]["allowed_write_dirs"])
    if top_level not in allowed:
        raise PermissionError(f"Write denied by file policy: {resolved}")

    read_only = set(_load_policy()["filesystem"].get("read_only_dirs", []))
    if top_level in read_only:
        raise PermissionError(f"Write denied in read-only directory: {resolved}")

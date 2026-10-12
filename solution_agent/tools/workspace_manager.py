from __future__ import annotations

from pathlib import Path

from ..utils.paths import project_root


def ensure_workspace(name: str) -> Path:
    workspace = project_root() / "workspaces" / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace

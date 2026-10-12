from __future__ import annotations

from pathlib import Path

import yaml

from ..registry.db import init_registry_db, upsert_tool_record


def register_tool_directory(tool_dir: Path) -> dict:
    tool_yaml = tool_dir / "tool.yaml"
    if not tool_yaml.exists():
        raise FileNotFoundError(f"Missing tool.yaml in {tool_dir}")

    payload = yaml.safe_load(tool_yaml.read_text(encoding="utf-8"))
    init_registry_db()
    upsert_tool_record(
        name=str(payload["name"]),
        version=str(payload["version"]),
        description=str(payload.get("description", "")),
        path=str(tool_dir),
        tags=",".join(payload.get("tags", [])),
    )
    return {
        "registered": True,
        "name": payload["name"],
        "version": payload["version"],
        "path": str(tool_dir),
    }

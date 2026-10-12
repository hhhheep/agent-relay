from __future__ import annotations

import sqlite3
from pathlib import Path

from ..utils.paths import project_root


def registry_db_path() -> Path:
    return project_root() / "registry" / "tools.sqlite"


def init_registry_db() -> Path:
    path = registry_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tools (
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                description TEXT,
                path TEXT NOT NULL,
                tags TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (name, version)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def upsert_tool_record(name: str, version: str, description: str, path: str, tags: str) -> None:
    conn = sqlite3.connect(registry_db_path())
    try:
        conn.execute(
            """
            INSERT INTO tools (name, version, description, path, tags)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name, version) DO UPDATE SET
                description=excluded.description,
                path=excluded.path,
                tags=excluded.tags,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, version, description, path, tags),
        )
        conn.commit()
    finally:
        conn.close()

"""Atomic file write helpers for execution_port state files."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write tmp → fsync → rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=2, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    try:
        os.fsync(tmp.open("rb").fileno())
    except (OSError, AttributeError):
        pass  # best-effort fsync
    tmp.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically: write tmp → rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    """Read JSON; return {} if file missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

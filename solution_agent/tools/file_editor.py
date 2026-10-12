from __future__ import annotations

from pathlib import Path

from ..policies.file_policy import assert_can_write
from .patch_writer import build_unified_diff


def write_text_file(path: Path, content: str) -> dict:
    assert_can_write(path)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    after = content
    return {
        "path": str(path),
        "changed": before != after,
        "diff": build_unified_diff(path, before, after),
    }


def replace_in_file(path: Path, old: str, new: str) -> dict:
    assert_can_write(path)
    if not path.exists():
        raise FileNotFoundError(path)
    before = path.read_text(encoding="utf-8")
    if old not in before:
        raise ValueError("Target text not found in file.")
    after = before.replace(old, new, 1)
    path.write_text(after, encoding="utf-8")
    return {
        "path": str(path),
        "changed": before != after,
        "diff": build_unified_diff(path, before, after),
    }

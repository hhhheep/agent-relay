from __future__ import annotations

from difflib import unified_diff
from pathlib import Path
import subprocess
import tempfile


def build_unified_diff(path: Path, before: str, after: str) -> str:
    if before and not before.endswith("\n"):
        before += "\n"
    if after and not after.endswith("\n"):
        after += "\n"
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path.as_posix()}",
        tofile=f"b/{path.as_posix()}",
    )
    return "".join(diff)


def apply_diff(path: Path, diff_text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
        temp_file.write(diff_text)
        temp_file_path = temp_file.name

    try:
        result = subprocess.run(
            ["git", "apply", temp_file_path],
            cwd=path.parent,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() if result.stderr else "git apply failed")
    finally:
        import os
        os.unlink(temp_file_path)

from __future__ import annotations

from pathlib import Path
from typing import Dict


def verify_run(workspace: Path, returncode: int) -> Dict[str, object]:
    return {
        "workspace_exists": workspace.exists(),
        "command_succeeded": returncode == 0,
        "workspace_path": str(workspace),
    }

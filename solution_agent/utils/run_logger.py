from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RunLogger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, filename: str, content: str) -> Path:
        path = self.run_dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, filename: str, payload: Any) -> Path:
        path = self.run_dir / filename
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

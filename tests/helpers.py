from __future__ import annotations

import os
import shutil
import stat
import time
import tempfile
from pathlib import Path


def _rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    def _make_writable_and_retry(func, failed_path, _exc_info) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception:
            if not ignore_errors:
                raise

    shutil.rmtree(path, ignore_errors=ignore_errors, onerror=_make_writable_and_retry)


class WorkspaceTempDir:
    def __enter__(self) -> Path:
        # Deep artifact names must fit Windows MAX_PATH even from a long checkout.
        self.root = Path(tempfile.gettempdir()) if os.name == "nt" else Path.cwd() / ".tmp" / "test_runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="sa-", dir=self.root))
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        root = self.root
        if hasattr(self, "path") and self.path.exists() and root in self.path.parents:
            for _ in range(10):
                try:
                    _rmtree(self.path)
                    return
                except PermissionError:
                    time.sleep(0.2)
            _rmtree(self.path, ignore_errors=True)

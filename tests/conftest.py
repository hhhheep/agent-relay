from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import WorkspaceTempDir


@pytest.fixture
def tmp_path() -> Path:
    """Isolated subprocess-readable paths, short enough for Windows artifacts."""
    with WorkspaceTempDir() as path:
        yield path

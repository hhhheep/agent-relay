from __future__ import annotations

import importlib.util
from pathlib import Path

from solution_agent.execution_port.script_registry import load_registry
from solution_agent.execution_port.spec import load_spec


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harness" / "run_fsfm_s0_inventory.py"
SPEC = importlib.util.spec_from_file_location("fsfm_s0_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_inventory_is_bounded_and_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(module.MAX_ENTRIES_PER_DIRECTORY + 1):
        (root / f"item_{index:03d}.jpg").write_bytes(b"not an image")
    result = module.inventory(root)
    assert result["truncated"] is True
    assert result["entry_count"] == module.MAX_ENTRIES_PER_DIRECTORY
    assert all(item["kind"] == "file" for item in result["entries"])


def test_source_presence_requires_all_expected_files(tmp_path: Path) -> None:
    assert module.source_inventory(tmp_path)["exists"] is False
    for relative in module.SOURCE_FILES:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
    result = module.source_inventory(tmp_path)
    assert result["exists"] is True
    assert all(item["sha256"] for item in result["files"].values())


def test_remote_spec_is_allowlisted_and_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(root / "solution_agent" / "execution_port" / "script_registry.yaml")
    spec = load_spec(root / "harness_specs" / "fan_fsfm_s0_inventory_20260927.yaml", registry)
    assert spec.command.script_id == "fsfm_s0_inventory"
    assert spec.resources.gpu_required is False

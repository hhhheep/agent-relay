from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

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
    spec = load_spec(root / "tests/fixtures/harness/inventory.execution_spec.yaml", registry)
    assert spec.command.script_id == "fsfm_s0_inventory"
    assert spec.resources.gpu_required is False


def test_inventory_repeat_uses_per_job_outputs_and_preserves_prior_artifacts(tmp_path: Path) -> None:
    from solution_agent.execution_port.registry import register_execution
    from solution_agent.execution_port.script_registry import build_argv

    root = SCRIPT.parents[2]
    registry_path = root / "solution_agent/execution_port/script_registry.yaml"
    registry = load_registry(registry_path)
    data = yaml.safe_load((root / "tests/fixtures/harness/inventory.execution_spec.yaml").read_text(encoding="utf-8"))
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "sample.txt").write_bytes(b"synthetic metadata")
    outputs = tmp_path / "artifacts"
    data["workspace"] = {"root": str(root), "cwd": str(root), "input_roots": [str(inputs)], "artifact_roots": [str(outputs)]}
    for name in ("ffpp_root", "dfdc_root", "celeb_df_v2_root", "repo_root"):
        data["command"]["args"][name] = str(inputs)
    data["command"]["args"]["output_dir"] = str(outputs / "{job_id}")
    data["artifacts"]["required"] = [str(outputs / "{job_id}" / Path(path).name) for path in data["artifacts"]["required"]]
    data["success_criteria"]["metrics_file"] = str(outputs / "{job_id}" / "fsfm_s0_inventory_metrics.json")
    spec_path = tmp_path / "inventory.yaml"
    spec_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    observed = []
    for _ in range(2):
        job = register_execution(spec_path, registry_path, tmp_path / "jobs")
        spec = load_spec(tmp_path / "jobs" / job / "execution_spec.yaml", registry)
        argv = build_argv(spec.command.script_id, spec.command.args, registry)
        argv[0] = sys.executable
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["metrics"]["contract_status"] == "pass"
        output = Path(spec.command.args["output_dir"])
        observed.append((output, {path.name: path.read_bytes() for path in output.iterdir()}, argv))
    assert observed[0][0] != observed[1][0]
    for output, expected, argv in observed:
        assert {path.name: path.read_bytes() for path in output.iterdir()} == expected
        repeated = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        assert repeated.returncode != 0
        assert "FileExistsError" in repeated.stderr
        assert {path.name: path.read_bytes() for path in output.iterdir()} == expected

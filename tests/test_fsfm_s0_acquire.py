from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from solution_agent.execution_port.script_registry import load_registry
from solution_agent.execution_port.spec import load_spec, SpecValidationError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harness" / "run_fsfm_s0_acquire.py"
SPEC = importlib.util.spec_from_file_location("fsfm_s0_acquire", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _public_spec(tmp_path: Path, **changes):
    data = yaml.safe_load((ROOT/'tests/fixtures/harness/acquisition.execution_spec.yaml').read_text(encoding='utf-8'))
    data['command']['args'].update(changes)
    path = tmp_path/'acquisition.yaml'
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    registry = load_registry(ROOT / "solution_agent" / "execution_port" / "script_registry.yaml")
    return load_spec(path, registry)


def test_acquisition_spec_is_allowlisted_without_gpu(tmp_path: Path) -> None:
    spec = _public_spec(tmp_path)
    assert spec.command.script_id == "fsfm_s0_acquire"
    assert spec.resources.gpu_required is False
    assert len(module.ASSETS) == 3


def test_source_only_spec_is_allowlisted_without_gpu(tmp_path: Path) -> None:
    spec = _public_spec(tmp_path, source_only=True)
    assert spec.command.args["source_only"] is True
    assert spec.resources.gpu_required is False


def test_assets_spec_reuses_declared_checkout_without_gpu(tmp_path: Path) -> None:
    spec = _public_spec(tmp_path)
    assert spec.command.script_id == "fsfm_s0_acquire"
    assert spec.command.args["checkout_dir"].endswith("/external/FSFM-CVPR25")
    assert spec.command.args.get("source_only") is not True
    assert spec.resources.gpu_required is False


@pytest.mark.parametrize('argument',['checkout_dir','weights_dir','output_dir'])
def test_acquisition_rejects_outputs_outside_declared_roots(tmp_path: Path, argument: str) -> None:
    with pytest.raises(SpecValidationError, match='outside workspace.root/artifact_roots'):
        _public_spec(tmp_path, **{argument:'/synthetic/outside/output'})


def test_existing_bad_checkpoint_stops_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "checkpoint-400.pth"
    target.write_bytes(b"not the pinned checkpoint")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module.asset(tmp_path, "checkpoint-400.pth", module.ASSETS[0][1])
    assert target.read_bytes() == b"not the pinned checkpoint"


def test_normalization_is_small_structured_text(tmp_path: Path) -> None:
    target = tmp_path / "pretrain_ds_mean_std.txt"
    target.write_text('{"mean":[0.1,0.2,0.3],"std":[0.4,0.5,0.6]}\n', encoding="utf-8")
    module.validate_normalization(target)
    target.write_bytes(b"x" * 4097)
    with pytest.raises(RuntimeError, match="small-text bound"):
        module.validate_normalization(target)

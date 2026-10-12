from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from solution_agent.execution_port.script_registry import load_registry
from solution_agent.execution_port.spec import load_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harness" / "run_fsfm_s0_boundary.py"
MODULE_SPEC = importlib.util.spec_from_file_location("fsfm_s0_boundary", SCRIPT)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reviewed_fsfm_s0_boundary_packet_is_registered_and_unchanged() -> None:
    registry = load_registry(ROOT / "solution_agent" / "execution_port" / "script_registry.yaml")
    spec_path = ROOT / "harness_specs" / "fan_fsfm_s0_boundary_20260928.yaml"
    runner = ROOT / "scripts" / "harness" / "run_fsfm_s0_boundary.py"
    spec = load_spec(spec_path, registry)

    assert spec.command.script_id == "fsfm_s0_boundary"
    assert spec.command.timeout_sec == 2700
    assert spec.resources.gpu_required is False
    assert registry["scripts"]["fsfm_s0_boundary"]["gpu_allowed"] is False
    assert _sha256(spec_path) == "cd28d0da38ba53bb8462eec5ae38f030c4ebfff8fcc44d58a1e08a5db653b5e2"
    assert _sha256(runner) == "709c77a696905f6570afb711ce4d808ad9fcbbe06921358cc04597af1955b89d"


def test_boundary_runner_keeps_the_reviewed_safety_anchors() -> None:
    text = (ROOT / "scripts" / "harness" / "run_fsfm_s0_boundary.py").read_text(encoding="utf-8")
    for anchor in (
        "checkpoint_body_downloaded\": False",
        "checkpoint_unpickled\": False",
        "target_results_read\": False",
        "researchflow_write_enabled\": False",
        "gpu_used\": False",
        "defer_pending_s0_data",
    ):
        assert anchor in text


def test_boundary_and_acquisition_use_the_official_dfd_engine_path() -> None:
    official = "fsfm-3c/finuetune/cross_dataset_DfD/engine_finetune.py"
    boundary = (ROOT / "scripts" / "harness" / "run_fsfm_s0_boundary.py").read_text(encoding="utf-8")
    acquisition = (ROOT / "scripts" / "harness" / "run_fsfm_s0_acquire.py").read_text(encoding="utf-8")
    assert official in boundary
    assert official in acquisition
    assert '"fsfm-3c/engine_finetune.py"' not in boundary


def test_boundary_spec_materializes_an_isolated_output_per_job() -> None:
    text = (ROOT / "harness_specs" / "fan_fsfm_s0_boundary_20260928.yaml").read_text(encoding="utf-8")
    assert text.count("{job_id}") == 11


def test_boundary_uses_hash_pinned_metadata_without_live_network() -> None:
    receipt, texts = module.verify_official_metadata()
    assert receipt["snapshot"]["network_required"] is False
    assert len(receipt["snapshot"]["sha256"]) == 64
    assert receipt["checkpoint_pointer"]["sha256"] == module.OFFICIAL_PAYLOAD_SHA256["checkpoint_pointer"]
    assert module.DETECTOR_SHA256 in texts["checkpoint_pointer"]
    assert "urllib.request" not in SCRIPT.read_text(encoding="utf-8")

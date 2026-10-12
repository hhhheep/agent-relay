from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import (
    dispatch_dir,
    registry_path,
    result_latest_dir,
    task_dir,
    worker_out_dir,
    worktree_dir,
)
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.small_task_worker.admission import check_opencoder_admission
from solution_agent.small_task_worker.runner import run_small_task_worker
from solution_agent.small_task_worker.task_contract import parse_task_md
from solution_agent.tools.patch_tools import check_changed_files_allowed
from tests.helpers import WorkspaceTempDir


STATUSES = ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed")


def _cleanup_task(task_id: str) -> None:
    for status in STATUSES:
        path = task_dir(status, task_id)
        if path.exists():
            shutil.rmtree(path)
    for path in (dispatch_dir(task_id), worker_out_dir(task_id), worktree_dir(task_id), result_latest_dir(task_id).parent):
        if path.exists():
            shutil.rmtree(path)


def _write_task(
    task_id: str,
    *,
    status: str = "pending",
    worker: str = "materialized_execution",
    line: str = "A_residual_correction",
    batch: str = "A_residual_correction_inbox_batch",
    dag_root: str = "a_rescorr_001_prepare_contract_inventory",
    extra_front_matter: str = "",
) -> None:
    folder = task_dir(status, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "task.md").write_text(
        f"""---
task_id: {task_id}
title: {task_id}
priority: normal
worker: {worker}
status: {status}
line: {line}
batch: {batch}
dag_root: {dag_root}
resource_hint: cpu
created_at: 2026-05-13T00:00:00+08:00
{extra_front_matter}---

# Task

Reliability replay fixture.
""",
        encoding="utf-8",
    )


def _write_index(folder: Path) -> None:
    (folder / "TASK_BATCH_INDEX.md").write_text("# A-line replay\n", encoding="utf-8")


def test_replay_a_line_minimal_task_metabolizes_required_decision_contract(monkeypatch):
    with WorkspaceTempDir() as tmp:
        workspace = tmp / "workspace"
        inbox = workspace / "tasks" / "inbox"
        batch = inbox / "A_residual_correction_inbox_batch"
        batch.mkdir(parents=True)
        _write_index(batch)
        (batch / "a_rescorr_014_line_a_decision_synthesis.md").write_text(
            """---
task_id: a_rescorr_014_line_a_decision_synthesis
title: Line A decision synthesis
worker: materialized_execution
intent: decision_synthesis
outputs:
  - decision_summary.json
  - A_residual_correction_report.md
---

# Task

Write the final A-line decision.
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(core, "root", lambda: workspace)

        result = core.inbox_metabolize(batch, write=False)

        assert result["ok"] is True
        contract = result["planned"][0]["updates"]["decision_contract"]
        assert contract["primary_json"] == "decision_summary.json"
        assert "best_iou_gain" in contract["required_fields"]
        assert "best_boundary_f1_gain" in contract["required_fields"]
        assert "recommended_use" in contract["required_fields"]
        assert contract["field_types"]["best_iou_gain"] == "number"
        assert contract["field_types"]["detector_validated"] == "bool"
        assert contract["semantic_checks"] == ["a_residual_decision_consistency"]


def test_replay_collect_downgrades_a_line_success_when_decision_fields_missing():
    task_id = f"replay_a_final_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
    - A_residual_correction_report.md
  required_fields:
    - detector_validated
    - best_iou_gain
    - best_boundary_f1_gain
    - recommended_use
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed done",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps({"detector_validated": True, "recommended_use": "diagnostic_only"}),
            encoding="utf-8",
        )
        (out / "A_residual_correction_report.md").write_text("# Report\n", encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        latest = result_latest_dir(task_id)
        result_yaml = yaml.safe_load((latest / "result.yaml").read_text(encoding="utf-8"))
        assert result_yaml["status"] == "partial"
        errors = (latest / "contract_errors.json").read_text(encoding="utf-8")
        assert "best_iou_gain" in errors
        assert "best_boundary_f1_gain" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_accepts_final_contract_files_under_artifacts_dir():
    task_id = f"replay_artifacts_final_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
    - A_residual_correction_report.md
  required_fields:
    - detector_validated
    - best_iou_gain
    - recommended_use
""",
        )
        out = worker_out_dir(task_id)
        artifacts = out / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: done with artifacts dir",
                ]
            ),
            encoding="utf-8",
        )
        (artifacts / "decision_summary.json").write_text(
            json.dumps(
                {
                    "detector_validated": True,
                    "best_iou_gain": 0.012,
                    "recommended_use": "diagnostic_only",
                }
            ),
            encoding="utf-8",
        )
        (artifacts / "A_residual_correction_report.md").write_text("# Report\n", encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "completed"
        latest = result_latest_dir(task_id)
        validation = json.loads((latest / "final_contract_validation.json").read_text(encoding="utf-8"))
        assert validation["ok"] is True
        assert not (latest / "contract_errors.json").exists()
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_rejects_unsafe_final_contract_file_path():
    task_id = f"replay_unsafe_contract_path_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - ../outside.json
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed unsafe contract file",
                ]
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        validation = json.loads((result_latest_dir(task_id) / "final_contract_validation.json").read_text(encoding="utf-8"))
        assert any(finding["rule_id"] == "unsafe_required_file_path" for finding in validation["findings"])
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_success_when_primary_json_invalid():
    task_id = f"replay_bad_json_final_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - detector_validated
    - best_iou_gain
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed final decision with corrupt json",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text('{"detector_validated": true,', encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        latest = result_latest_dir(task_id)
        errors = (latest / "contract_errors.json").read_text(encoding="utf-8")
        validation = json.loads((latest / "final_contract_validation.json").read_text(encoding="utf-8"))
        assert "decision_summary.json is not valid JSON" in errors
        assert any(finding["rule_id"] == "primary_json_invalid" for finding in validation["findings"])
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_empty_decision_contract_warns_without_downgrading_success():
    task_id = f"replay_empty_contract_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed final decision with advisory-only contract",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(json.dumps({"note": "diagnostic only"}), encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "completed"
        latest = result_latest_dir(task_id)
        validation = json.loads((latest / "final_contract_validation.json").read_text(encoding="utf-8"))
        assert validation["ok"] is True
        assert any(finding["rule_id"] == "empty_contract" for finding in validation["findings"])
        assert not (latest / "contract_errors.json").exists()
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_final_contract_wrong_metric_type():
    task_id = f"replay_bad_type_final_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - detector_validated
    - best_iou_gain
    - recommended_use
  field_types:
    detector_validated: bool
    best_iou_gain: number
    recommended_use: string
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed typed metrics",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps(
                {
                    "detector_validated": True,
                    "best_iou_gain": "not_a_number",
                    "recommended_use": "diagnostic_only",
                }
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "best_iou_gain expected number" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_final_contract_nan_metric():
    task_id = f"replay_nan_final_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - best_iou_gain
  field_types:
    best_iou_gain: number
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed nan metric",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text('{"best_iou_gain": NaN}', encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "best_iou_gain" in errors
        assert "not finite" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_semantic_inconsistent_fusion_claim():
    task_id = f"replay_bad_semantic_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  semantic_checks:
    - a_residual_decision_consistency
  required_files:
    - decision_summary.json
  required_fields:
    - fusion_beats_detector
    - best_iou_gain
    - recommended_use
  field_types:
    fusion_beats_detector: bool
    best_iou_gain: number
    recommended_use: string
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed semantic consistency",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps(
                {
                    "fusion_beats_detector": True,
                    "best_iou_gain": -0.01,
                    "recommended_use": "adopt",
                }
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "fusion_beats_detector is true but best_iou_gain is not positive" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_a_line_metabolized_contract_catches_semantic_inconsistency():
    task_id = f"replay_auto_semantic_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - detector_validated
    - fusion_beats_detector
    - best_iou_gain
    - recommended_use
  field_types:
    detector_validated: bool
    fusion_beats_detector: bool
    best_iou_gain: number
    recommended_use: string
  semantic_checks:
    - a_residual_decision_consistency
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed auto semantic contract",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps(
                {
                    "detector_validated": True,
                    "fusion_beats_detector": False,
                    "best_iou_gain": 0.03,
                    "recommended_use": "diagnostic_only",
                }
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "fusion_beats_detector is false but best_iou_gain is positive" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_semantic_warning_does_not_downgrade_diagnostic_result():
    task_id = f"replay_diag_warning_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - fusion_beats_detector
    - best_iou_gain
    - best_boundary_f1_gain
    - real_side_fp_reduction
    - fake_core_recall_loss
    - recommended_use
  field_types:
    fusion_beats_detector: bool
    best_iou_gain: number
    best_boundary_f1_gain: number
    real_side_fp_reduction: number
    fake_core_recall_loss: number
    recommended_use: string
  semantic_checks:
    - a_residual_decision_consistency
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: diagnostic despite positive gains",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps(
                {
                    "fusion_beats_detector": True,
                    "best_iou_gain": 0.03,
                    "best_boundary_f1_gain": 0.02,
                    "real_side_fp_reduction": 0.0,
                    "fake_core_recall_loss": 0.0,
                    "recommended_use": "diagnostic_only",
                }
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "completed"
        validation = json.loads((result_latest_dir(task_id) / "final_contract_validation.json").read_text(encoding="utf-8"))
        assert validation["ok"] is True
        assert validation["warnings"]
        assert validation["findings"][-1]["severity"] == "warning"
        assert not (result_latest_dir(task_id) / "contract_errors.json").exists()
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_rejects_non_finite_numeric_string_even_for_string_field():
    task_id = f"replay_string_nan_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  required_files:
    - decision_summary.json
  required_fields:
    - recommended_use
  field_types:
    recommended_use: string
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed string value",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(json.dumps({"recommended_use": "NaN"}), encoding="utf-8")
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "non-finite numeric string" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_adopt_claim_with_negative_fp_reduction():
    task_id = f"replay_bad_adopt_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(
            task_id,
            status="running",
            extra_front_matter="""decision_contract:
  primary_json: decision_summary.json
  semantic_checks: a_residual_decision_consistency
  required_files:
    - decision_summary.json
  required_fields:
    - fusion_beats_detector
    - best_iou_gain
    - real_side_fp_reduction
    - recommended_use
  field_types:
    fusion_beats_detector: bool
    best_iou_gain: number
    real_side_fp_reduction: number
    recommended_use: string
""",
        )
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed adopt",
                ]
            ),
            encoding="utf-8",
        )
        (out / "decision_summary.json").write_text(
            json.dumps(
                {
                    "fusion_beats_detector": True,
                    "best_iou_gain": 0.02,
                    "real_side_fp_reduction": -0.2,
                    "recommended_use": "adopt",
                }
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        errors = (result_latest_dir(task_id) / "contract_errors.json").read_text(encoding="utf-8")
        assert "recommended_use claims adoption but real_side_fp_reduction is negative" in errors
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_scheduler_does_not_leak_across_worker_or_line():
    prefix = f"replay_sched_{uuid.uuid4().hex[:8]}_"
    small = prefix + "small"
    other_line = prefix + "other_line"
    eligible = prefix + "eligible"
    original = load_registry()
    try:
        _write_task(small, worker="small_task_worker", line="A_residual_correction")
        _write_task(other_line, worker="materialized_execution", line="B_graph_line")
        _write_task(eligible, worker="materialized_execution", line="A_residual_correction")
        save_registry(
            {
                "tasks": {
                    small: {"task_id": small, "status": "pending", "worker": "small_task_worker"},
                    other_line: {"task_id": other_line, "status": "pending", "worker": "materialized_execution"},
                    eligible: {"task_id": eligible, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        selected = core.next_pending_task_id(
            worker="materialized_execution",
            line="A_residual_correction",
            prefix=prefix,
        )

        assert selected == eligible
    finally:
        save_registry(original, path=registry_path())
        for task_id in (small, other_line, eligible):
            _cleanup_task(task_id)


def test_replay_scheduler_applies_scope_before_start_budget_explanation():
    prefix = f"replay_budget_scope_{uuid.uuid4().hex[:8]}_"
    eligible = prefix + "eligible"
    other_line = prefix + "other_line"
    original = load_registry()
    try:
        _write_task(eligible, worker="materialized_execution", line="A_residual_correction")
        _write_task(other_line, worker="materialized_execution", line="B_graph_line")
        save_registry(
            {
                "tasks": {
                    eligible: {"task_id": eligible, "status": "pending", "worker": "materialized_execution"},
                    other_line: {"task_id": other_line, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_cpu=1,
                max_running_gpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert [item["task_id"] for item in plan["plan"]] == [eligible]
        skipped = next(item for item in plan["skipped"] if item.get("task_id") == other_line)
        assert skipped["reason"] == "line_scope_mismatch"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(eligible)
        _cleanup_task(other_line)


def test_replay_scheduler_explains_total_capacity_before_lane_capacity():
    task_id = f"replay_total_capacity_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_task(task_id, worker="materialized_execution", line="A_residual_correction")
        save_registry(
            {
                "tasks": {
                    task_id: {"task_id": task_id, "status": "pending", "worker": "materialized_execution"}
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 1, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_cpu=1,
                max_running_gpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=task_id,
            )

        assert plan["plan"] == []
        skipped = next(item for item in plan["skipped"] if item.get("task_id") == task_id)
        assert skipped["reason"] == "total_capacity_full"
        assert skipped["running_total"] == 1
        assert skipped["max_running_total"] == 1
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_scheduler_skips_target_workplace_mismatch_before_launch():
    prefix = f"replay_workplace_{uuid.uuid4().hex[:8]}_"
    targeted = prefix + "targeted"
    eligible = prefix + "eligible"
    original = load_registry()
    try:
        _write_task(
            targeted,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""target_workplace:
  host: host
  remote_root: /runs-b
  remote_workdir: /work
""",
        )
        _write_task(eligible, worker="materialized_execution", line="A_residual_correction")
        save_registry(
            {
                "tasks": {
                    targeted: {"task_id": targeted, "status": "pending", "worker": "materialized_execution"},
                    eligible: {"task_id": eligible, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs-a",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_cpu=2,
                max_running_gpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert [item["task_id"] for item in plan["plan"]] == [eligible]
        skipped = next(item for item in plan["skipped"] if item.get("task_id") == targeted)
        assert skipped["reason"] == "target_workplace_mismatch"
        assert skipped["mismatches"][0]["field"] == "remote_root"
        assert skipped["active_workplace"]["remote_root"] == "/runs-a"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(targeted)
        _cleanup_task(eligible)


def test_replay_prepare_first_policy_is_scoped_before_blocking_gpu():
    prefix = f"replay_prepare_scope_{uuid.uuid4().hex[:8]}_"
    foreign_prepare = prefix + "foreign_prepare"
    gpu_task = prefix + "gpu_probe"
    original = load_registry()
    try:
        _write_task(
            foreign_prepare,
            worker="materialized_execution",
            line="B_graph_line",
            extra_front_matter="execution_stage: prepare\n",
        )
        _write_task(
            gpu_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="resource_hint: gpu\nmin_free_gpu_mem_gb: 10\n",
        )
        save_registry(
            {
                "tasks": {
                    foreign_prepare: {"task_id": foreign_prepare, "status": "pending", "worker": "materialized_execution"},
                    gpu_task: {"task_id": gpu_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch(
                "solution_agent.orchestrator.core.remote_gpu_snapshot",
                return_value={
                    "gpus": [
                        {
                            "index": "0",
                            "uuid": "GPU-test",
                            "free_gb": 20.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        }
                    ]
                },
            ),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_gpu=1,
                max_running_cpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert plan["ready_prepare_exists"] is False
        assert [item["task_id"] for item in plan["plan"]] == [gpu_task]
        assert not any(
            item.get("task_id") == gpu_task and item.get("reason") == "prepare_wave_not_complete"
            for item in plan["skipped"]
        )
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(foreign_prepare)
        _cleanup_task(gpu_task)


def test_replay_source_code_change_serializes_pending_source_edits():
    prefix = f"replay_source_lane_{uuid.uuid4().hex[:8]}_"
    running_source = prefix + "running_source"
    pending_source = prefix + "pending_source"
    original = load_registry()
    try:
        _write_task(
            running_source,
            status="running",
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""allow_code_changes: true
code_change_scope: source
""",
        )
        _write_task(
            pending_source,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""allow_code_changes: true
code_change_scope: source
""",
        )
        save_registry(
            {
                "tasks": {
                    running_source: {
                        "task_id": running_source,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": running_source,
                        "runs": [{"task_id": running_source, "run_id": running_source, "status": "running"}],
                    },
                    pending_source: {
                        "task_id": pending_source,
                        "status": "pending",
                        "worker": "materialized_execution",
                    },
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 1, "gpu": 0, "cpu": 1}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", return_value={"gpus": []}),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=2,
                max_running_cpu=2,
                max_running_gpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert plan["plan"] == []
        assert plan["running_source_change_tasks"] == [running_source]
        skipped = next(item for item in plan["skipped"] if item.get("task_id") == pending_source)
        assert skipped["reason"] == "source_code_change_serialized"
        assert skipped["running_source_change_tasks"] == [running_source]
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(running_source)
        _cleanup_task(pending_source)


def test_replay_scheduler_plans_cpu_and_gpu_lanes_in_same_wave():
    prefix = f"replay_lane_{uuid.uuid4().hex[:8]}_"
    cpu_task = prefix + "cpu_analysis"
    gpu_task = prefix + "gpu_probe"
    original = load_registry()
    try:
        _write_task(cpu_task, worker="materialized_execution", line="A_residual_correction")
        _write_task(
            gpu_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="resource_hint: gpu\nmin_free_gpu_mem_gb: 10\n",
        )
        save_registry(
            {
                "tasks": {
                    cpu_task: {"task_id": cpu_task, "status": "pending", "worker": "materialized_execution"},
                    gpu_task: {"task_id": gpu_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch(
                "solution_agent.orchestrator.core.remote_gpu_snapshot",
                return_value={
                    "gpus": [
                        {
                            "index": "0",
                            "uuid": "GPU-test",
                            "free_gb": 20.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        }
                    ]
                },
            ),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_gpu=1,
                max_running_cpu=1,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        planned = {item["task_id"]: item for item in plan["plan"]}
        assert set(planned) == {cpu_task, gpu_task}
        assert planned[cpu_task]["resource_hint"] == "cpu"
        assert planned[gpu_task]["resource_hint"] == "gpu"
        assert planned[gpu_task]["selected_gpu_id"] == "0"
    finally:
        save_registry(original, path=registry_path())
        for task_id in (cpu_task, gpu_task):
            _cleanup_task(task_id)


def test_replay_scheduler_launch_precheck_blocks_stale_gpu_memory():
    prefix = f"replay_stale_gpu_{uuid.uuid4().hex[:8]}_"
    gpu_task = prefix + "gpu_probe"
    original = load_registry()
    try:
        _write_task(
            gpu_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="resource_hint: gpu\nmin_free_gpu_mem_gb: 10\n",
        )
        save_registry(
            {
                "tasks": {
                    gpu_task: {"task_id": gpu_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        snapshots = [
            {
                "gpus": [
                    {
                        "index": "0",
                        "uuid": "GPU-test",
                        "free_gb": 20.0,
                        "own_compute_app_count": 0,
                        "external_compute_app_count": 0,
                    }
                ]
            },
            {
                "gpus": [
                    {
                        "index": "0",
                        "uuid": "GPU-test",
                        "free_gb": 2.0,
                        "own_compute_app_count": 0,
                        "external_compute_app_count": 0,
                    }
                ]
            },
        ]
        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch("solution_agent.orchestrator.core.remote_gpu_snapshot", side_effect=snapshots),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
            patch("solution_agent.orchestrator.core.run_remote") as run_remote,
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_gpu=1,
                dry_run=False,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert plan["started"] == []
        assert not run_remote.called
        assert any(item.get("reason") == "launch_precheck_free_memory_changed" for item in plan["skipped"])
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(gpu_task)


def test_replay_scheduler_rejects_allowed_gpu_id_with_insufficient_memory():
    prefix = f"replay_gpu_id_{uuid.uuid4().hex[:8]}_"
    gpu_task = prefix + "gpu_probe"
    original = load_registry()
    try:
        _write_task(
            gpu_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""resource_hint: gpu
min_free_gpu_mem_gb: 10
allowed_gpu_ids:
  - "1"
""",
        )
        save_registry(
            {
                "tasks": {
                    gpu_task: {"task_id": gpu_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch(
                "solution_agent.orchestrator.core.remote_gpu_snapshot",
                return_value={
                    "gpus": [
                        {
                            "index": "0",
                            "uuid": "GPU-fast",
                            "free_gb": 30.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        },
                        {
                            "index": "1",
                            "uuid": "GPU-allowed-but-full",
                            "free_gb": 2.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        },
                    ]
                },
            ),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_gpu=1,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert plan["plan"] == []
        skipped = next(item for item in plan["skipped"] if item.get("task_id") == gpu_task)
        assert skipped["reason"] == "no_gpu_with_required_free_memory"
        assert skipped["allowed_gpu_ids"] == ["1"]
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(gpu_task)


def test_replay_scheduler_avoids_external_gpu_process_even_with_more_free_memory():
    prefix = f"replay_external_gpu_{uuid.uuid4().hex[:8]}_"
    gpu_task = prefix + "gpu_probe"
    original = load_registry()
    try:
        _write_task(
            gpu_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""resource_hint: gpu
min_free_gpu_mem_gb: 10
allowed_gpu_ids:
  - "0"
  - "1"
""",
        )
        save_registry(
            {
                "tasks": {
                    gpu_task: {"task_id": gpu_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch(
                "solution_agent.orchestrator.core.remote_gpu_snapshot",
                return_value={
                    "gpus": [
                        {
                            "index": "0",
                            "uuid": "GPU-external-high-free",
                            "free_gb": 40.0,
                            "utilization_pct": 5.0,
                            "has_external_compute_apps": True,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 1,
                        },
                        {
                            "index": "1",
                            "uuid": "GPU-clean-lower-free",
                            "free_gb": 20.0,
                            "utilization_pct": 0.0,
                            "has_external_compute_apps": False,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        },
                    ]
                },
            ),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=1,
                max_running_total=1,
                max_running_gpu=1,
                max_running_cpu=0,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        assert plan["plan"][0]["task_id"] == gpu_task
        assert plan["plan"][0]["selected_gpu_id"] == "1"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(gpu_task)


def test_replay_scheduler_respects_distinct_allowed_gpu_ids_in_same_wave():
    prefix = f"replay_gpu_ids_{uuid.uuid4().hex[:8]}_"
    gpu0_task = prefix + "gpu0_probe"
    gpu1_task = prefix + "gpu1_probe"
    original = load_registry()
    try:
        _write_task(
            gpu0_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""resource_hint: gpu
min_free_gpu_mem_gb: 10
allowed_gpu_ids:
  - "0"
""",
        )
        _write_task(
            gpu1_task,
            worker="materialized_execution",
            line="A_residual_correction",
            extra_front_matter="""resource_hint: gpu
min_free_gpu_mem_gb: 10
allowed_gpu_ids:
  - "1"
""",
        )
        save_registry(
            {
                "tasks": {
                    gpu0_task: {"task_id": gpu0_task, "status": "pending", "worker": "materialized_execution"},
                    gpu1_task: {"task_id": gpu1_task, "status": "pending", "worker": "materialized_execution"},
                }
            },
            path=registry_path(),
        )

        with (
            patch("solution_agent.orchestrator.core.dependency_reconcile", return_value={"promoted": []}),
            patch("solution_agent.orchestrator.core._running_capacity", return_value={"total": 0, "gpu": 0, "cpu": 0}),
            patch("solution_agent.orchestrator.core._running_gpu_counts", return_value={}),
            patch(
                "solution_agent.orchestrator.core.remote_gpu_snapshot",
                return_value={
                    "gpus": [
                        {
                            "index": "0",
                            "uuid": "GPU-zero",
                            "free_gb": 20.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        },
                        {
                            "index": "1",
                            "uuid": "GPU-one",
                            "free_gb": 20.0,
                            "own_compute_app_count": 0,
                            "external_compute_app_count": 0,
                        },
                    ]
                },
            ),
            patch("solution_agent.orchestrator.core._has_ready_prepare_task", return_value=False),
            patch("solution_agent.orchestrator.core._running_source_change_task_ids", return_value=[]),
        ):
            plan = core.schedule_remote(
                host="host",
                remote_root="/runs",
                remote_workdir="/work",
                worker="materialized_execution",
                max_starts=2,
                max_running_total=2,
                max_running_gpu=2,
                max_running_cpu=0,
                max_tasks_per_gpu=1,
                dry_run=True,
                line="A_residual_correction",
                prefix=prefix,
            )

        planned = {item["task_id"]: item for item in plan["plan"]}
        assert set(planned) == {gpu0_task, gpu1_task}
        assert planned[gpu0_task]["selected_gpu_id"] == "0"
        assert planned[gpu1_task]["selected_gpu_id"] == "1"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(gpu0_task)
        _cleanup_task(gpu1_task)


def test_replay_collect_missing_result_yaml_turns_failed_not_completed():
    task_id = f"replay_missing_result_{uuid.uuid4().hex[:8]}"
    original = load_registry()
    try:
        _write_task(task_id, status="running")
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "stdout.log").write_text("worker printed logs only\n", encoding="utf-8")
        (out / "stderr.log").write_text("missing result.yaml\n", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "runs": [{"task_id": task_id, "run_id": task_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "failed"
        result_yaml = yaml.safe_load((result_latest_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
        assert result_yaml["status"] == "failed"
        assert "missing _worker_out/result.yaml" in result_yaml["summary_short"]
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_downgrades_success_on_high_risk_safety_audit():
    task_id = f"replay_safety_audit_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(task_id, status="running")
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: claimed success",
                ]
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("worker executed git reset --hard during cleanup\n", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "partial"
        result_yaml = yaml.safe_load((result_latest_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
        safety_audit = json.loads((result_latest_dir(task_id) / "safety_audit.json").read_text(encoding="utf-8"))
        assert result_yaml["status"] == "partial"
        assert "Safety audit found high-risk log patterns" in result_yaml["summary_short"]
        assert safety_audit["high_count"] == 1
        assert safety_audit["findings"][0]["code"] == "git_reset"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_creates_action_request_for_gpu_runtime_oom_partial():
    task_id = f"replay_gpu_oom_{uuid.uuid4().hex[:8]}"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(task_id, status="running")
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: partial",
                    "worker: materialized_execution",
                    "failure_type: gpu_runtime_oom",
                    "summary_short: CUDA out of memory after launch precheck passed",
                ]
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("CUDA out of memory during detector sweep\n", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("torch.cuda.OutOfMemoryError: CUDA out of memory\n", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)
        listed = core.list_action_requests(status="pending")

        assert collected["status"] == "partial"
        assert collected["failure_action_request"]["request"]["action_type"] == "failure_followup"
        matching = [
            item
            for item in listed["requests"]
            if item.get("task_id") == task_id and item.get("payload", {}).get("failure_type") == "gpu_runtime_oom"
        ]
        assert len(matching) == 1
        assert matching[0]["payload"]["run_id"] == run_id
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_collect_caveats_success_when_partial_inputs_were_bound():
    task_id = f"replay_partial_input_{uuid.uuid4().hex[:8]}"
    parent_id = f"{task_id}_parent"
    run_id = f"{task_id}__run"
    original = load_registry()
    try:
        _write_task(task_id, status="running")
        out = worker_out_dir(task_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    "status: success",
                    "worker: materialized_execution",
                    "summary_short: downstream completed",
                ]
            ),
            encoding="utf-8",
        )
        (out / "worker_notes.md").write_text("notes", encoding="utf-8")
        (out / "stdout.log").write_text("", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": task_id,
                        "status": "running",
                        "worker": "materialized_execution",
                        "latest_run_id": run_id,
                        "input_bindings": {
                            parent_id: {
                                "run_id": "parent_run",
                                "source_status": "partial",
                                "binding_reason": "partial artifact accepted for diagnostic continuation",
                                "caveats": ["parent cache missing; metrics are diagnostic only"],
                            }
                        },
                        "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                    }
                }
            },
            path=registry_path(),
        )

        collected = core.collect(task_id)

        assert collected["status"] == "completed"
        result_yaml = yaml.safe_load((result_latest_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
        assert result_yaml["status"] == "success"
        assert result_yaml["used_partial_inputs"] is True
        assert result_yaml["formal_claim_allowed"] is False
        assert result_yaml["claim_level"] == "provisional"
        assert result_yaml["partial_input_sources"][0]["task_id"] == parent_id
        assert result_yaml["claim_limitations"][0]["type"] == "partial_input_claim_limitation"
    finally:
        save_registry(original, path=registry_path())
        _cleanup_task(task_id)


def test_replay_action_request_quality_gate_rejects_vague_context_request():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ):
        save_registry({"tasks": {"replay_partial": {"task_id": "replay_partial", "status": "partial"}}}, path=tmp / "registry.json")
        result_path = tmp / "results" / "replay_partial" / "latest"
        artifacts = result_path / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "action_requests.json").write_text(
            json.dumps(
                {
                    "action_requests": [
                        {
                            "kind": "request_context",
                            "missing_or_needed": "context",
                            "why_needed": "need more context",
                            "next_owner": "scheduler",
                            "blocks": "claim",
                            "blocks_claim_not_execution": True,
                            "blocks_execution": False,
                            "suggested_next_step": "Review.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = core.create_artifact_action_requests("replay_partial", run_id="run1", result_path=result_path)

        assert result["created"] == []
        assert result["skipped"][0]["reason"] == "invalid_action_request"
        assert "why_needed_too_generic" in result["skipped"][0]["errors"]


def test_replay_action_request_rejects_contradictory_block_flags():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ):
        save_registry({"tasks": {"replay_partial": {"task_id": "replay_partial", "status": "partial"}}}, path=tmp / "registry.json")
        result_path = tmp / "results" / "replay_partial" / "latest"
        artifacts = result_path / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "action_requests.json").write_text(
            json.dumps(
                {
                    "action_requests": [
                        {
                            "kind": "request_bounded_probe",
                            "missing_or_needed": "detector_calibration_metrics.json",
                            "why_needed": "Needed to decide whether detector_validated can be true.",
                            "next_owner": "scheduler",
                            "blocks": "claim",
                            "blocks_claim_not_execution": True,
                            "blocks_execution": True,
                            "suggested_next_step": "Run the bounded detector calibration task.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = core.create_artifact_action_requests("replay_partial", run_id="run1", result_path=result_path)

        assert result["created"] == []
        assert result["skipped"][0]["reason"] == "invalid_action_request"
        assert "blocks_execution_requires_blocks_execution" in result["skipped"][0]["errors"]
        assert "ambiguous_claim_and_execution_block" in result["skipped"][0]["errors"]


def test_replay_action_request_rejects_nonexistent_owner_claim():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ):
        save_registry({"tasks": {"replay_partial": {"task_id": "replay_partial", "status": "partial"}}}, path=tmp / "registry.json")
        result_path = tmp / "results" / "replay_partial" / "latest"
        artifacts = result_path / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "action_requests.json").write_text(
            json.dumps(
                {
                    "action_requests": [
                        {
                            "kind": "request_bounded_probe",
                            "missing_or_needed": "detector_calibration_metrics.json",
                            "why_needed": "Needed to decide whether detector_validated can be true.",
                            "next_owner": "imaginary_worker_pool",
                            "blocks": "claim",
                            "blocks_claim_not_execution": True,
                            "blocks_execution": False,
                            "suggested_next_step": "Run the bounded detector calibration task.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = core.create_artifact_action_requests("replay_partial", run_id="run1", result_path=result_path)
        listed = core.list_action_requests(status="pending")

        assert result["created"] == []
        assert result["skipped"][0]["reason"] == "invalid_action_request"
        assert "next_owner_invalid" in result["skipped"][0]["errors"]
        assert listed["counts"]["pending"] == 0


def test_replay_schedule_once_action_request_requires_launch_permission():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ), patch("solution_agent.orchestrator.core.schedule_remote") as schedule_remote:
        save_registry({"tasks": {}}, path=tmp / "registry.json")
        created = core.create_action_request(
            "schedule_once",
            reason="execution-blocking action request replay",
            safe_level="requires_launch_approval",
            payload={
                "host": "host",
                "remote_root": "/runs",
                "remote_workdir": "/work",
                "worker": "materialized_execution",
            },
        )

        result = core.process_action_requests(apply_selected=created["action_id"], allow_launch=False)
        listed = core.list_action_requests()

        assert result["processed"][0]["status"] == "rejected"
        assert "requires --allow-launch" in result["processed"][0]["reason"]
        assert listed["counts"]["rejected"] == 1
        schedule_remote.assert_not_called()


def test_replay_missing_artifact_action_request_is_imported_when_specific():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ):
        save_registry({"tasks": {"replay_partial": {"task_id": "replay_partial", "status": "partial"}}}, path=tmp / "registry.json")
        result_path = tmp / "results" / "replay_partial" / "latest"
        artifacts = result_path / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "action_requests.json").write_text(
            json.dumps(
                {
                    "action_requests": [
                        {
                            "kind": "request_missing_artifact",
                            "missing_or_needed": "detector_calibration_metrics.json",
                            "why_needed": "Needed to decide whether detector_validated can be true in Line A synthesis.",
                            "next_owner": "scheduler",
                            "blocks": "claim",
                            "blocks_claim_not_execution": True,
                            "blocks_execution": False,
                            "suggested_next_step": "Run the bounded detector calibration metrics task on the GenD workplace.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = core.create_artifact_action_requests("replay_partial", run_id="run1", result_path=result_path)
        listed = core.list_action_requests(status="pending")

        assert len(result["created"]) == 1
        assert result["created"][0]["missing_or_needed"] == "detector_calibration_metrics.json"
        assert listed["counts"]["pending"] == 1
        assert listed["requests"][0]["payload"]["source_run_id"] == "run1"


def test_replay_opencoder_rejects_parent_directory_allowed_modify():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: unsafe_opencoder
title: Unsafe OpenCoder task
worker: small_task_worker
status: pending
---

## Goal

Patch outside the repo.

## Context Paths

- solution_agent/tools/patch_tools.py

## Allowed Modify

- ../outside.py

## Allowed Commands

- python -m py_compile solution_agent/tools/patch_tools.py

## Expected Outputs

- result.yaml
- worker_notes.md
- patch.diff

## Done Definition

py_compile passes.
""",
            encoding="utf-8",
        )
        task = parse_task_md(task_path)

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "unsafe_allowed_modify_path" for issue in result.errors)


def test_replay_opencoder_rejects_parent_directory_context_path():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: unsafe_opencoder_context
title: Unsafe OpenCoder context task
worker: small_task_worker
status: pending
---

## Goal

Read outside the repo.

## Context Paths

- ../outside.py

## Allowed Modify

- solution_agent/tools/patch_tools.py

## Allowed Commands

- python -m py_compile solution_agent/tools/patch_tools.py

## Expected Outputs

- result.yaml
- worker_notes.md
- patch.diff

## Done Definition

py_compile passes.
""",
            encoding="utf-8",
        )
        task = parse_task_md(task_path)

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "unsafe_context_path" for issue in result.errors)


def test_replay_opencoder_admission_failure_handoff_imports_action_request():
    with WorkspaceTempDir() as tmp, patch.multiple(
        "solution_agent.orchestrator.core",
        state_dir=lambda: tmp / "state",
        registry_path=lambda: tmp / "registry.json",
        root=lambda: tmp,
    ), patch(
        "solution_agent.orchestrator.core.load_maintenance_mode",
        return_value={"enabled": False, "allow_action_requests": True, "allow_scheduler_launch": True},
    ):
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: unsafe_opencoder_handoff
title: Unsafe OpenCoder handoff
worker: small_task_worker
status: pending
---

## Goal

Patch outside the repo.

## Context Paths

- solution_agent/tools/patch_tools.py

## Allowed Modify

- .

## Allowed Commands

- python -m py_compile solution_agent/tools/patch_tools.py

## Expected Outputs

- result.yaml
- worker_notes.md
- patch.diff

## Done Definition

py_compile passes.
""",
            encoding="utf-8",
        )
        out_dir = tmp / "_worker_out"
        run_small_task_worker(task_path=task_path, worktree=tmp, out_dir=out_dir)
        save_registry({"tasks": {"unsafe_opencoder_handoff": {"task_id": "unsafe_opencoder_handoff", "status": "partial"}}}, path=tmp / "registry.json")

        result = core.create_artifact_action_requests("unsafe_opencoder_handoff", run_id="run1", result_path=out_dir)
        listed = core.list_action_requests(status="pending")

        assert len(result["created"]) == 1
        assert result["created"][0]["kind"] == "request_codex_review"
        assert result["created"][0]["missing_or_needed"] == "codex_handoff_required"
        assert listed["counts"]["pending"] == 1
        assert listed["requests"][0]["payload"]["blocks_execution"] is True
        assert listed["requests"][0]["safe_level"] == "diagnostic"


def test_replay_patch_diff_touching_undeclared_file_is_rejected():
    result = check_changed_files_allowed(
        ["docs/allowed.md", "solution_agent/orchestrator/core.py"],
        ["docs/allowed.md"],
    )

    assert result.ok is False
    assert any("solution_agent/orchestrator/core.py" in error for error in result.errors)

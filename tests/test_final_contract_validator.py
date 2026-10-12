from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

import yaml

from solution_agent.orchestrator import core
from solution_agent.orchestrator.contract_validator import validate_final_contract
from solution_agent.orchestrator.paths import (
    ensure_orchestrator_dirs,
    result_latest_dir,
    task_dir,
    worker_out_dir,
    worktree_dir,
)
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.schema import TaskBook


def _contract_task(**contract: object) -> TaskBook:
    return TaskBook(
        task_id="contract_task",
        title="Contract task",
        raw_front_matter={
            "task_id": "contract_task",
            "title": "Contract task",
            "decision_contract": contract,
        },
    )


class FinalContractValidatorTests(unittest.TestCase):
    def test_no_contract_is_not_checked(self):
        result = validate_final_contract(TaskBook(task_id="t", title="T"), Path("."))
        self.assertFalse(result.checked)
        self.assertTrue(result.ok)

    def test_missing_required_file_fails(self):
        task = _contract_task(required_files=["decision_summary.json"])
        result = validate_final_contract(task, Path("missing-root"))
        self.assertTrue(result.checked)
        self.assertFalse(result.ok)
        self.assertIn("decision_summary.json", "\n".join(result.errors))

    def test_unsafe_required_file_path_fails_before_lookup(self):
        task = _contract_task(required_files=["../outside.json"])
        result = validate_final_contract(task, Path("missing-root"))
        self.assertTrue(result.checked)
        self.assertFalse(result.ok)
        self.assertTrue(any(finding.rule_id == "unsafe_required_file_path" for finding in result.findings))

    def test_required_fields_and_nullable_policy(self):
        root = Path(self._testMethodName)
        root.mkdir(exist_ok=True)
        try:
            (root / "decision_summary.json").write_text(
                json.dumps({"best": {"iou_gain": None}, "allowed_null": None}),
                encoding="utf-8",
            )
            task = _contract_task(
                required_fields=["best.iou_gain", "allowed_null"],
                nullable_fields=["allowed_null"],
            )
            result = validate_final_contract(task, root)
            self.assertFalse(result.ok)
            self.assertIn("best.iou_gain", "\n".join(result.errors))
            self.assertNotIn("allowed_null", "\n".join(result.errors))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_semantic_warning_does_not_fail_contract(self):
        root = Path(self._testMethodName)
        root.mkdir(exist_ok=True)
        try:
            (root / "decision_summary.json").write_text(
                json.dumps(
                    {
                        "fusion_beats_detector": True,
                        "best_iou_gain": 0.01,
                        "best_boundary_f1_gain": 0.02,
                        "real_side_fp_reduction": 0.0,
                        "fake_core_recall_loss": 0.0,
                        "recommended_use": "diagnostic_only",
                    }
                ),
                encoding="utf-8",
            )
            task = _contract_task(
                primary_json="decision_summary.json",
                required_files=["decision_summary.json"],
                required_fields=[
                    "fusion_beats_detector",
                    "best_iou_gain",
                    "best_boundary_f1_gain",
                    "real_side_fp_reduction",
                    "fake_core_recall_loss",
                    "recommended_use",
                ],
                field_types={
                    "fusion_beats_detector": "bool",
                    "best_iou_gain": "number",
                    "best_boundary_f1_gain": "number",
                    "real_side_fp_reduction": "number",
                    "fake_core_recall_loss": "number",
                    "recommended_use": "string",
                },
                semantic_checks=["a_residual_decision_consistency"],
            )

            result = validate_final_contract(task, root)

            self.assertTrue(result.ok)
            self.assertEqual(result.errors, [])
            self.assertTrue(result.warnings)
            self.assertEqual(result.findings[-1].severity, "warning")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class CollectFinalContractTests(unittest.TestCase):
    def setUp(self):
        ensure_orchestrator_dirs()
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup)

    def _cleanup(self, task_id: str) -> None:
        for status in ("pending", "running", "completed", "partial", "failed"):
            path = task_dir(status, task_id)
            if path.exists():
                shutil.rmtree(path)
        for path in (worker_out_dir(task_id), worktree_dir(task_id), result_latest_dir(task_id).parent):
            if path.exists():
                shutil.rmtree(path)

    def test_collect_downgrades_success_when_decision_contract_missing_field(self):
        task_id = f"final_contract_{uuid.uuid4().hex[:8]}"
        run_id = f"{task_id}__run"
        try:
            running_dir = task_dir("running", task_id)
            running_dir.mkdir(parents=True, exist_ok=True)
            (running_dir / "task.md").write_text(
                "\n".join(
                    [
                        "---",
                        f"task_id: {task_id}",
                        "title: Final contract collect",
                        "priority: normal",
                        "worker: materialized_execution",
                        "status: running",
                        "decision_contract:",
                        "  primary_json: decision_summary.json",
                        "  required_files:",
                        "    - decision_summary.json",
                        "  required_fields:",
                        "    - detector_validated",
                        "    - best_iou_gain",
                        "---",
                        "",
                        "# Task",
                        "",
                        "Produce final decision metrics.",
                    ]
                ),
                encoding="utf-8",
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
                        "summary_short: done",
                    ]
                ),
                encoding="utf-8",
            )
            (out / "decision_summary.json").write_text(
                json.dumps({"detector_validated": True}),
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
                            "title": "Final contract collect",
                            "status": "running",
                            "latest_run_id": run_id,
                            "runs": [{"task_id": task_id, "run_id": run_id, "status": "running"}],
                        }
                    }
                }
            )

            collected = core.collect(task_id)

            self.assertEqual(collected["status"], "partial")
            latest = result_latest_dir(task_id)
            result_payload = yaml.safe_load((latest / "result.yaml").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "partial")
            self.assertTrue((latest / "final_contract_validation.json").exists())
            self.assertTrue((latest / "contract_errors.json").exists())
            self.assertIn("best_iou_gain", (latest / "contract_errors.json").read_text(encoding="utf-8"))
        finally:
            self._cleanup(task_id)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import yaml

from scripts.loupe.run_seg_overfit import _metric_from_ckpt_name, _metrics_from_train_log
from helpers import WorkspaceTempDir
from solution_agent.orchestrator.core import (
    _execution_plan_gate,
    _execution_output_names_from_task,
    _normalize_materialized_execution_spec,
    _one_shot_repair_execution_plan,
    _resolve_provider_policy,
    _scale_gate_block_reason,
    _scale_gate_claim_limitation,
    _try_synthesize_contract_writer_handoff,
    _try_synthesize_registry_handoff,
    run_materialized_execution_task,
)
from solution_agent.orchestrator.task_loader import load_task


def test_direct_execution_output_names_accept_expected_outputs_front_matter():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: expected_outputs_direct_execution
title: Expected outputs direct execution
priority: normal
worker: materialized_execution
status: pending
expected_outputs:
  - metrics.csv
  - report.md
execution:
  script_id: demo_script
---

# Goal

Exercise expected_outputs compatibility.
""",
            encoding="utf-8",
        )

        task = load_task(task_path)

    assert _execution_output_names_from_task(task) == ["metrics.csv", "report.md"]


def test_direct_execution_output_names_accept_fenced_required_outputs_section():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: fenced_outputs_direct_execution
title: Fenced outputs direct execution
priority: normal
worker: materialized_execution
status: pending
execution:
  script_id: demo_script
---

# Task

## Required Outputs

```text
metrics.csv
predictions.jsonl
report.md
action_requests.json if needed
```
""",
            encoding="utf-8",
        )

        task = load_task(task_path)

    assert _execution_output_names_from_task(task) == [
        "metrics.csv",
        "predictions.jsonl",
        "report.md",
        "action_requests.json",
    ]


def test_bypass_invalid_script_id_writes_failed_worker_contract():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: bypass_bad_script_contract
title: Bypass bad script contract
priority: normal
worker: materialized_execution
status: pending
task_type: material_regeneration
materialized_execution_bypass_materializer: true
execution:
  script_id: definitely_not_allowlisted
  args:
    output_dir: .tmp/bypass_bad_script_contract
  timeout_sec: 30
  gpu_required: false
  gpu_count: 0
  required_artifacts:
    - .tmp/bypass_bad_script_contract/metrics.json
  success_criteria:
    exit_code: 0
---

# Goal

Exercise invalid execution_spec handling.
""",
            encoding="utf-8",
        )

        out_dir = tmp / "_worker_out"
        result = run_materialized_execution_task(
            task_path=task_path,
            out_dir=out_dir,
            workspace_root=tmp,
            materializer_root=tmp / "materializer",
        )
        payload = yaml.safe_load((out_dir / "result.yaml").read_text(encoding="utf-8"))

    assert result["status"] == "failed"
    assert result["execution_status"] == "validation_failed"
    assert result["error_type"] == "SpecValidationError"
    assert payload["status"] == "failed"
    assert payload["worker"] == "materialized_execution"


def test_execution_block_runs_directly_without_materializer_bypass_flag():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: direct_bad_script_contract
title: Direct bad script contract
priority: normal
worker: materialized_execution
status: pending
task_type: material_regeneration
execution:
  script_id: definitely_not_allowlisted
  args:
    output_dir: .tmp/direct_bad_script_contract
  timeout_sec: 30
  gpu_required: false
  gpu_count: 0
  required_artifacts:
    - .tmp/direct_bad_script_contract/metrics.json
  success_criteria:
    exit_code: 0
---

# Goal

Exercise direct execution-block handling without API materializer fallback.
""",
            encoding="utf-8",
        )

        out_dir = tmp / "_worker_out"
        result = run_materialized_execution_task(
            task_path=task_path,
            out_dir=out_dir,
            workspace_root=tmp,
            materializer_root=tmp / "materializer",
        )
        materializer_result = yaml.safe_load((out_dir / "materializer" / "result_package.yaml").read_text(encoding="utf-8"))

    assert result["status"] == "failed"
    assert result["execution_status"] == "validation_failed"
    assert result["materializer_status"] == "direct_execution_block"
    assert result["retry_attempts"] == 0
    assert materializer_result["status"] == "direct_execution_block"


def test_execution_block_relative_required_artifacts_resolve_under_default_output_dir():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: direct_relative_artifact_contract
title: Direct relative artifact contract
priority: normal
worker: materialized_execution
status: pending
task_type: material_regeneration
execution:
  script_id: definitely_not_allowlisted
  args:
    output_dir: .tmp/direct_relative_artifact_contract
  timeout_sec: 30
  gpu_required: false
  gpu_count: 0
  required_artifacts:
    - metrics.json
  success_criteria:
    exit_code: 0
---

# Goal

Exercise relative required artifact normalization.
""",
            encoding="utf-8",
        )

        out_dir = tmp / "_worker_out"
        run_materialized_execution_task(
            task_path=task_path,
            out_dir=out_dir,
            workspace_root=tmp,
            materializer_root=tmp / "materializer",
        )
        spec = yaml.safe_load((out_dir / "execution" / "execution_spec.yaml").read_text(encoding="utf-8"))

    assert spec["artifacts"]["required"] == [str(tmp / "artifacts" / "metrics.json")]


def test_execution_plan_gate_rejects_unapproved_recursive_image_scan():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_bad_scan
title: Plan gate bad scan
worker: materialized_execution
status: pending
allow_heavy_data_scan: false
allow_image_header_scan: false
---

# Goal

Audit metadata without unbounded raw image scans.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_bad_scan"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_bad_scan",
  "read_paths": [
    {
      "path": "/datasets/raw",
      "kind": "image_headers",
      "recursive": true,
      "estimated_files": 200000,
      "purpose": "open every image"
    }
  ],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": false,
    "opens_image_files": true,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": true,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Scan all image headers."
}
""",
            encoding="utf-8",
        )

        task = load_task(task_path)
        report = _execution_plan_gate(task, mat_run, tmp / "_worker_out")

    assert report["ok"] is False
    assert report["failure_type"] == "gate_denied_missing_approval"
    assert report["recommended_action"]["type"] == "request_approval"
    assert any("full_recursive_dataset_scan" in error for error in report["errors"])
    assert any("image file/header" in error for error in report["errors"])


def test_execution_plan_gate_heavy_flag_does_not_approve_image_or_recursive_scan():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_heavy_not_generic
title: Plan gate heavy is not generic approval
worker: materialized_execution
status: pending
allow_heavy_data_scan: true
---

# Goal

Heavy approval alone should not approve image headers or recursive scans.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_heavy_not_generic"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_heavy_not_generic",
  "read_paths": [
    {
      "path": "/datasets/raw",
      "kind": "image_headers",
      "recursive": true,
      "estimated_files": 100,
      "purpose": "open image headers"
    }
  ],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": false,
    "opens_image_files": true,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": true,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out")

    assert report["ok"] is False
    assert report["failure_type"] == "gate_denied_missing_approval"
    assert any("allow_image_header_scan" in error for error in report["errors"])
    assert any("allow_recursive_dataset_scan" in error for error in report["errors"])


def test_execution_plan_gate_requires_bounded_raw_dataset_discovery_flag():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_bounded_discovery
title: Plan gate bounded discovery
worker: materialized_execution
status: pending
allow_recursive_dataset_scan: true
max_planned_read_files_without_approval: 500
---

# Goal

Bounded raw dataset discovery still needs its own approval.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_bounded_discovery"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_bounded_discovery",
  "read_paths": [
    {
      "path": "/datasets/raw",
      "kind": "dataset_root",
      "recursive": true,
      "estimated_files": 100,
      "purpose": "discover raw dataset samples"
    }
  ],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": false,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": true,
    "bounded_raw_dataset_discovery": true,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out")

    assert report["ok"] is False
    assert report["failure_type"] == "gate_denied_missing_approval"
    assert any("allow_bounded_raw_dataset_discovery" in error for error in report["errors"])


def test_execution_plan_gate_allows_contract_writer_for_contract_outputs():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_contract_writer
title: Plan gate contract writer
worker: materialized_execution
status: pending
required_outputs:
  - seg_overfit_evidence_contract.md
  - seg_overfit_expected_metrics_schema.json
  - seg_overfit_judge_rules.yaml
  - seg_overfit_required_outputs_checklist.md
---

# Goal

Write bounded contract artifacts.
""",
            encoding="utf-8",
        )
        root = tmp / "workspace"
        runtime_script = root / ".codex-orchestrator" / "runtime" / "scripts" / "reports" / "run_contract_artifact_writer.py"
        runtime_script.parent.mkdir(parents=True)
        runtime_script.write_text("print('ok')\n", encoding="utf-8")
        mat_run = tmp / "materializer" / "plan_gate_contract_writer"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_contract_writer",
  "read_paths": [
    {"path": "context_pack/task_brief.md", "kind": "config", "recursive": false, "estimated_files": 1, "purpose": "task contract"}
  ],
  "write_paths": [
    {"path": "seg_overfit_evidence_contract.md", "purpose": "contract artifact"}
  ],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            f"""schema_version: 1
task_id: plan_gate_contract_writer
job_type: materialized_execution
workspace:
  root: {root.as_posix()}
  cwd: {root.as_posix()}
  artifact_roots:
    - {(tmp / '_worker_out').as_posix()}
command:
  script_id: contract_artifact_writer
  args:
    config: contract_artifact_request.yaml
    output_dir: {(tmp / '_worker_out').as_posix()}
  timeout_sec: 300
resources:
  gpu_required: false
  gpu_count: 0
artifacts:
  required:
    - {(tmp / '_worker_out' / 'seg_overfit_evidence_contract.md').as_posix()}
success_criteria:
  exit_code: 0
  metrics_file: ''
  metric_keys: []
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is True
    assert report["failure_types"] == []


def test_contract_writer_handoff_synthesizes_spec_from_valid_plan():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: synth_contract_writer
title: Synthesize contract writer handoff
worker: materialized_execution
status: pending
required_outputs:
  - evidence_contract.md
  - expected_metrics_schema.json
  - judge_rules.yaml
---

# Task

Write a bounded evidence contract.

## Success Criteria

- The schema separates execution_status and science_status.
- The judge reads train IoU/F1 rows.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "attempt"
        context_pack = mat_run / "context_pack"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        context_pack.mkdir(parents=True)
        (context_pack / "execution_hints.yaml").write_text(
            f"""output_policy:
  default_output_dir: {(tmp / 'remote_task' / 'artifacts').as_posix()}
  allowed_artifact_roots:
    - {(tmp / 'workspace').as_posix()}
    - {(tmp / 'remote_task').as_posix()}
""",
            encoding="utf-8",
        )
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "synth_contract_writer",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Use contract_artifact_writer for bounded contract artifacts."
}
""",
            encoding="utf-8",
        )

        result = _try_synthesize_contract_writer_handoff(
            load_task(task_path),
            mat_run,
            tmp / "remote_task" / "_worker_out",
            tmp / "workspace",
            {"status": "escalation_required", "generated_files": ["generated/execution_plan.json"]},
        )
        spec = yaml.safe_load((mat_run / "execution_spec.yaml").read_text(encoding="utf-8"))
        request = yaml.safe_load((generated / "contract_artifact_request.yaml").read_text(encoding="utf-8"))

    assert result["status"] == "materialized"
    assert result["contract_writer_handoff_synthesized"] is True
    assert spec["command"]["script_id"] == "contract_artifact_writer"
    assert spec["resources"]["gpu_required"] is False
    assert "evidence_contract.md" in spec["artifacts"]["required"][0]
    assert request["outputs"][0]["path"] == "evidence_contract.md"


def test_contract_writer_handoff_accepts_report_matrix_and_recommendation_outputs():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: synth_contract_report
title: Synthesize contract report handoff
worker: materialized_execution
status: pending
execution_stage: analysis
required_outputs:
  - seg_overfit_contract_decision_report.md
  - seg_overfit_contract_decision.json
  - seg_overfit_contract_artifact_matrix.csv
  - next_action_recommendation.md
---

# Task

Produce a final decision report.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "attempt"
        context_pack = mat_run / "context_pack"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        context_pack.mkdir(parents=True)
        (context_pack / "execution_hints.yaml").write_text(
            f"""output_policy:
  default_output_dir: {(tmp / 'remote_task' / 'artifacts').as_posix()}
  allowed_artifact_roots:
    - {(tmp / 'workspace').as_posix()}
    - {(tmp / 'remote_task').as_posix()}
""",
            encoding="utf-8",
        )
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "synth_contract_report",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Use the contract_artifact_writer script to synthesize a decision report."
}
""",
            encoding="utf-8",
        )

        result = _try_synthesize_contract_writer_handoff(
            load_task(task_path),
            mat_run,
            tmp / "remote_task" / "_worker_out",
            tmp / "workspace",
            {"status": "escalation_required", "generated_files": ["generated/execution_plan.json"]},
        )
        spec = yaml.safe_load((mat_run / "execution_spec.yaml").read_text(encoding="utf-8"))
        request = yaml.safe_load((generated / "contract_artifact_request.yaml").read_text(encoding="utf-8"))

    assert result["status"] == "materialized"
    assert spec["command"]["script_id"] == "contract_artifact_writer"
    assert "seg_overfit_contract_artifact_matrix.csv" in "\n".join(spec["artifacts"]["required"])
    assert [item["path"] for item in request["outputs"]] == [
        "seg_overfit_contract_decision_report.md",
        "seg_overfit_contract_decision.json",
        "seg_overfit_contract_artifact_matrix.csv",
        "next_action_recommendation.md",
    ]
    assert request["outputs"][2]["kind"] == "csv"


def test_execution_plan_gate_rejects_output_dir_outside_remote_task_dir():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_bad_output_dir
title: Plan gate bad output dir
worker: materialized_execution
status: pending
---

# Goal

Output dir must stay inside the task run directory.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_bad_output_dir"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_bad_output_dir",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            f"""schema_version: 1
task_id: plan_gate_bad_output_dir
workspace:
  root: {tmp.as_posix()}
  cwd: {tmp.as_posix()}
command:
  script_id: solution_agent_smoke_write_metrics
  args:
    output_dir: {str((tmp / 'outside').resolve())}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "run_001" / "_worker_out", spec_path)

    assert report["ok"] is False
    assert report["failure_type"] == "path_policy_violation"
    assert report["recommended_action"]["type"] == "repair_plan"
    assert any("output_dir must be under remote_task_dir" in error for error in report["errors"])


def test_one_shot_repair_repairs_invalid_json_plan_only_once():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_repair_json
title: Plan repair json
worker: materialized_execution
status: pending
---

# Goal

Repair a narrow malformed execution plan.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_repair_json"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{"schema_version": 1, "read_paths": [], "write_paths": [], "data_access": {"uses_existing_indices": true""",
            encoding="utf-8",
        )
        task = load_task(task_path)
        out_dir = tmp / "_worker_out"
        first_gate = _execution_plan_gate(task, mat_run, out_dir)

        repair = _one_shot_repair_execution_plan(task, mat_run, out_dir, first_gate)
        second_gate = _execution_plan_gate(task, mat_run, out_dir)

    assert first_gate["failure_type"] == "plan_parse_error"
    assert repair["attempted"] is True
    assert repair["repair_model"] == "qwen3-coder-next"
    assert repair["repair_scope"] == "execution_plan"
    assert repair["codex_fallback"] is False
    assert repair["approval_changes"] is False
    assert repair["real_world_state_changes"] is False
    assert second_gate["ok"] is True


def test_one_shot_repair_adds_small_missing_plan_schema_fields():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_repair_schema
title: Plan repair schema
worker: materialized_execution
status: pending
---

# Goal

Repair missing schema fields without changing approvals.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_repair_schema"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{"schema_version": 1, "execution_strategy": "bounded"}""",
            encoding="utf-8",
        )
        task = load_task(task_path)
        out_dir = tmp / "_worker_out"
        first_gate = _execution_plan_gate(task, mat_run, out_dir)

        repair = _one_shot_repair_execution_plan(task, mat_run, out_dir, first_gate)
        payload = yaml.safe_load((generated / "execution_plan.json").read_text(encoding="utf-8"))
        second_gate = _execution_plan_gate(task, mat_run, out_dir)

    assert first_gate["failure_type"] == "plan_schema_error"
    assert repair["attempted"] is True
    assert payload["read_paths"] == []
    assert payload["write_paths"] == []
    assert payload["data_access"]["copies_or_moves_data"] is False
    assert second_gate["ok"] is True


def test_one_shot_repair_does_not_repair_missing_approval():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_repair_missing_approval
title: Plan repair missing approval
worker: materialized_execution
status: pending
---

# Goal

Missing approvals are user decisions, not repair scope.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_repair_missing_approval"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "read_paths": [{"path": "/data", "kind": "image_headers", "recursive": false, "estimated_files": 1}],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": false,
    "opens_image_files": true,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        task = load_task(task_path)
        gate = _execution_plan_gate(task, mat_run, tmp / "_worker_out")
        repair = _one_shot_repair_execution_plan(task, mat_run, tmp / "_worker_out", gate)

    assert gate["failure_type"] == "gate_denied_missing_approval"
    assert repair["attempted"] is False
    assert "non_repairable" in repair["reason"]


def test_execution_plan_gate_reports_heavy_read_failure_type():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_heavy_read
title: Plan gate heavy read
worker: materialized_execution
status: pending
allow_recursive_dataset_scan: true
allow_bounded_raw_dataset_discovery: true
max_planned_read_files_without_approval: 10
---

# Goal

Oversized bounded reads should route as heavy read approval.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_heavy_read"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_heavy_read",
  "read_paths": [
    {
      "path": "/datasets/raw",
      "kind": "dataset_root",
      "recursive": true,
      "estimated_files": 1000,
      "purpose": "discover raw dataset samples"
    }
  ],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": false,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": true,
    "bounded_raw_dataset_discovery": true,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out")

    assert report["ok"] is False
    assert report["failure_type"] == "gate_denied_heavy_read"
    assert report["recommended_action"]["type"] == "request_approval"


def test_execution_plan_gate_accepts_bounded_manifest_plan():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_good_manifest
title: Plan gate good manifest
worker: materialized_execution
status: pending
---

# Goal

Use existing manifests.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_good_manifest"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_good_manifest",
  "read_paths": [
    {
      "path": "assets/datasets/smoke/train.parquet",
      "kind": "parquet",
      "recursive": false,
      "estimated_files": 1,
      "purpose": "read an existing manifest"
    }
  ],
  "write_paths": [
    {"path": "artifacts/metadata_audit_summary.md", "purpose": "summary"}
  ],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Read existing parquet metadata only."
}
""",
            encoding="utf-8",
        )

        task = load_task(task_path)
        report = _execution_plan_gate(task, mat_run, tmp / "_worker_out")

    assert report["ok"] is True
    assert report["errors"] == []


def test_execution_plan_gate_rejects_risky_script_without_task_flags():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_risky_script
title: Plan gate risky script
worker: materialized_execution
status: pending
---

# Goal

Use a script with known heavy read behavior.
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_risky_script"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_risky_script",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "bounded"
}
""",
            encoding="utf-8",
        )
        spec_path = mat_run / "execution_spec.yaml"
        spec_path.write_text(
            """task_id: plan_gate_risky_script
workspace:
  root: /remote/worktree
command:
  script_id: loupe_metadata_audit
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is False
    assert any("allow_image_header_scan" in error for error in report["errors"])


def test_execution_plan_gate_rejects_script_that_cannot_satisfy_output_family():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_wrong_script_family
title: Plan gate wrong script family
worker: materialized_execution
status: pending
---

# Task

Train a metadata-only classifier.

## Required Outputs

- metadata_only_metrics.json
- metadata_only_predictions.csv
- metadata_only_summary.md
- worker_notes.md
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_wrong_script_family"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_wrong_script_family",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Use a metadata audit script."
}
""",
            encoding="utf-8",
        )
        spec_path = mat_run / "execution_spec.yaml"
        spec_path.write_text(
            """task_id: plan_gate_wrong_script_family
workspace:
  root: /remote/worktree
command:
  script_id: loupe_manifest_metadata_audit
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is False
    assert any("cannot satisfy required output family" in error for error in report["errors"])


def test_execution_plan_gate_accepts_metadata_only_script_family():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_metadata_only_script_family
title: Plan gate metadata-only script family
worker: materialized_execution
status: pending
---

# Task

Train a metadata-only classifier.

## Required Outputs

- metadata_only_metrics.json
- metadata_only_predictions.csv
- metadata_only_summary.md
- worker_notes.md
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_metadata_only_script_family"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_metadata_only_script_family",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: plan_gate_metadata_only_script_family
command:
  script_id: loupe_metadata_only_classifier
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is True, report["errors"]
    assert report["errors"] == []


def test_execution_plan_gate_accepts_contract_writer_for_family_named_report_tables():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_seg_report_table
title: Localization mechanism report
worker: materialized_execution
status: pending
---

# Task

Write a report that reviews seg overfit artifacts.

## Required Outputs

- loupe_localization_mechanism.md
- localization_evidence_decision_table.csv
- seg_overfit_artifact_checklist.csv
- action_requests.json
- artifact_summary.md
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_seg_report_table"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_seg_report_table",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: plan_gate_seg_report_table
command:
  script_id: contract_artifact_writer
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is True
    assert report["errors"] == []


def test_execution_plan_gate_still_rejects_contract_writer_for_classifier_metrics():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_metadata_classifier_writer
title: Metadata-only classifier
worker: materialized_execution
status: pending
---

# Task

Run a metadata-only classifier.

## Required Outputs

- metadata_only_classifier_metrics.json
- metadata_only_feature_importance.csv
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_metadata_classifier_writer"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_metadata_classifier_writer",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: plan_gate_metadata_classifier_writer
command:
  script_id: contract_artifact_writer
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is False
    assert any("metadata_only_" in error for error in report["errors"])


def test_execution_plan_gate_reads_output_family_allowlist_from_registry(monkeypatch):
    import solution_agent.orchestrator.core as core

    monkeypatch.setattr(
        core,
        "_default_execution_registry",
        lambda: {
            "scripts": {
                "custom_probe_script": {
                    "produces_artifact_families": [{"prefix": "custom_probe_"}],
                }
            }
        },
    )
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_custom_registry_family
title: Custom registry family
worker: materialized_execution
status: pending
---

# Task

Run a custom probe.

## Required Outputs

- custom_probe_metrics.json
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_custom_registry_family"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_custom_registry_family",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: plan_gate_custom_registry_family
command:
  script_id: custom_probe_script
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is True
    assert report["errors"] == []


def test_execution_plan_gate_reads_contract_artifact_family_from_registry(monkeypatch):
    import solution_agent.orchestrator.core as core

    monkeypatch.setattr(
        core,
        "_default_execution_registry",
        lambda: {
            "artifact_families": {
                "contract_artifact": {
                    "suffixes": ["_memo.csv"],
                    "exact_names": ["action_requests.json"],
                    "markers": [],
                }
            },
            "scripts": {
                "contract_artifact_writer": {
                    "produces_artifact_families": [{"family": "contract_artifact", "mode": "writer"}],
                },
                "custom_probe_script": {
                    "produces_artifact_families": [{"prefix": "custom_probe_"}],
                },
            },
        },
    )
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: plan_gate_custom_contract_family
title: Custom contract family
worker: materialized_execution
status: pending
---

# Task

Write a custom probe memo.

## Required Outputs

- custom_probe_memo.csv
""",
            encoding="utf-8",
        )
        mat_run = tmp / "materializer" / "plan_gate_custom_contract_family"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "plan_gate_custom_contract_family",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  }
}
""",
            encoding="utf-8",
        )
        spec_path = generated / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: plan_gate_custom_contract_family
command:
  script_id: contract_artifact_writer
  args: {}
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, tmp / "_worker_out", spec_path)

    assert report["ok"] is True
    assert report["errors"] == []


def test_registry_handoff_synthesizes_loupe_seg_overfit_without_legacy_branch(monkeypatch):
    import solution_agent.orchestrator.core as core

    registry = {
        "scripts": {
            "loupe_seg_overfit": {
                "max_timeout_sec": 28800,
                "gpu_allowed": True,
                "handoff_synthesis": {
                    "when_strategy_mentions": "loupe_seg_overfit",
                    "output_prefixes": ["seg_overfit_", "seg_micro_overfit_"],
                    "required_task_flags": ["allow_image_header_scan", "allow_image_pixel_access"],
                    "project_asset_bindings": {
                        "data_dir": {
                            "path": ["workplace_init", "selected", "project_assets", "datasets", "ddl_test_root"],
                        }
                    },
                    "workspace_arg": "workdir",
                    "default_args": {"batch_size": 4, "seed": 42},
                    "text_int_args": {
                        "sample_count": {
                            "default": 100,
                            "pattern": r"\b(\d+)\s*[- ]?fake",
                            "mirror_to": ["min_fake_samples"],
                        },
                        "max_epochs": {
                            "default": 10,
                            "pattern": r"\b(\d+)\s*epochs?\b",
                        },
                    },
                    "artifact_prefix": {
                        "default": "seg_overfit",
                        "output_prefix_map": {
                            "seg_micro_overfit_": "seg_micro_overfit",
                            "seg_overfit_": "seg_overfit",
                        },
                    },
                    "timeout": {
                        "default_sec": 14400,
                        "min_sec": 3600,
                        "max_sec": 28800,
                        "task_runtime_budget_hours_field": "runtime_budget_hours",
                    },
                    "resources": {"gpu_required": True, "gpu_count": 1},
                    "success_criteria": {
                        "metrics_file_template": "{output_dir}/{artifact_prefix}_metrics.json",
                        "metric_keys": ["train_iou", "train_loss_final", "execution_status"],
                    },
                },
            }
        }
    }
    monkeypatch.setattr(core, "_default_execution_registry", lambda: registry)
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: registry_seg_handoff
title: Registry seg handoff
worker: materialized_execution
status: pending
allow_image_header_scan: true
allow_image_pixel_access: true
runtime_budget_hours: 2
---

# Task

Run a bounded 100-fake seg overfit probe for 3 epochs.

## Required Outputs

- seg_overfit_metrics.json
- seg_overfit_loss_curve.csv
- seg_overfit_train_eval.csv
""",
            encoding="utf-8",
        )
        task = load_task(task_path)
        workspace = tmp / "Loupe_agent"
        workspace.mkdir()
        mat_run = tmp / "materializer" / "registry_seg_handoff"
        generated = mat_run / "generated"
        context = mat_run / "context_pack"
        generated.mkdir(parents=True)
        context.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "registry_seg_handoff",
  "read_paths": [],
  "write_paths": [],
  "data_access": {},
  "execution_strategy": "Use loupe_seg_overfit for the requested bounded probe."
}
""",
            encoding="utf-8",
        )
        (context / "workspace_context_excerpt.yaml").write_text(
            """workplace_init:
  selected:
    project_assets:
      datasets:
        ddl_test_root: /remote/datasets/ddl_test
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"

        result = _try_synthesize_registry_handoff(task, mat_run, out_dir, workspace, {"status": "escalation_required"})

        spec = yaml.safe_load((mat_run / "execution_spec.yaml").read_text(encoding="utf-8"))
        assert result["registry_handoff_synthesized"] is True
        assert spec["command"]["script_id"] == "loupe_seg_overfit"
        assert spec["command"]["args"]["data_dir"] == "/remote/datasets/ddl_test"
        assert spec["command"]["args"]["sample_count"] == 100
        assert spec["command"]["args"]["min_fake_samples"] == 100
        assert spec["command"]["args"]["max_epochs"] == 3
        assert spec["command"]["timeout_sec"] == 7200
        assert spec["resources"] == {"gpu_required": True, "gpu_count": 1}
        assert spec["success_criteria"]["metrics_file"].endswith("/artifacts/seg_overfit_metrics.json")
        assert spec["success_criteria"]["metric_keys"] == ["train_iou", "train_loss_final", "execution_status"]


def test_registry_handoff_uses_bundled_loupe_seg_overfit_contract():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: bundled_registry_seg_handoff
title: Bundled registry seg handoff
worker: materialized_execution
status: pending
allow_image_header_scan: true
allow_image_pixel_access: true
runtime_budget_hours: 2
---

# Task

Run a bounded 100-fake seg overfit probe for 3 epochs.

## Required Outputs

- seg_overfit_metrics.json
- seg_overfit_loss_curve.csv
- seg_overfit_train_eval.csv
""",
            encoding="utf-8",
        )
        task = load_task(task_path)
        workspace = tmp / "Loupe_agent"
        workspace.mkdir()
        mat_run = tmp / "materializer" / "bundled_registry_seg_handoff"
        generated = mat_run / "generated"
        context = mat_run / "context_pack"
        generated.mkdir(parents=True)
        context.mkdir(parents=True)
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "bundled_registry_seg_handoff",
  "read_paths": [],
  "write_paths": [],
  "data_access": {},
  "execution_strategy": "Use loupe_seg_overfit for the requested bounded probe."
}
""",
            encoding="utf-8",
        )
        (context / "workspace_context_excerpt.yaml").write_text(
            """workplace_init:
  selected:
    project_assets:
      datasets:
        ddl_test_root: /remote/datasets/ddl_test
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"

        result = _try_synthesize_registry_handoff(task, mat_run, out_dir, workspace, {"status": "escalation_required"})

        spec = yaml.safe_load((mat_run / "execution_spec.yaml").read_text(encoding="utf-8"))
        assert result["registry_handoff_synthesized"] is True
        assert spec["solution_agent_normalizations"][0]["type"] == "registry_handoff_from_plan"
        assert spec["command"]["script_id"] == "loupe_seg_overfit"
        assert spec["command"]["args"]["data_dir"] == "/remote/datasets/ddl_test"
        assert spec["command"]["args"]["sample_count"] == 100
        assert spec["command"]["args"]["min_fake_samples"] == 100
        assert spec["command"]["args"]["max_epochs"] == 3
        assert spec["command"]["args"]["artifact_prefix"] == "seg_overfit"
        assert spec["command"]["timeout_sec"] == 7200
        assert spec["resources"] == {"gpu_required": True, "gpu_count": 1}
        assert spec["success_criteria"]["metrics_file"].endswith("/artifacts/seg_overfit_metrics.json")
        assert spec["success_criteria"]["metric_keys"] == [
            "train_iou",
            "train_loss_final",
            "execution_status",
            "contract_status",
            "science_status",
        ]


def test_normalize_materialized_execution_spec_forces_workspace_and_run_artifacts():
    with WorkspaceTempDir() as tmp:
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: normalize_spec_paths
title: Normalize spec paths
worker: materialized_execution
status: pending
---

## Required Outputs

- worker_notes.md
- metrics.json
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        bad_output = str(tmp / "run_001" / "materializer" / "attempts" / "attempt_001" / "artifacts")
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            f"""schema_version: 1
task_id: normalize_spec_paths
workspace:
  root: {tmp.as_posix()}
  cwd: {tmp.as_posix()}
command:
  script_id: test_write_metrics
  args:
    output_dir: {bad_output}
  timeout_sec: 60
artifacts:
  required:
    - {bad_output}/worker_notes.md
success_criteria:
  exit_code: 0
""",
            encoding="utf-8",
        )

        _normalize_materialized_execution_spec(spec_path, tmp / "worktree", out_dir, load_task(task_path))
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert payload["workspace"]["root"].endswith("worktree")
    assert payload["workspace"]["cwd"].endswith("worktree")
    output_dir = payload["command"]["args"]["output_dir"].replace("\\", "/")
    required = [item.replace("\\", "/") for item in payload["artifacts"]["required"]]
    assert output_dir.endswith("run_001/artifacts")
    assert required[0].endswith("run_001/artifacts/worker_notes.md")
    assert required[1].endswith("run_001/artifacts/metrics.json")


def test_normalize_loupe_seg_overfit_uses_raw_dataset_and_allowed_args():
    with WorkspaceTempDir() as tmp:
        worktree = tmp / "Loupe_agent"
        context_dir = worktree / ".codex-orchestrator"
        context_dir.mkdir(parents=True)
        context_dir.joinpath("agent_context.yaml").write_text(
            """project_assets:
  datasets:
    ddl_test_root: /srv/example-data/datasets/DDL_dataset/test
    loupe_smoke_parquet: /srv/example-workspaces-b/loupe_workspace/assets/datasets/ddl_l_test_loupe_smoke_parquet
""",
            encoding="utf-8",
        )
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: normalize_loupe_seg
title: Normalize Loupe seg overfit
worker: materialized_execution
status: pending
---

## Required Outputs

- `seg_overfit_sample_manifest.csv`
- `seg_overfit_visual_sheet.jpg` or `.png`
- `worker_notes.md`
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: normalize_loupe_seg
workspace: {}
command:
  script_id: loupe_seg_overfit
  args:
    output_dir: /tmp/materializer/attempts/attempt_001/artifacts
    data_dir: /srv/example-workspaces-b/loupe_workspace/assets/datasets/ddl_l_test_loupe_smoke_parquet
    ckpt: /tmp/not-allowed.ckpt
    sample_count: 100
    min_fake_samples: 50
    max_epochs: 100
  timeout_sec: 28800
artifacts:
  required: []
success_criteria:
  exit_code: 0
""",
            encoding="utf-8",
        )

        _normalize_materialized_execution_spec(spec_path, worktree, out_dir, load_task(task_path))
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    args = payload["command"]["args"]
    required = [item.replace("\\", "/") for item in payload["artifacts"]["required"]]
    assert args["data_dir"] == "/srv/example-data/datasets/DDL_dataset/test"
    assert "ckpt" not in args
    assert args["max_epochs"] == 10
    assert payload["success_criteria"]["metrics_file"].endswith("seg_overfit_metrics.json")
    assert payload["success_criteria"]["metric_keys"] == ["train_iou", "train_loss_final"]
    assert any(item.endswith("seg_overfit_visual_sheet.jpg") for item in required)
    assert not any(" or " in item for item in required)


def test_normalize_format_eval_asset_replacements_are_registry_driven():
    with WorkspaceTempDir() as tmp:
        worktree = tmp / "Loupe_agent"
        context_dir = worktree / ".codex-orchestrator"
        context_dir.mkdir(parents=True)
        context_dir.joinpath("agent_context.yaml").write_text(
            """project_assets:
  datasets:
    loupe_smoke_parquet: /remote/assets/loupe_smoke_parquet
  known_checkpoints:
    cls_debug_best: /remote/checkpoints/cls_debug_best.ckpt
""",
            encoding="utf-8",
        )
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: normalize_format_eval_assets
title: Normalize format eval assets
worker: materialized_execution
status: pending
---

## Required Outputs

- format_normalized_metrics.json
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: normalize_format_eval_assets
workspace: {}
command:
  script_id: loupe_format_normalized_eval
  args:
    output_dir: /tmp/materializer/attempts/attempt_001/artifacts
    ckpt: /remote/missing/checkpoint.ckpt
    data_dir: /remote/assets/loupe_smoke_parquet_missing
    split: /remote/assets/loupe_debug_eval_split
  timeout_sec: 21600
artifacts:
  required: []
success_criteria:
  exit_code: 0
""",
            encoding="utf-8",
        )

        _normalize_materialized_execution_spec(spec_path, worktree, out_dir, load_task(task_path))

        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        args = payload["command"]["args"]
        assert args["ckpt"] == "/remote/checkpoints/cls_debug_best.ckpt"
        assert args["data_dir"] == "/remote/assets/loupe_smoke_parquet"
        assert args["split"] == "test"
        required = [item.replace("\\", "/") for item in payload["artifacts"]["required"]]
        assert any(item.endswith("/worker_notes.md") for item in required)
        assert any(item.endswith("/artifact_summary.md") for item in required)
        assert any(item.endswith("/action_requests.json") for item in required)
        normalizations = payload["solution_agent_normalizations"]
        assert all(item["reason"] for item in normalizations)

    core_source = Path("solution_agent/orchestrator/core.py").read_text(encoding="utf-8")
    assert 'script_id == "loupe_format_normalized_eval"' not in core_source


def test_normalize_loupe_seg_overfit_metrics_are_registry_driven():
    with WorkspaceTempDir() as tmp:
        worktree = tmp / "Loupe_agent"
        worktree.mkdir()
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: normalize_loupe_registry_metrics
title: Normalize Loupe registry metrics
worker: materialized_execution
status: pending
allow_long_training_epochs: true
---

# Task

Run the registry-driven overfit contract.
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: normalize_loupe_registry_metrics
workspace: {}
command:
  script_id: loupe_seg_overfit
  args:
    output_dir: /tmp/materializer/attempts/attempt_001/artifacts
    data_dir: /tmp/data
  timeout_sec: 28800
artifacts:
  required: []
success_criteria:
  exit_code: 0
""",
            encoding="utf-8",
        )

        _normalize_materialized_execution_spec(spec_path, worktree, out_dir, load_task(task_path))
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    required = [item.replace("\\", "/") for item in payload["artifacts"]["required"]]
    assert payload["success_criteria"]["metrics_file"].endswith("seg_overfit_metrics.json")
    assert payload["success_criteria"]["metric_keys"] == ["train_iou", "train_loss_final"]
    assert any(item.endswith("seg_overfit_metrics.json") for item in required)
    assert any(item.endswith("seg_overfit_summary.md") for item in required)


def test_normalizer_has_no_loupe_seg_overfit_metric_special_case():
    import inspect
    import solution_agent.orchestrator.core as core

    source = inspect.getsource(core._normalize_materialized_execution_spec)
    assert 'script_id") == "loupe_seg_overfit"' not in source
    assert "seg_overfit_metrics.json" not in source


def test_resolve_provider_policy_accepts_portable_policy_name():
    with WorkspaceTempDir() as tmp:
        pkg = tmp / "solution_agent"
        config_dir = pkg / "remote_materializer" / "config"
        config_dir.mkdir(parents=True)
        policy = config_dir / "provider_policy.cost_optimized.yaml"
        policy.write_text("primary_reader: {}\n", encoding="utf-8")
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: policy_name_task
title: Policy name task
worker: materialized_execution
status: pending
remote_materializer_policy: cost_optimized
---

# Task
Test portable provider policy names.
""",
            encoding="utf-8",
        )

        resolved = _resolve_provider_policy(load_task(task_path), pkg)

    assert resolved == policy


def test_loupe_seg_overfit_parses_checkpoint_metric_names():
    with WorkspaceTempDir() as tmp:
        ckpt = tmp / "seg-f1=0.2435-iou=0.0693.ckpt"
        ckpt.write_text("", encoding="utf-8")

        assert _metric_from_ckpt_name(ckpt, "iou") == 0.0693
        assert _metric_from_ckpt_name(ckpt, "f1") == 0.2435


def test_loupe_seg_overfit_parses_lightning_log_metrics():
    with WorkspaceTempDir() as tmp:
        log_path = tmp / "seg_overfit_train.log"
        log_path.write_text(
            """Epoch 9/9  total_loss: 9.228
seg_loss: 9.228
val_loss: 8.139
val_seg_loss: 8.139 iou: 0.009
f1: 0.027
exit_code=0
""",
            encoding="utf-8",
        )

        metrics = _metrics_from_train_log(log_path)

    assert metrics["total_loss"] == 9.228
    assert metrics["seg_loss"] == 9.228
    assert metrics["iou"] == 0.009
    assert metrics["f1"] == 0.027


def test_normalize_caps_registry_max_epochs_without_long_training_flag():
    with WorkspaceTempDir() as tmp:
        worktree = tmp / "worktree"
        worktree.mkdir()
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: normalize_generic_epochs
title: Normalize generic epochs
worker: materialized_execution
status: pending
---

## Required Outputs

- `worker_notes.md`
""",
            encoding="utf-8",
        )
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: normalize_generic_epochs
workspace: {}
command:
  script_id: loupe_seg_overfit
  args:
    output_dir: /tmp/materializer/attempts/attempt_001/artifacts
    max_epochs: 100
artifacts:
  required: []
success_criteria:
  exit_code: 0
""",
            encoding="utf-8",
        )

        _normalize_materialized_execution_spec(spec_path, worktree, out_dir, load_task(task_path))
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert payload["command"]["args"]["max_epochs"] == 10


def test_scale_gate_does_not_block_analysis_decision_task():
    with WorkspaceTempDir() as tmp:
        mat_run = tmp / "materializer"
        mat_run.mkdir()
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: analyze_scale_decision
title: Analyze scale decision
worker: materialized_execution
status: pending
execution_stage: analysis
---

# Task

Analyze whether `can_scale_to_100fake` is justified.

## Required Outputs

- `seg_micro_overfit_decision.json`
""",
            encoding="utf-8",
        )

        reason = _scale_gate_block_reason(load_task(task_path), mat_run)

    assert reason is None


def test_scale_gate_downgrades_claim_instead_of_blocking_execution():
    with WorkspaceTempDir() as tmp:
        mat_run = tmp / "materializer"
        context = mat_run / "context_pack"
        context.mkdir(parents=True)
        decision_path = tmp / "seg_micro_overfit_decision.json"
        decision_path.write_text(
            json.dumps(
                {
                    "can_scale_to_100fake": False,
                    "blocking_reason": "missing_evidence",
                    "required_next_action": "fix_logging",
                }
            ),
            encoding="utf-8",
        )
        (context / "artifact_summary.md").write_text(f"- {decision_path.as_posix()}\n", encoding="utf-8")
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: exploratory_scale_claim_limit
title: Run queued 100-fake evidence task
worker: materialized_execution
status: pending
execution_stage: run
---

# Task

Run the queued 100-fake evidence task and inspect whether `can_scale_to_100fake` is justified.
""",
            encoding="utf-8",
        )

        task = load_task(task_path)
        notice = _scale_gate_claim_limitation(task, mat_run)
        block = _scale_gate_block_reason(task, mat_run)

    assert block is None
    assert notice is not None
    assert notice["execution_allowed"] is True
    assert notice["formal_claim_allowed"] is False
    assert notice["claim_level"] == "exploratory"
    assert notice["blocking_reason"] == "missing_evidence"


def test_contract_writer_gate_ignores_downgraded_model_image_reads():
    with WorkspaceTempDir() as tmp:
        mat_run = tmp / "materializer"
        generated = mat_run / "generated"
        generated.mkdir(parents=True)
        out_dir = tmp / "run_001" / "_worker_out"
        out_dir.mkdir(parents=True)
        task_path = tmp / "task.md"
        task_path.write_text(
            """---
task_id: analysis_writer_gate
title: Analysis writer gate
worker: materialized_execution
status: pending
execution_stage: analysis
---

# Task

Analyze generated visual evidence from summaries only.
""",
            encoding="utf-8",
        )
        (generated / "execution_plan.json").write_text(
            """{
  "schema_version": 1,
  "task_id": "analysis_writer_gate",
  "read_paths": [{"path": "contact_sheet.jpg", "kind": "image_pixels"}],
  "write_paths": [{"path": "report.md"}],
  "data_access": {"opens_image_files": true, "opens_image_pixels": true}
}
""",
            encoding="utf-8",
        )
        spec_path = mat_run / "execution_spec.yaml"
        spec_path.write_text(
            """schema_version: 1
task_id: analysis_writer_gate
command:
  script_id: contract_artifact_writer
  args:
    config: request.yaml
    output_dir: artifacts
artifacts:
  required: []
""",
            encoding="utf-8",
        )

        report = _execution_plan_gate(load_task(task_path), mat_run, out_dir, spec_path)

    assert report["ok"] is True

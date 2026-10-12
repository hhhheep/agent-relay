from __future__ import annotations

from pathlib import Path

import yaml

from solution_agent.remote_materializer.controller import (
    _ensure_materialized_execution_helpers,
    _normalize_ambiguous_work_order,
    _normalize_ready_work_order,
    _writable_materializer_outputs,
)
from solution_agent.orchestrator.core import _normalize_materialized_execution_spec
from solution_agent.remote_materializer.task_loader import load_task
from solution_agent.remote_materializer.validators import summarize, validate_generated_bundle


def _task(tmp_path: Path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: normalize_mat
title: Normalize materializer outputs
worker: materialized_execution
status: pending
task_type: material_regeneration
execution:
  script_id: run_probe_smoke
  args:
    output_dir: outputs/normalize_artifacts
  timeout_sec: 60
  required_artifacts:
    - outputs/normalize_artifacts/metrics.json
    - outputs/normalize_artifacts/summary.md
---

# Required Materializer Outputs

- smoke_config.yaml
- commands.md
- output_contract.yaml
""",
        encoding="utf-8",
    )
    return task_path, load_task(task_path)


def test_ready_work_order_prefers_explicit_materializer_outputs_over_execution_artifacts(tmp_path):
    task_path, task = _task(tmp_path)
    reader_payload = {
        "status": "ready",
        "task_id": "normalize_mat",
        "task_type": "material_regeneration",
        "source_task_path": "",
        "jobs": ["run_probe_smoke"],
        "required_outputs": [
            "outputs/normalize_artifacts/metrics.json",
            "outputs/normalize_artifacts/summary.md",
            "smoke_config.yaml",
            "commands.md",
            "output_contract.yaml",
        ],
        "input_files": [],
        "allowed_output_root": "runs/normalize_mat/",
        "forbidden_actions": [],
        "validation_steps": ["validate generated files"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["missing"]},
    }

    normalized = _normalize_ready_work_order(task, task_path, reader_payload)

    assert normalized["required_outputs"] == ["smoke_config.yaml", "commands.md", "output_contract.yaml"]
    assert normalized["reader_meta"]["materializer_required_outputs_source"] == "task_explicit_materializer_outputs"


def test_ambiguous_report_task_type_normalizes_to_probe_inference(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: report_alias
title: Existing artifact synthesis
worker: materialized_execution
status: pending
task_type: report
execution_stage: analysis
---

# Task

Summarize existing evidence.

## Required Outputs

- feature_leakage_audit_report.md
- action_requests.json
""",
        encoding="utf-8",
    )
    task = load_task(task_path)

    normalized = _normalize_ambiguous_work_order(
        task,
        task_path,
        {
            "status": "ambiguous",
            "reason": "task_type report is not one of the materializer schema task types",
        },
    )

    assert normalized is not None
    assert normalized["task_type"] == "probe_inference"
    assert normalized["reader_meta"]["front_matter_task_type"] == "report"
    assert normalized["reader_meta"]["normalized_task_type"] == "probe_inference"
    assert normalized["required_outputs"] == ["execution_plan.json", "execution_spec.yaml"]


def test_ready_report_task_type_alias_is_normalized_before_validation(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: ready_report_alias
title: Existing artifact synthesis
worker: materialized_execution
status: pending
task_type: report
execution_stage: analysis
---

# Task

Summarize existing evidence.

## Required Outputs

- feature_leakage_audit_report.md
- action_requests.json
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    reader_payload = {
        "status": "ready",
        "task_id": "ready_report_alias",
        "task_type": "report",
        "source_task_path": "",
        "jobs": ["synthesize_existing_artifacts"],
        "required_outputs": ["feature_leakage_audit_report.md", "action_requests.json"],
        "input_files": [],
        "allowed_output_root": "runs/ready_report_alias/",
        "forbidden_actions": [],
        "validation_steps": ["validate generated files"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["missing"]},
    }

    normalized = _normalize_ready_work_order(task, task_path, reader_payload)

    assert normalized["task_type"] == "probe_inference"
    assert normalized["reader_meta"]["front_matter_task_type"] == "report"
    assert normalized["reader_meta"]["task_type_alias_policy"].startswith("human-facing")


def test_writable_materializer_outputs_include_contract_artifact_request_as_ancillary():
    outputs = _writable_materializer_outputs({"required_outputs": ["decision.json"]})

    assert outputs == ["decision.json", "contract_artifact_request.yaml"]


def test_ready_work_order_marks_explicit_closure_outputs_as_direct_materializer_artifacts(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: closure_replay
title: Replay closure task
worker: materialized_execution
status: pending
materializer_required_outputs:
  - decision.json
  - action_requests.json
  - artifact_summary.md
  - worker_notes.md
  - execution_plan.json
---

# Task

Classify a historical resource wait and write the closure artifacts.
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    reader_payload = {
        "status": "ready",
        "task_id": "closure_replay",
        "task_type": "probe_inference",
        "source_task_path": "",
        "jobs": ["classify_wait"],
        "required_outputs": ["expected_tables.md"],
        "input_files": [],
        "allowed_output_root": "runs/closure_replay/",
        "forbidden_actions": [],
        "validation_steps": ["validate generated files"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["missing"]},
        "reader_meta": {
            "execution_capability_needed": "specialized classifier script",
        },
    }

    normalized = _normalize_ready_work_order(task, task_path, reader_payload)

    assert normalized["required_outputs"] == [
        "decision.json",
        "action_requests.json",
        "artifact_summary.md",
        "worker_notes.md",
        "execution_plan.json",
    ]
    assert normalized["reader_meta"]["materializer_output_mode"] == "direct_closure_artifacts"
    assert normalized["reader_meta"]["execution_capability_needed"].startswith("none:")
    assert normalized["reader_meta"]["task_input_sufficiency"] == "task_book_is_sufficient_for_requested_closure_artifacts"
    assert "materialization_failed.json" in normalized["reader_meta"]["refusal_policy"]
    assert "task.md" in normalized["reader_meta"]["task_text_evidence_policy"]


def test_ready_work_order_keeps_execution_outputs_out_of_materializer_contract(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: loupe_audit
title: Loupe audit
worker: materialized_execution
status: pending
---

# Task

Audit metadata.

## Required Outputs

- metadata_all.csv
- metadata_audit_summary.md
- worker_notes.md

Minimum fields for metadata_all.csv:

- image_id
- path
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    reader_payload = {
        "status": "ready",
        "task_id": "loupe_audit",
        "task_type": "material_regeneration",
        "source_task_path": "",
        "jobs": ["audit_metadata"],
        "required_outputs": ["metadata_all.csv", "metadata_audit_summary.md", "worker_notes.md"],
        "input_files": [],
        "allowed_output_root": "runs/loupe_audit/",
        "forbidden_actions": [],
        "validation_steps": ["validate generated files"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["missing"]},
    }

    normalized = _normalize_ready_work_order(task, task_path, reader_payload)

    assert normalized["required_outputs"] == ["execution_plan.json", "execution_spec.yaml"]
    assert normalized["reader_meta"]["materializer_required_outputs_source"] == "materialized_execution_default_plan_and_execution_spec"
    assert normalized["reader_meta"]["execution_required_outputs"] == [
        "metadata_all.csv",
        "metadata_audit_summary.md",
        "worker_notes.md",
    ]


def test_materialized_execution_helpers_fill_human_commands_and_contract(tmp_path):
    _, task = _task(tmp_path)
    generated = tmp_path / "generated"
    generated.mkdir()
    work_order = {"required_outputs": ["commands.md", "output_contract.yaml"]}

    _ensure_materialized_execution_helpers(generated, task, work_order)

    assert "human-readable only" in (generated / "commands.md").read_text(encoding="utf-8")
    contract = yaml.safe_load((generated / "output_contract.yaml").read_text(encoding="utf-8"))
    assert contract["required_files"] == [
        "outputs/normalize_artifacts/metrics.json",
        "outputs/normalize_artifacts/summary.md",
    ]


def test_materialized_execution_helpers_expand_contract_artifact_request(tmp_path):
    _, task = _task(tmp_path)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "contract_artifact_request.yaml").write_text(
        """task_id: closure
outputs:
  - path: decision.json
    kind: json
    content:
      status: partial
      recommended_next_action: continuation_probe
  - path: worker_notes.md
    kind: markdown
    title: Worker Notes
    sections:
      - heading: Summary
        bullets:
          - Preserve completed shards.
          - Do not claim final ranking.
""",
        encoding="utf-8",
    )
    work_order = {"required_outputs": ["decision.json", "worker_notes.md"]}

    _ensure_materialized_execution_helpers(generated, task, work_order)

    decision = yaml.safe_load((generated / "decision.json").read_text(encoding="utf-8"))
    assert decision["recommended_next_action"] == "continuation_probe"
    notes = (generated / "worker_notes.md").read_text(encoding="utf-8")
    assert "Preserve completed shards" in notes


def test_default_contract_writer_handoff_uses_work_order_required_outputs(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: partial_eval
title: Partial eval continuation
worker: materialized_execution
status: pending
materializer_required_outputs:
  - worker_notes.md
  - artifact_summary.md
  - action_requests.json
  - decision.json
  - execution_plan.json
  - execution_spec.yaml
---

# Required Replay Output

Produce decision/action request closure artifacts.
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "context_pack").mkdir(exist_ok=True)
    work_order = {
        "reader_meta": {
            "materializer_required_outputs_source": "task_explicit_materializer_outputs",
        },
        "required_outputs": [
            "worker_notes.md",
            "artifact_summary.md",
            "action_requests.json",
            "decision.json",
            "execution_plan.json",
            "execution_spec.yaml",
        ],
    }

    _ensure_materialized_execution_helpers(generated, task, work_order)

    request = yaml.safe_load((generated / "contract_artifact_request.yaml").read_text(encoding="utf-8"))
    output_names = [item["path"] for item in request["outputs"]]
    assert output_names == [
        "worker_notes.md",
        "artifact_summary.md",
        "action_requests.json",
        "decision.json",
        "execution_plan.json",
    ]
    assert (generated / "worker_notes.md").exists()
    assert (generated / "decision.json").exists()


def test_default_contract_writer_handoff_replaces_invalid_contract_request(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: invalid_request
title: Invalid request recovery
worker: materialized_execution
status: pending
materializer_required_outputs:
  - decision.json
  - execution_spec.yaml
---
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "contract_artifact_request.yaml").write_text(
        "outputs:\n  - path: decision.json\n    kind: markdown\n    title: bad: yaml: value\n",
        encoding="utf-8",
    )
    work_order = {
        "reader_meta": {
            "materializer_required_outputs_source": "task_explicit_materializer_outputs",
        },
        "required_outputs": ["decision.json", "execution_spec.yaml"],
    }

    _ensure_materialized_execution_helpers(generated, task, work_order)

    request = yaml.safe_load((generated / "contract_artifact_request.yaml").read_text(encoding="utf-8"))
    assert request["outputs"][0]["path"] == "decision.json"


def test_materialized_execution_spec_allows_absolute_remote_paths_for_execution_port(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "execution_plan.json").write_text(
        """{
  "schema_version": 1,
  "task_id": "remote_abs",
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
    (generated / "execution_spec.yaml").write_text(
        """schema_version: 1
task_id: remote_abs
workspace:
  root: /remote/worktree
  cwd: /remote/worktree
command:
  script_id: test_write_metrics
  args:
    output_dir: /remote/runs/task/artifacts
  timeout_sec: 60
resources:
  gpu_required: false
  gpu_count: 0
artifacts:
  required: []
success_criteria:
  exit_code: 0
""",
        encoding="utf-8",
    )

    checks = validate_generated_bundle(
        generated,
        {
            "task_type": "material_regeneration",
            "required_outputs": ["execution_plan.json", "execution_spec.yaml"],
            "allowed_output_root": "runs/remote_abs/",
        },
    )

    assert summarize(checks)["status"] == "pass"


def test_materialized_execution_helpers_synthesize_contract_writer_spec_without_execution_block(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_b01
title: Review existing Loupe evidence
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/Loupe_agent
  remote_root: /remote/runs
---

Review available evidence and write action requests.

## Required Outputs

- evidence_review.md
- action_requests.json
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "truth_b01" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  contract_artifact_writer:
    max_timeout_sec: 1800
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/truth_b01__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/Loupe_agent
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text('{"execution_strategy": "bounded review"}', encoding="utf-8")

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "contract_artifact_writer"
    assert spec["command"]["args"]["output_dir"] == "/remote/runs/truth_b01__run/artifacts"
    assert spec["artifacts"]["required"] == [
        "/remote/runs/truth_b01__run/artifacts/evidence_review.md",
        "/remote/runs/truth_b01__run/artifacts/action_requests.json",
    ]
    request = yaml.safe_load((generated / "contract_artifact_request.yaml").read_text(encoding="utf-8"))
    assert [item["path"] for item in request["outputs"]] == ["evidence_review.md", "action_requests.json"]


def test_materialized_execution_helpers_synthesize_loupe_manifest_audit_spec_without_execution_block(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_a01_metadata_audit
title: Loupe metadata shortcut risk audit
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/Loupe_agent
  remote_root: /remote/runs
---

Run a manifest metadata audit for shortcut risk.

## Required Outputs

- metadata_all.csv
- metadata_audit_summary.md
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "truth_a01_metadata_audit" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  loupe_manifest_metadata_audit:
    max_timeout_sec: 3600
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/truth_a01__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/Loupe_agent
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text('{"execution_strategy": "manifest metadata audit"}', encoding="utf-8")

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "loupe_manifest_metadata_audit"
    assert spec["command"]["args"]["output_dir"] == "/remote/runs/truth_a01__run/artifacts"
    assert "/remote/runs/truth_a01__run/artifacts/metadata_all.csv" in spec["artifacts"]["required"]
    decision = yaml.safe_load((generated / "normalizer_decision.json").read_text(encoding="utf-8"))
    assert decision["status"] == "execution_spec_synthesized"


def test_materialized_execution_helpers_prefer_contract_writer_for_model_inventory(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_c01_model_evidence_inventory
title: Inventory Loupe/EFFORT/third-place evidence
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/Loupe_agent
  remote_root: /remote/runs
---

Inventory model evidence and mention metadata audit artifacts only as possible upstream context.

## Required Outputs

- model_evidence_inventory.json
- model_evidence_inventory_table.csv
- missing_model_component_table.csv
- action_requests.json
- artifact_summary.md
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "truth_c01_model_evidence_inventory" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  loupe_manifest_metadata_audit:
    max_timeout_sec: 3600
  contract_artifact_writer:
    max_timeout_sec: 1800
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/truth_c01__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/Loupe_agent
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text('{"execution_strategy": "bounded model evidence inventory"}', encoding="utf-8")

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "contract_artifact_writer"
    assert spec["artifacts"]["required"] == [
        "/remote/runs/truth_c01__run/artifacts/model_evidence_inventory.json",
        "/remote/runs/truth_c01__run/artifacts/model_evidence_inventory_table.csv",
        "/remote/runs/truth_c01__run/artifacts/missing_model_component_table.csv",
        "/remote/runs/truth_c01__run/artifacts/action_requests.json",
        "/remote/runs/truth_c01__run/artifacts/artifact_summary.md",
    ]


def test_materialized_execution_helpers_prefer_metadata_classifier_over_report_writer(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_a02_metadata_only_classifier
title: Loupe metadata-only classifier evidence probe
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/Loupe_agent
  remote_root: /remote/runs
---

Run a metadata-only classifier and write evidence summaries.

## Required Outputs

- metadata_only_classifier_metrics.json
- metadata_only_feature_importance.csv
- metadata_only_split_summary.csv
- metadata_only_shortcut_summary.md
- artifact_summary.md
- action_requests.json
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "truth_a02_metadata_only_classifier" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  loupe_metadata_only_classifier:
    max_timeout_sec: 3600
  contract_artifact_writer:
    max_timeout_sec: 1800
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/truth_a02__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/Loupe_agent
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text(
        """{
  "execution_strategy": "Use loupe_metadata_only_classifier for evidence collection.",
  "read_paths": ["/remote/runs/truth_a01/artifacts/metadata_all.csv"]
}
""",
        encoding="utf-8",
    )

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "loupe_metadata_only_classifier"
    assert spec["command"]["args"]["metadata_csv"] == "/remote/runs/truth_a01/artifacts/metadata_all.csv"
    assert "/remote/runs/truth_a02__run/artifacts/metadata_only_classifier_metrics.json" in spec["artifacts"]["required"]


def test_materialized_execution_helpers_route_from_registry_declarations(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: registry_route_custom_probe
title: Registry route custom probe
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/worktree
  remote_root: /remote/runs
---

Run a custom probe from registry declarations.

## Required Outputs

- custom_probe_metrics.json
- custom_probe_summary.md
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "registry_route_custom_probe" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  custom_probe_script:
    max_timeout_sec: 120
    route_when:
      output_prefixes:
        - custom_probe_
    default_execution:
      args:
        mode: bounded
      required_files:
        - custom_probe_metrics.json
        - custom_probe_summary.md
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/custom__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/worktree
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text('{"execution_strategy": "bounded custom probe"}', encoding="utf-8")

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "custom_probe_script"
    assert spec["command"]["args"] == {"output_dir": "/remote/runs/custom__run/artifacts", "mode": "bounded"}
    assert spec["command"]["timeout_sec"] == 120
    assert spec["artifacts"]["required"] == [
        "/remote/runs/custom__run/artifacts/custom_probe_metrics.json",
        "/remote/runs/custom__run/artifacts/custom_probe_summary.md",
    ]


def test_materialized_execution_helpers_metadata_classifier_uses_registry_route_and_bindings(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: registry_route_metadata_classifier
title: Registry route classifier
worker: materialized_execution
status: pending
target_workplace:
  remote_workdir: /remote/Loupe_agent
  remote_root: /remote/runs
---

Run a classifier; evidence/report wording should not force report writer.

## Required Outputs

- metadata_only_classifier_metrics.json
- metadata_only_feature_importance.csv
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    generated = tmp_path / "mat" / "registry_route_metadata_classifier" / "generated"
    context = generated.parent / "context_pack"
    context.mkdir(parents=True)
    generated.mkdir()
    (context / "script_registry_excerpt.yaml").write_text(
        """scripts:
  loupe_metadata_only_classifier:
    max_timeout_sec: 3600
    route_when:
      output_prefixes:
        - metadata_only_
    default_execution:
      input_bindings:
        metadata_csv:
          read_path_suffixes:
            - metadata_all.csv
            - .csv
  contract_artifact_writer:
    max_timeout_sec: 1800
    route_when:
      text_terms:
        - evidence
        - report
""",
        encoding="utf-8",
    )
    (context / "execution_hints.yaml").write_text(
        """output_policy:
  default_output_dir: /remote/runs/metadata__run/materializer/attempts/attempt_002/artifacts
  allowed_artifact_roots:
    - /remote/Loupe_agent
    - /remote/runs
""",
        encoding="utf-8",
    )
    (generated / "execution_plan.json").write_text(
        '{"execution_strategy": "bounded evidence probe", "read_paths": ["/remote/a01/artifacts/metadata_all.csv"]}',
        encoding="utf-8",
    )

    _ensure_materialized_execution_helpers(
        generated,
        task,
        {"required_outputs": ["execution_plan.json", "execution_spec.yaml"]},
    )

    spec = yaml.safe_load((generated / "execution_spec.yaml").read_text(encoding="utf-8"))
    assert spec["command"]["script_id"] == "loupe_metadata_only_classifier"
    assert spec["command"]["args"]["metadata_csv"] == "/remote/a01/artifacts/metadata_all.csv"


def test_orchestrator_keeps_normalizer_synthesized_artifacts_required(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_a01_metadata_audit
title: Loupe metadata shortcut risk audit
worker: materialized_execution
status: pending
---

Run a manifest metadata audit.

## Required Outputs

- metadata_audit_table.csv
- metadata_distribution_by_label.csv
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    workspace = tmp_path / "remote" / "Loupe_agent"
    workspace.mkdir(parents=True)
    out_dir = tmp_path / "remote" / "runs" / "truth_a01__run" / "_worker_out"
    spec_path = out_dir / "execution" / "execution_spec.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "task_id": "truth_a01_metadata_audit",
                "job_type": "materialized_execution",
                "workspace": {"root": str(workspace), "cwd": str(workspace), "artifact_roots": [str(workspace)]},
                "command": {
                    "script_id": "loupe_manifest_metadata_audit",
                    "args": {"output_dir": str(out_dir.parent / "artifacts"), "max_manifest_depth": 4},
                    "timeout_sec": 3600,
                },
                "resources": {"gpu_required": False, "gpu_count": 0},
                "artifacts": {"required": [str(out_dir.parent / "artifacts" / "metadata_all.csv")]},
                "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
                "solution_agent_normalizations": [{"type": "default_execution_spec_from_task_intent"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _normalize_materialized_execution_spec(spec_path, workspace, out_dir, task)

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    required = [item.replace("\\", "/") for item in spec["artifacts"]["required"]]
    artifacts_dir = str(out_dir.parent / "artifacts").replace("\\", "/")
    assert f"{artifacts_dir}/metadata_all.csv" in required
    assert f"{artifacts_dir}/worker_notes.md" in required
    assert f"{artifacts_dir}/artifact_summary.md" in required
    assert f"{artifacts_dir}/action_requests.json" in required


def test_orchestrator_repoints_contract_writer_config_to_generated_request(tmp_path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """---
task_id: truth_b01
title: Review evidence
worker: materialized_execution
status: pending
---

Review evidence.

## Required Outputs

- report.md
""",
        encoding="utf-8",
    )
    task = load_task(task_path)
    workspace = tmp_path / "remote" / "Loupe_agent"
    workspace.mkdir(parents=True)
    out_dir = tmp_path / "remote" / "runs" / "truth_b01__run" / "_worker_out"
    generated_request = out_dir / "materializer" / "generated" / "contract_artifact_request.yaml"
    generated_request.parent.mkdir(parents=True)
    generated_request.write_text("task_id: truth_b01\noutputs: []\n", encoding="utf-8")
    spec_path = out_dir / "execution" / "execution_spec.yaml"
    spec_path.parent.mkdir(parents=True)
    old_prefix = out_dir.parent / "materializer" / "attempts" / "attempt_001" / "artifacts"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "task_id": "truth_b01",
                "job_type": "materialized_execution",
                "workspace": {"root": str(workspace), "cwd": str(workspace), "artifact_roots": [str(workspace)]},
                "command": {
                    "script_id": "contract_artifact_writer",
                    "args": {
                        "config": str(old_prefix / "contract_artifact_request.yaml"),
                        "output_dir": str(old_prefix),
                    },
                    "timeout_sec": 1800,
                },
                "resources": {"gpu_required": False, "gpu_count": 0},
                "artifacts": {"required": [str(old_prefix / "report.md")]},
                "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
                "solution_agent_normalizations": [{"type": "default_execution_spec_from_task_intent"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _normalize_materialized_execution_spec(spec_path, workspace, out_dir, task)

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert spec["command"]["args"]["output_dir"] == str(out_dir.parent / "artifacts")
    assert spec["command"]["args"]["config"] == str(generated_request)

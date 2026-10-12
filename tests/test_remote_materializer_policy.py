from __future__ import annotations

from pathlib import Path

import yaml

from solution_agent.remote_materializer.contract_artifacts import (
    contract_artifact_request_from_task,
    contract_artifact_request_is_valid,
    expand_contract_artifact_request,
    repair_csv_artifact_tables,
)
from solution_agent.remote_materializer.materializer_policy import direct_closure_policy, load_materializer_policy
from solution_agent.remote_materializer.validators import validate_generated_bundle, validate_patch_syntax
from solution_agent.remote_materializer.validators import validate_action_requests_file, validate_patch_intent_file


class _Task:
    task_id = "policy_task"


def test_materializer_policy_loads_direct_closure_contract():
    policy = load_materializer_policy()

    assert policy.direct_closure.mode_name == "direct_closure_artifacts"
    assert policy.direct_closure.matches_output_name("decision.json")
    assert policy.direct_closure.matches_output_name("foo_summary.md")
    assert not policy.direct_closure.matches_output_name("seg_overfit_metrics.json")
    assert policy.direct_closure.allowed_execution_scripts == frozenset({"contract_artifact_writer"})
    assert not policy.direct_closure.materialization_failed_allowed


def test_direct_closure_reader_meta_policy_is_single_source():
    reader_meta = direct_closure_policy().reader_meta

    assert reader_meta["task_input_sufficiency"] == "task_book_is_sufficient_for_requested_closure_artifacts"
    assert "bounded execution" in reader_meta["claim_policy"]
    assert "materialization_failed.json" in reader_meta["refusal_policy"]


def test_contract_artifact_request_helpers_render_required_outputs(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    request = contract_artifact_request_from_task(_Task(), ["decision.json", "worker_notes.md"])
    request_path = generated / "contract_artifact_request.yaml"
    request_path.write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")

    assert contract_artifact_request_is_valid(request_path)

    expand_contract_artifact_request(generated, {"decision.json", "worker_notes.md"})

    assert (generated / "decision.json").exists()
    assert (generated / "worker_notes.md").exists()
    assert "pending_evidence_review" in (generated / "decision.json").read_text(encoding="utf-8")


def test_repair_csv_artifact_tables_converts_model_header_rows_dict(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    table = generated / "gap_table.csv"
    table.write_text(
        "{'headers': ['item', 'status'], 'rows': [{'item': 'loss_curve', 'status': 'missing_evidence'}]}",
        encoding="utf-8",
    )

    repair_csv_artifact_tables(generated, {"gap_table.csv"})

    assert table.read_text(encoding="utf-8") == "item,status\nloss_curve,missing_evidence\n"


def test_repair_csv_artifact_tables_converts_flat_row_list(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    table = generated / "evidence.csv"
    table.write_text(
        "{'headers': ['item', 'status', 'notes'], 'rows': ['ckpt', 'present', 'weak', 'iou', 'missing', 'needed']}",
        encoding="utf-8",
    )

    repair_csv_artifact_tables(generated, {"evidence.csv"})

    assert table.read_text(encoding="utf-8") == "item,status,notes\nckpt,present,weak\niou,missing,needed\n"


def test_repair_csv_artifact_tables_accepts_header_singular(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    table = generated / "available.csv"
    table.write_text(
        "{'header': ['source', 'available'], 'rows': [['Loupe', 'true'], ['EFFORT', 'false']]}",
        encoding="utf-8",
    )

    repair_csv_artifact_tables(generated, {"available.csv"})

    assert table.read_text(encoding="utf-8") == "source,available\nLoupe,true\nEFFORT,false\n"


def test_repair_csv_artifact_tables_accepts_single_dict_list_column_names(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    table = generated / "evidence.csv"
    table.write_text(
        "[{'column_names': ['fact', 'status'], 'rows': [{'fact': 'train_iou_last', 'status': 'missing'}]}]",
        encoding="utf-8",
    )

    repair_csv_artifact_tables(generated, {"evidence.csv"})

    assert table.read_text(encoding="utf-8") == "fact,status\ntrain_iou_last,missing\n"


def test_repair_csv_artifact_tables_accepts_split_column_names_and_rows(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    table = generated / "gaps.csv"
    table.write_text(
        "[{'column_names': ['gap', 'status']}, {'rows': [{'gap': 'visual_sheet', 'status': 'missing'}]}]",
        encoding="utf-8",
    )

    repair_csv_artifact_tables(generated, {"gaps.csv"})

    assert table.read_text(encoding="utf-8") == "gap,status\nvisual_sheet,missing\n"


def test_deferred_patch_statement_passes_when_closure_artifacts_exist(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "patch.diff").write_text(
        "# No patch.diff generated - exact source context is not available\n",
        encoding="utf-8",
    )
    (generated / "decision.json").write_text('{"status": "partial"}\n', encoding="utf-8")
    (generated / "action_requests.json").write_text(
        """[
  {
    "kind": "request_context",
    "missing_or_needed": "docs/PATCH_QUARANTINE_PLAN.md source content",
    "why_needed": "needed to generate a real unified diff without inventing patch hunks",
    "next_owner": "user",
    "blocks": "execution",
    "blocks_claim_not_execution": false,
    "blocks_execution": true,
    "suggested_next_step": "include the target file content in context_pack"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_patch_syntax(generated / "patch.diff")

    assert result.status == "pass"


def test_deferred_patch_statement_accepts_patch_cannot_be_generated_wording(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "patch.diff").write_text(
        "# Patch cannot be generated - target file docs/PATCH_QUARANTINE_PLAN.md not available in context_pack\n"
        "# This file is intentionally left empty as no valid unified diff can be produced\n",
        encoding="utf-8",
    )
    (generated / "decision.json").write_text('{"status": "partial"}\n', encoding="utf-8")
    (generated / "action_requests.json").write_text(
        """[
  {
    "kind": "request_context",
    "missing_or_needed": "docs/PATCH_QUARANTINE_PLAN.md source content",
    "why_needed": "needed to generate a real unified diff without inventing patch hunks",
    "next_owner": "user",
    "blocks": "execution",
    "blocks_claim_not_execution": false,
    "blocks_execution": true,
    "suggested_next_step": "include the target file content in context_pack"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_patch_syntax(generated / "patch.diff")

    assert result.status == "pass"


def test_missing_patch_diff_is_allowed_when_deferred_closure_exists(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "decision.json").write_text('{"status": "partial", "missing_context": "source file"}\n', encoding="utf-8")
    (generated / "action_requests.json").write_text(
        """[
  {
    "kind": "request_context",
    "missing_or_needed": "docs/PATCH_QUARANTINE_PLAN.md source content",
    "why_needed": "needed to generate a real unified diff without inventing patch hunks",
    "next_owner": "user",
    "blocks": "execution",
    "blocks_claim_not_execution": false,
    "blocks_execution": true,
    "suggested_next_step": "include the target file content in context_pack"
  }
]
""",
        encoding="utf-8",
    )
    (generated / "artifact_summary.md").write_text("# Summary\nPatch deferred because source context is missing.\n", encoding="utf-8")
    work_order = {
        "task_type": "material_regeneration",
        "allowed_output_root": "runs/patch/",
        "required_outputs": ["decision.json", "action_requests.json", "artifact_summary.md", "patch.diff"],
        "reader_meta": {
            "materializer_required_outputs_source": "task_explicit_materializer_outputs",
        },
    }

    results = validate_generated_bundle(generated, work_order)

    failed = [result for result in results if result.status == "fail"]
    assert failed == []


def test_action_request_quality_gate_accepts_specific_request(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text(
        """[
  {
    "kind": "request_artifact",
    "missing_or_needed": "prior metadata_only_auc.json",
    "why_needed": "needed to support comparative claim against metadata-only baseline",
    "next_owner": "scheduler",
    "blocks": "claim",
    "blocks_claim_not_execution": true,
    "blocks_execution": false,
    "evidence": ["task asks for comparative claim"],
    "suggested_next_step": "run bounded metadata-only probe or downgrade claim strength"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_action_requests_file(path)

    assert result.status == "pass"


def test_action_request_quality_gate_rejects_vague_request(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text('[{"kind": "request_context", "why_needed": "need more context"}]\n', encoding="utf-8")

    result = validate_action_requests_file(path)

    assert result.status == "fail"
    assert any("missing fields" in error for error in result.errors)
    assert any("too vague" in error for error in result.errors)


def test_action_request_quality_gate_requires_approval_flag(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text(
        """[
  {
    "kind": "request_approval",
    "missing_or_needed": "image access",
    "why_needed": "loupe_seg_overfit reads image pixels to build masks/contact sheets",
    "next_owner": "user",
    "blocks": "execution",
    "blocks_claim_not_execution": false,
    "blocks_execution": true,
    "suggested_next_step": "add allow_image_pixel_access: true if this bounded image probe is intended"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_action_requests_file(path)

    assert result.status == "fail"
    assert any("allow_" in error for error in result.errors)


def test_action_request_quality_gate_normalizes_common_model_aliases(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text(
        """[
  {
    "kind": "bounded_exploratory_probe",
    "missing_or_needed": "train_iou_last, train_eval.csv, visual_sheet",
    "why_needed": "Current evidence insufficient to assess overfitting or mask alignment",
    "next_owner": "materialized_execution",
    "blocks": "tiny_set_overfit and segmentation_learnability remain unproven",
    "blocks_claim_not_execution": true,
    "blocks_execution": false,
    "suggested_next_step": "Run bounded seg_overfit probe with required visual and train_eval outputs"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_action_requests_file(path)

    assert result.status == "pass"


def test_action_request_quality_gate_normalizes_probe_owner_aliases(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text(
        """[
  {
    "kind": "evidence_probe",
    "missing_or_needed": "train_eval_rows, visual_sheet",
    "why_needed": "Required to assess overfitting and segmentation learnability",
    "next_owner": "experiment_runner",
    "blocks": "tiny_set_overfit: not_proven",
    "blocks_claim_not_execution": true,
    "blocks_execution": false,
    "suggested_next_step": "Run bounded seg_overfit evidence probe"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_action_requests_file(path)

    assert result.status == "pass"


def test_action_request_quality_gate_normalizes_execution_port_owner(tmp_path: Path):
    path = tmp_path / "action_requests.json"
    path.write_text(
        """[
  {
    "kind": "bounded_exploratory_probe",
    "missing_or_needed": "seg_overfit_train_eval.csv",
    "why_needed": "needed to evaluate overfit claim",
    "next_owner": "execution_port",
    "blocks": "claim",
    "blocks_claim_not_execution": true,
    "blocks_execution": false,
    "suggested_next_step": "run the allowlisted seg overfit probe"
  }
]
""",
        encoding="utf-8",
    )

    result = validate_action_requests_file(path)

    assert result.status == "pass"


def test_patch_intent_file_validator_accepts_p0_doc_intent(tmp_path: Path):
    path = tmp_path / "patch_intent.json"
    path.write_text(
        """{
  "schema_version": 1,
  "intent_id": "patch_intent_001",
  "tier": "P0",
  "target_file": "docs/PATCH_QUARANTINE_PLAN.md",
  "operation": "insert_after",
  "anchor": {
    "type": "literal",
    "text": "Current materializer policy extraction:",
    "occurrence": "unique"
  },
  "new_text": "Prefer materializer_policy.yaml over prompt-only policy rules.",
  "reason": "Document the patch quarantine boundary.",
  "expected_validation": {
    "must_apply_cleanly": true,
    "must_change_only": ["docs/PATCH_QUARANTINE_PLAN.md"],
    "required_tests": []
  },
  "risk": {
    "source_code_behavior_change": false,
    "multi_file": false,
    "requires_codex": false
  }
}
""",
        encoding="utf-8",
    )

    result = validate_patch_intent_file(path)

    assert result.status == "pass"


def test_patch_intent_file_validator_rejects_codex_required_intent(tmp_path: Path):
    path = tmp_path / "patch_intent.json"
    path.write_text(
        """{
  "schema_version": 1,
  "intent_id": "patch_intent_002",
  "tier": "P2",
  "target_file": "solution_agent/remote_materializer/controller.py",
  "operation": "replace_block",
  "anchor": {"type": "literal", "text": "def run", "occurrence": "unique"},
  "replacement_text": "def run(): pass",
  "reason": "Change runtime behavior.",
  "expected_validation": {"must_apply_cleanly": true, "must_change_only": ["solution_agent/remote_materializer/controller.py"]},
  "risk": {"source_code_behavior_change": true, "multi_file": false, "requires_codex": true}
}
""",
        encoding="utf-8",
    )

    result = validate_patch_intent_file(path)

    assert result.status == "fail"
    assert any("OpenCoder may only propose" in error for error in result.errors)

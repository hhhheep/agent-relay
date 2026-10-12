from __future__ import annotations

import unittest

from helpers import WorkspaceTempDir
from solution_agent.remote_materializer.validators import (
    validate_commands_file,
    validate_decision_summary_file,
    validate_generated_bundle,
    validate_output_contract,
    validate_patch_syntax,
    validate_work_order_payload,
    validate_yaml_file,
)


class RemoteMaterializerValidatorTests(unittest.TestCase):
    def test_work_order_accepts_training_validation(self):
        result = validate_work_order_payload(
            {
                "status": "ready",
                "task_id": "tv_test",
                "task_type": "training_validation",
                "source_task_path": "tasks/tv_test/task.md",
                "jobs": [{"job_id": "generate"}],
                "required_outputs": ["train_config.yaml"],
                "input_files": ["tasks/tv_test/task.md"],
                "allowed_output_root": "runs/tv_test/",
                "forbidden_actions": [
                    "modify_source_code",
                    "execute_training",
                    "change_dataset_split",
                    "overwrite_checkpoint",
                    "delete_files",
                ],
                "validation_steps": ["validate_yaml_schema"],
                "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
                "escalation_policy": {"escalate_if": ["validator_failed_twice"]},
            }
        )

        self.assertEqual(result.status, "pass")

    def test_commands_rejects_injection(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "commands.md"
            path.write_text("```bash\nrm -rf runs/\n```", encoding="utf-8")

            result = validate_commands_file(path)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any("denied command" in item for item in result.errors))

    def test_yaml_rejects_path_escape(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "probe_infer_config.yaml"
            path.write_text("task_id: probe\noutput_dir: ../../checkpoints/baseline\n", encoding="utf-8")

            result = validate_yaml_file(path, "runs/probe/")

        self.assertEqual(result.status, "fail")

    def test_output_contract_rejects_path_escape(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "output_contract.yaml"
            path.write_text("task_id: probe\nrequired_files:\n  - ../../checkpoints/baseline\n", encoding="utf-8")

            result = validate_output_contract(path, "runs/probe/")

        self.assertEqual(result.status, "fail")

    def test_explicit_materializer_outputs_skip_task_type_template_defaults(self):
        with WorkspaceTempDir() as tmp:
            generated = tmp / "generated"
            generated.mkdir()
            (generated / "decision.json").write_text('{"status": "partial"}\n', encoding="utf-8")
            (generated / "action_requests.json").write_text('{"requests": []}\n', encoding="utf-8")
            (generated / "artifact_summary.md").write_text("# Summary\n", encoding="utf-8")
            (generated / "worker_notes.md").write_text("# Notes\n", encoding="utf-8")
            work_order = {
                "task_type": "probe_inference",
                "allowed_output_root": "runs/replay/",
                "required_outputs": [
                    "decision.json",
                    "action_requests.json",
                    "artifact_summary.md",
                    "worker_notes.md",
                ],
                "reader_meta": {
                    "materializer_required_outputs_source": "task_explicit_materializer_outputs",
                },
            }

            results = validate_generated_bundle(generated, work_order)

        failed = [result for result in results if result.status == "fail"]
        self.assertEqual(failed, [])

    def test_direct_closure_rejects_materialization_failed_as_substitute(self):
        with WorkspaceTempDir() as tmp:
            generated = tmp / "generated"
            generated.mkdir()
            (generated / "materialization_failed.json").write_text('{"failure_kind": "missing_context"}\n', encoding="utf-8")
            work_order = {
                "task_type": "probe_inference",
                "allowed_output_root": "runs/replay/",
                "required_outputs": ["decision.json", "action_requests.json"],
                "reader_meta": {
                    "materializer_required_outputs_source": "task_explicit_materializer_outputs",
                    "materializer_output_mode": "direct_closure_artifacts",
                },
            }

            results = validate_generated_bundle(generated, work_order)

        failed_names = [result.name for result in results if result.status == "fail"]
        self.assertIn("direct_closure_no_materialization_failed_substitute", failed_names)

    def test_direct_closure_rejects_experiment_script_execution_spec(self):
        with WorkspaceTempDir() as tmp:
            generated = tmp / "generated"
            generated.mkdir()
            (generated / "execution_spec.yaml").write_text(
                """task_id: replay
command:
  script_id: run_recipe_ablation
""",
                encoding="utf-8",
            )
            work_order = {
                "task_type": "material_regeneration",
                "allowed_output_root": "runs/replay/",
                "required_outputs": ["execution_spec.yaml"],
                "reader_meta": {
                    "materializer_required_outputs_source": "task_explicit_materializer_outputs",
                    "materializer_output_mode": "direct_closure_artifacts",
                },
            }

            results = validate_generated_bundle(generated, work_order)

        failed = [result for result in results if result.name == "validate_execution_spec_artifact" and result.status == "fail"]
        self.assertTrue(failed)
        self.assertIn("contract_artifact_writer", failed[0].errors[0])

    def test_patch_syntax_rejects_placeholder_hunks(self):
        with WorkspaceTempDir() as tmp:
            patch = tmp / "patch.diff"
            patch.write_text(
                """--- a/solution_agent/remote_materializer/artifact_parser.py
+++ b/solution_agent/remote_materializer/artifact_parser.py
@@ -XX,XX +XX,XX @@
-old
+new
""",
                encoding="utf-8",
            )

            result = validate_patch_syntax(patch)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any("placeholder" in item for item in result.errors))

    def test_decision_summary_rejects_formal_claim_for_proxy_model(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "decision_summary.json"
            path.write_text(
                """{
  "summary": "DINOv3 formally validated the feature leakage hypothesis.",
  "model_execution_evidence": {
    "requested_model": "GenD_DINOv3_L",
    "executed_model": "GenD_CLIP_L_14",
    "fallback_reason": "Remote transformers lacks DINOv3 classes.",
    "proxy_only": true,
    "formal_evidence": false,
    "evidence_scope": "proxy_probe",
    "allowed_claims": ["bounded proxy probe completed"],
    "forbidden_claims": ["DINOv3 formally validated the feature leakage hypothesis"]
  }
}
""",
                encoding="utf-8",
            )

            result = validate_decision_summary_file(path)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any("formal" in item for item in result.errors))

    def test_decision_summary_accepts_bounded_proxy_claim(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "decision_summary.json"
            path.write_text(
                """{
  "summary": "Bounded proxy probe completed and produced requested artifacts.",
  "claim_allowed": "bounded_proxy_probe_only",
  "model_execution_evidence": {
    "requested_model": "GenD_DINOv3_L",
    "executed_model": "GenD_CLIP_L_14",
    "fallback_reason": "Remote transformers lacks DINOv3 classes.",
    "proxy_only": true,
    "formal_evidence": false,
    "evidence_scope": "proxy_probe",
    "allowed_claims": ["bounded proxy probe completed"],
    "forbidden_claims": ["DINOv3 formally validated the feature leakage hypothesis"]
  }
}
""",
                encoding="utf-8",
            )

            result = validate_decision_summary_file(path)

        self.assertEqual(result.status, "pass")


if __name__ == "__main__":
    unittest.main()

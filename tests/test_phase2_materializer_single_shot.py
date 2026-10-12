"""Phase 2 materializer single-shot invariant tests.

P0: When feedback_text is non-empty, materializer must call the LLM provider
     exactly once. RetryController owns the outer loop; materializer must not
     have its own semantic retry when operating under orchestrator control.

P1: Materializer must return a result dict with required keys even on failure.
     It must not raise an unhandled exception that leaves the task stuck.

Architecture boundary:
    RetryController (outer loop)
        → run_materializer(feedback_text=X) [single shot]
            → materializer_client.materialize() [exactly once]
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.remote_materializer.controller import run_materializer
from solution_agent.remote_materializer.model_clients import StaticReaderClient


_FORBIDDEN_ACTIONS = [
    "modify_source_code", "execute_training",
    "change_dataset_split", "overwrite_checkpoint", "delete_files",
]

REQUIRED_OUTPUTS = ["train_config.yaml", "commands.md", "output_contract.yaml",
                    "data_manifest.csv", "val_config.yaml"]


def _task_md(task_id: str) -> str:
    return f"""---
task_id: {task_id}
title: Single-shot invariant test
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-01T00:00:00+08:00
task_type: training_validation
---

# Goal

Materializer single-shot boundary test.
"""


def _work_order(task_id: str, max_retries: int = 2) -> str:
    return json.dumps({
        "status": "ready",
        "task_id": task_id,
        "task_type": "training_validation",
        "source_task_path": f"tasks/{task_id}/task.md",
        "jobs": [{"job_id": "generate", "worker": "qwen_coder_30b", "job_type": "generate_materials"}],
        "required_outputs": REQUIRED_OUTPUTS,
        "input_files": [f"tasks/{task_id}/task.md"],
        "allowed_output_root": f"runs/{task_id}/",
        "forbidden_actions": _FORBIDDEN_ACTIONS,
        "validation_steps": ["validate_yaml_schema", "validate_commands_allowlist"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": max_retries},
        "escalation_policy": {"escalate_if": ["validator_failed_twice"]},
    })


def _valid_output(task_id: str) -> str:
    return (
        f'```yaml file="train_config.yaml"\ntask_id: {task_id}\n```\n'
        f'```md file="commands.md"\npython scripts/run_validation.py\n```\n'
        f'```yaml file="output_contract.yaml"\ntask_id: {task_id}\nrequired_files:\n  - generated/train_config.yaml\n```\n'
        f'```csv file="data_manifest.csv"\npath,split\ndata/train.csv,train\n```\n'
        f'```yaml file="val_config.yaml"\ntask_id: {task_id}\n```\n'
    )


class _CountingClient:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.call_count = 0
        self.received_feedbacks = []

    def materialize(self, work_order_json, prompt_text, context_pack_text, schemas_text, feedback_text=""):
        self.received_feedbacks.append(feedback_text)
        idx = min(self.call_count, len(self._outputs) - 1)
        self.call_count += 1
        return self._outputs[idx]


# ---------------------------------------------------------------------------
# P0: single-call when feedback_text is set
# ---------------------------------------------------------------------------

class TestSingleShotWithFeedback(unittest.TestCase):
    """P0: When feedback_text is non-empty, materializer calls provider exactly once."""

    def test_exactly_one_call_with_feedback_even_on_invalid_output(self):
        task_id = "ss_fb_invalid"
        client = _CountingClient(["```yaml file='train_config.yaml'\nbad: [\n```"])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id)),
                client,
                feedback_text="Fix the missing status field.",
            )
        self.assertEqual(client.call_count, 1,
                         "materializer must call provider exactly once when feedback_text is set")

    def test_exactly_one_call_with_feedback_on_valid_output(self):
        task_id = "ss_fb_valid"
        client = _CountingClient([_valid_output(task_id)])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id)),
                client,
                feedback_text="Previous attempt was missing train_config.yaml.",
            )
        self.assertEqual(client.call_count, 1)

    def test_feedback_text_passed_to_provider(self):
        task_id = "ss_fb_pass"
        client = _CountingClient([_valid_output(task_id)])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id)),
                client,
                feedback_text="SPECIFIC_FEEDBACK_MARKER",
            )
        self.assertEqual(len(client.received_feedbacks), 1)
        self.assertIn("SPECIFIC_FEEDBACK_MARKER", client.received_feedbacks[0])

    def test_zero_feedback_allows_internal_retry(self):
        """Without feedback_text, internal loop respects qwen_max_retries."""
        task_id = "ss_no_fb"
        bad_output = "```yaml file='train_config.yaml'\nbad: [\n```"
        client = _CountingClient([bad_output] * 5)
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id, max_retries=2)),
                client,
                feedback_text="",
            )
        self.assertEqual(client.call_count, 3,
                         "qwen_max_retries=2 means range(3) = 3 attempts")


# ---------------------------------------------------------------------------
# materializer result contract
# ---------------------------------------------------------------------------

class TestMaterializerResultContract(unittest.TestCase):
    """Materializer must always return a result dict with required keys."""

    REQUIRED_KEYS = {"task_id", "status", "generated_files", "validation_summary", "created_at"}

    def _run(self, task_id: str, output: str, feedback: str = "") -> dict:
        client = _CountingClient([output])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            return run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id)),
                client,
                feedback_text=feedback,
            )

    def test_result_has_required_keys_on_success(self):
        result = self._run("rs_ok", _valid_output("rs_ok"))
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"missing required key: {key}")

    def test_result_has_required_keys_on_malformed_yaml(self):
        result = self._run("rs_bad", "```yaml file='train_config.yaml'\nbad: [\n```")
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"missing required key on failure: {key}")

    def test_result_has_required_keys_on_empty_output(self):
        result = self._run("rs_empty", "")
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"missing required key on empty output: {key}")

    def test_result_has_required_keys_with_feedback(self):
        result = self._run("rs_fb", "```yaml file='out.yaml'\nstatus: ok\n```",
                           feedback="Fix the issue above.")
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result)

    def test_status_is_materialized_on_valid_output(self):
        result = self._run("rs_mat", _valid_output("rs_mat"))
        self.assertEqual(result["status"], "materialized")

    def test_status_is_not_success_on_bad_output(self):
        result = self._run("rs_fail", "no artifacts here")
        self.assertNotEqual(result["status"], "materialized")

    def test_generated_files_is_list(self):
        result = self._run("rs_gf", "no artifacts here")
        self.assertIsInstance(result["generated_files"], list)

    def test_validation_summary_has_status(self):
        result = self._run("rs_vs", "no artifacts here")
        self.assertIn("status", result["validation_summary"])

    def test_no_registry_mutation(self):
        """Materializer must not touch the task registry."""
        from solution_agent.orchestrator.registry import load_registry
        registry_before = load_registry()
        self._run("rs_noreg", _valid_output("rs_noreg"))
        registry_after = load_registry()
        self.assertEqual(
            set(registry_before.get("tasks", {}).keys()),
            set(registry_after.get("tasks", {}).keys()),
            "run_materializer must not add/remove registry entries",
        )


# ---------------------------------------------------------------------------
# P0: materializer does not manage feedback loop
# ---------------------------------------------------------------------------

class TestMaterializerDoesNotManageFeedbackLoop(unittest.TestCase):
    """P0: materializer.run() must not update registry attempt state.

    Registry state management belongs to RetryController + orchestrator.
    """

    def test_run_manifest_notes_no_registry_mutation(self):
        """run_manifest.json must include note that materializer does not mutate registry."""
        task_id = "no_reg"
        client = _CountingClient([_valid_output(task_id)])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(_task_md(task_id), encoding="utf-8")
            run_materializer(
                task_path, tmp / "runs",
                StaticReaderClient(_work_order(task_id)),
                client,
            )
            run_dir = tmp / "runs" / task_id
            manifest_path = run_dir / "run_manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                notes = manifest.get("notes", [])
                has_note = any("registry" in note.lower() for note in notes)
                self.assertTrue(has_note,
                                "run_manifest notes should state materializer does not mutate registry")


if __name__ == "__main__":
    unittest.main()

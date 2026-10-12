from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import WorkspaceTempDir
from solution_agent.remote_materializer.controller import run_materializer
from solution_agent.remote_materializer.model_clients import StaticMaterializerClient, StaticReaderClient


FORBIDDEN = [
    "modify_source_code",
    "execute_training",
    "change_dataset_split",
    "overwrite_checkpoint",
    "delete_files",
]


def work_order(task_id: str, task_type: str, outputs: list[str]) -> str:
    return json.dumps(
        {
            "status": "ready",
            "task_id": task_id,
            "task_type": task_type,
            "source_task_path": f"tasks/{task_id}/task.md",
            "jobs": [{"job_id": "generate", "worker": "qwen_coder_30b", "job_type": "generate_materials"}],
            "required_outputs": outputs,
            "input_files": [f"tasks/{task_id}/task.md"],
            "allowed_output_root": f"runs/{task_id}/",
            "forbidden_actions": FORBIDDEN,
            "validation_steps": ["validate_yaml_schema", "validate_csv_columns", "validate_commands_allowlist"],
            "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
            "escalation_policy": {"escalate_if": ["validator_failed_twice"]},
        }
    )


def task_md(task_id: str, task_type: str) -> str:
    return f"""---
task_id: {task_id}
title: Example
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-02T12:00:00+08:00
task_type: {task_type}
---

# Goal

Generate materials.
"""


class RemoteMaterializerControllerTests(unittest.TestCase):
    def test_training_validation_happy_path(self):
        outputs = ["train_config.yaml", "val_config.yaml", "data_manifest.csv", "commands.md", "output_contract.yaml"]
        qwen = """```yaml file="train_config.yaml"
task_id: tv_test
output_dir: runs/tv_test/generated
```
```yaml file="val_config.yaml"
task_id: tv_test
output_dir: runs/tv_test/generated
```
```csv file="data_manifest.csv"
path,label
runs/tv_test/input/sample.jpg,real
```
```md file="commands.md"
```bash
python scripts/run_validation.py --config runs/tv_test/generated/val_config.yaml
```
```
```yaml file="output_contract.yaml"
task_id: tv_test
required_files:
  - generated/train_config.yaml
  - generated/val_config.yaml
```
"""
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(task_md("tv_test", "training_validation"), encoding="utf-8")
            package = run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(work_order("tv_test", "training_validation", outputs)),
                StaticMaterializerClient([qwen]),
            )
            manifest_exists = (tmp / "runs" / "tv_test" / "run_manifest.json").exists()

        self.assertEqual(package["status"], "materialized")
        self.assertEqual(package["validation_summary"]["status"], "pass")
        self.assertTrue(manifest_exists)

    def test_probe_inference_happy_path(self):
        outputs = [
            "probe_infer_config.yaml",
            "sample_manifest.csv",
            "probe_axes.csv",
            "commands.md",
            "expected_tables.md",
            "output_contract.yaml",
        ]
        qwen = """```yaml file="probe_infer_config.yaml"
task_id: probe_test
output_dir: runs/probe_test/generated
```
```csv file="sample_manifest.csv"
path,label
runs/probe_test/input/sample.jpg,real
```
```csv file="probe_axes.csv"
axis,description
jpeg_stress,JPEG stress axis
```
```md file="commands.md"
python scripts/run_probe.py --config runs/probe_test/generated/probe_infer_config.yaml
```
```md file="expected_tables.md"
axis | metric | value
```
```yaml file="output_contract.yaml"
task_id: probe_test
required_files:
  - generated/probe_infer_config.yaml
  - generated/probe_axes.csv
```
"""
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(task_md("probe_test", "probe_inference"), encoding="utf-8")
            package = run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(work_order("probe_test", "probe_inference", outputs)),
                StaticMaterializerClient([qwen]),
            )

        self.assertEqual(package["status"], "materialized")
        self.assertEqual(package["validation_summary"]["status"], "pass")

    def test_ambiguous_skips_qwen(self):
        ambiguous = json.dumps(
            {
                "status": "ambiguous",
                "missing_fields": ["checkpoint"],
                "reason": "checkpoint is missing",
                "need_human_or_codex": True,
            }
        )
        qwen = StaticMaterializerClient([""])
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(task_md("ambiguous_test", "probe_inference"), encoding="utf-8")
            package = run_materializer(task_path, tmp / "runs", StaticReaderClient(ambiguous), qwen)

        self.assertEqual(package["status"], "ambiguous")
        self.assertEqual(qwen.calls, 0)

    def test_ambiguous_reader_normalizes_machine_owned_fields(self):
        ambiguous = json.dumps(
            {
                "status": "ambiguous",
                "missing_fields": ["source_task_path", "retry_policy", "escalation_policy"],
                "reason": "machine-owned fields are not explicit",
                "need_human_or_codex": True,
            }
        )
        qwen = """```yaml file="smoke_config.yaml"
task_id: normalized_test
script_id: solution_agent_smoke_write_metrics
output_dir: runs/normalized_test
message: ok
```
```md file="commands.md"
python -m solution_agent.execution_port.smoke_script --output_dir runs/normalized_test --message ok
```
```yaml file="output_contract.yaml"
required_files:
  - metrics.json
  - artifact.txt
```
"""
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(
                """---
task_id: normalized_test
title: Normalize ambiguous reader output
priority: normal
worker: remote_materializer
status: pending
task_type: material_regeneration
---

# Goal

Generate smoke materials.

# Required Outputs

- smoke_config.yaml
- commands.md
- output_contract.yaml
""",
                encoding="utf-8",
            )
            package = run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(ambiguous),
                StaticMaterializerClient([qwen]),
            )
            work_order_payload = json.loads(
                (tmp / "runs" / "normalized_test" / "work_order.json").read_text(encoding="utf-8")
            )

        self.assertEqual(package["status"], "materialized")
        self.assertTrue(work_order_payload["reader_meta"]["normalized_from_ambiguous"])
        self.assertEqual(work_order_payload["task_type"], "material_regeneration")

    def test_ready_reader_normalizes_machine_owned_safety_defaults(self):
        partial_forbidden = json.dumps(
            {
                "status": "ready",
                "task_id": "ready_normalized_test",
                "task_type": "material_regeneration",
                "source_task_path": "",
                "jobs": [{"name": "materialize"}],
                "required_outputs": ["smoke_config.yaml", "commands.md", "output_contract.yaml"],
                "input_files": [],
                "allowed_output_root": "runs/ready_normalized_test/",
                "forbidden_actions": ["modify_source_code", "execute_commands"],
                "validation_steps": ["validate generated files"],
                "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 0},
                "escalation_policy": {"escalate_if": "validator_failed"},
            }
        )
        qwen = """```yaml file="smoke_config.yaml"
task_id: ready_normalized_test
script_id: solution_agent_smoke_write_metrics
output_dir: runs/ready_normalized_test
message: ok
```
```md file="commands.md"
python -m solution_agent.execution_port.smoke_script --output_dir runs/ready_normalized_test --message ok
```
```yaml file="output_contract.yaml"
required_files:
  - metrics.json
  - artifact.txt
```
"""
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(task_md("ready_normalized_test", "material_regeneration"), encoding="utf-8")
            package = run_materializer(
                task_path,
                tmp / "runs",
                StaticReaderClient(partial_forbidden),
                StaticMaterializerClient([qwen]),
            )
            work_order_payload = json.loads(
                (tmp / "runs" / "ready_normalized_test" / "work_order.json").read_text(encoding="utf-8")
            )

        self.assertEqual(package["status"], "materialized")
        self.assertIn("execute_training", work_order_payload["forbidden_actions"])
        self.assertIn("change_dataset_split", work_order_payload["forbidden_actions"])
        self.assertIn("overwrite_checkpoint", work_order_payload["forbidden_actions"])
        self.assertIn("delete_files", work_order_payload["forbidden_actions"])
        self.assertTrue(work_order_payload["source_task_path"])
        self.assertTrue(work_order_payload["reader_meta"]["normalized_safety_defaults"])

    def test_cli_runs_with_static_outputs(self):
        outputs = ["train_config.yaml", "val_config.yaml", "data_manifest.csv", "commands.md", "output_contract.yaml"]
        qwen = """```yaml file="train_config.yaml"
task_id: cli_test
output_dir: runs/cli_test/generated
```
```yaml file="val_config.yaml"
task_id: cli_test
output_dir: runs/cli_test/generated
```
```csv file="data_manifest.csv"
path,label
runs/cli_test/input/sample.jpg,real
```
```md file="commands.md"
python scripts/run_validation.py --config runs/cli_test/generated/val_config.yaml
```
```yaml file="output_contract.yaml"
task_id: cli_test
required_files:
  - generated/train_config.yaml
```
"""
        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            reader_path = tmp / "reader.json"
            qwen_path = tmp / "qwen.md"
            task_path.write_text(task_md("cli_test", "training_validation"), encoding="utf-8")
            reader_path.write_text(work_order("cli_test", "training_validation", outputs), encoding="utf-8")
            qwen_path.write_text(qwen, encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "solution_agent.remote_materializer",
                    "--task",
                    str(task_path),
                    "--output-root",
                    str(tmp / "runs"),
                    "--reader-output",
                    str(reader_path),
                    "--materializer-output",
                    str(qwen_path),
                    "--print-json",
                ],
                cwd=Path.cwd(),
                text=True,
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"status": "materialized"', completed.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from helpers import WorkspaceTempDir
from solution_agent.remote_materializer.context_collector import collect_context_pack
from solution_agent.remote_materializer.controller import _read_context_pack
from solution_agent.remote_materializer.validators import validate_patch_allowed_files


FORBIDDEN = [
    "modify_source_code",
    "execute_training",
    "change_dataset_split",
    "overwrite_checkpoint",
    "delete_files",
]


def write_task(path, task_id: str) -> None:
    path.write_text(
        f"""---
task_id: {task_id}
title: Context task
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-02T12:00:00+08:00
---

# Goal

Collect context.
""",
        encoding="utf-8",
    )


def write_work_order(path, task_id: str, context_request: dict | None = None) -> None:
    payload = {
        "status": "ready",
        "task_id": task_id,
        "task_type": "training_validation",
        "source_task_path": f"tasks/{task_id}/task.md",
        "jobs": [{"job_id": "generate"}],
        "required_outputs": ["commands.md"],
        "input_files": [f"tasks/{task_id}/task.md"],
        "allowed_output_root": f"runs/{task_id}/",
        "forbidden_actions": FORBIDDEN,
        "validation_steps": ["validate_commands_allowlist"],
        "retry_policy": {"reader_max_retries": 1, "qwen_max_retries": 2},
        "escalation_policy": {"escalate_if": ["validator_failed_twice"]},
    }
    if context_request:
        payload["context_request"] = context_request
    path.write_text(json.dumps(payload), encoding="utf-8")


class RemoteMaterializerContextTests(unittest.TestCase):
    def test_materialize_context_pack_has_no_source_code(self):
        with WorkspaceTempDir() as tmp:
            task = tmp / "task.md"
            work_order = tmp / "work_order.json"
            run_dir = tmp / "runs" / "mat"
            write_task(task, "mat")
            write_work_order(work_order, "mat")

            result = collect_context_pack(tmp, run_dir, task, work_order)

            relevant = (run_dir / "context_pack" / "relevant_code.md").read_text(encoding="utf-8")
            manifest = (run_dir / "context_pack" / "manifest.yaml").read_text(encoding="utf-8")
            hints = (run_dir / "context_pack" / "execution_hints.yaml").read_text(encoding="utf-8")
        self.assertEqual(result.status, "pass")
        self.assertEqual(relevant.strip(), "")
        self.assertIn("context_pack_id:", manifest)
        self.assertIn("api_access_policy: context_pack_only", hints)

    def test_patch_low_risk_includes_only_allowed_files(self):
        with WorkspaceTempDir() as tmp:
            (tmp / "tools").mkdir()
            (tmp / "tools" / "build_manifest.py").write_text("print('ok')\n", encoding="utf-8")
            task = tmp / "task.md"
            work_order = tmp / "work_order.json"
            run_dir = tmp / "runs" / "patch"
            write_task(task, "patch")
            write_work_order(
                work_order,
                "patch",
                {
                    "task_id": "patch",
                    "context_level": "patch_low_risk",
                    "needed_files": ["tools/build_manifest.py"],
                    "reason": "Need helper source.",
                },
            )

            result = collect_context_pack(tmp, run_dir, task, work_order, json.loads(work_order.read_text())["context_request"])
            relevant = (run_dir / "context_pack" / "relevant_code.md").read_text(encoding="utf-8")

        self.assertEqual(result.status, "pass")
        self.assertIn("tools/build_manifest.py", relevant)

    def test_forbidden_request_fails(self):
        with WorkspaceTempDir() as tmp:
            (tmp / "models").mkdir()
            (tmp / "models" / "model.py").write_text("class Model: pass\n", encoding="utf-8")
            task = tmp / "task.md"
            work_order = tmp / "work_order.json"
            run_dir = tmp / "runs" / "bad"
            request = {
                "task_id": "bad",
                "context_level": "patch_low_risk",
                "needed_files": ["models/model.py"],
                "reason": "Forbidden model edit.",
            }
            write_task(task, "bad")
            write_work_order(work_order, "bad", request)

            result = collect_context_pack(tmp, run_dir, task, work_order, request)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any(item["reason"] == "forbidden_path" for item in result.rejected_files))

    def test_path_escape_request_fails(self):
        with WorkspaceTempDir() as tmp:
            task = tmp / "task.md"
            work_order = tmp / "work_order.json"
            run_dir = tmp / "runs" / "escape"
            request = {
                "task_id": "escape",
                "context_level": "patch_low_risk",
                "needed_files": ["../../.env"],
                "reason": "Path escape.",
            }
            write_task(task, "escape")
            write_work_order(work_order, "escape", request)

            result = collect_context_pack(tmp, run_dir, task, work_order, request)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any(item["reason"] == "path_escape_or_absolute" for item in result.rejected_files))

    def test_patch_forbidden_path_fails(self):
        with WorkspaceTempDir() as tmp:
            patch = tmp / "patch.diff"
            patch.write_text(
                """diff --git a/losses/loss.py b/losses/loss.py
--- a/losses/loss.py
+++ b/losses/loss.py
@@ -1 +1 @@
-old
+new
""",
                encoding="utf-8",
            )

            result = validate_patch_allowed_files(patch)

        self.assertEqual(result.status, "fail")
        self.assertTrue(any("losses/loss.py" in item for item in result.errors))

    def test_read_context_pack_uses_gateway_order_and_omits_unlisted_files(self):
        with WorkspaceTempDir() as tmp:
            pack = tmp / "context_pack"
            pack.mkdir()
            (pack / "task_brief.md").write_text("task\n", encoding="utf-8")
            (pack / "manifest.yaml").write_text("manifest\n", encoding="utf-8")
            (pack / "workspace_context_excerpt.yaml").write_text("workspace_context\n", encoding="utf-8")
            (pack / "project_map_excerpt.yaml").write_text("project_map\n", encoding="utf-8")
            (pack / "retrieval_context.md").write_text("retrieval\n", encoding="utf-8")
            (pack / "raw_dump.txt").write_text("do not include\n", encoding="utf-8")

            text = _read_context_pack(pack)

        self.assertLess(text.index("manifest.yaml"), text.index("task_brief.md"))
        self.assertIn("workspace_context_excerpt.yaml", text)
        self.assertIn("workspace_context", text)
        self.assertIn("project_map_excerpt.yaml", text)
        self.assertIn("retrieval_context.md", text)
        self.assertNotIn("raw_dump.txt", text)
        self.assertNotIn("do not include", text)


if __name__ == "__main__":
    unittest.main()

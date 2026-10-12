from __future__ import annotations

from contextlib import ExitStack, contextmanager
import uuid
import unittest
from unittest.mock import patch

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import (
    task_dir,
    worker_out_dir,
    result_latest_dir,
    result_run_dir,
)
from solution_agent.orchestrator.registry import load_registry, save_registry
from tests.helpers import WorkspaceTempDir


class ContinuationChainCollectTests(unittest.TestCase):
    @contextmanager
    def _patch_workspace(self, tmp):
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple(
                    "solution_agent.orchestrator.paths",
                    root=lambda: tmp,
                    registry_path=lambda: tmp / "registry.json",
                )
            )
            stack.enter_context(
                patch.multiple(
                    "solution_agent.orchestrator.core",
                    root=lambda: tmp,
                    state_dir=lambda: tmp / "state",
                )
            )
            stack.enter_context(
                patch.multiple(
                    "solution_agent.orchestrator.registry",
                    registry_path=lambda: tmp / "registry.json",
                )
            )
            yield

    def _write_task(
        self,
        task_id: str,
        run_id: str,
        *,
        continuation: str,
        status: str = "running",
        result_status: str = "success",
    ) -> None:
        folder = task_dir(status, task_id)
        folder.mkdir(parents=True, exist_ok=True)
        folder.joinpath("task.md").write_text(
            f"""---
task_id: {task_id}
title: {task_id}
worker: materialized_execution
status: {status}
line: A_residual_correction
batch: A_residual_correction_inbox_batch
dag_root: a_rescorr_001_prepare_contract_inventory
resource_hint: cpu
created_at: 2026-05-13T00:00:00+08:00
{continuation}---

# Task

Residual continuity test.
""",
            encoding="utf-8",
        )
        out_dir = worker_out_dir(task_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir.joinpath("result.yaml").write_text(
            "\n".join(
                [
                    f"task_id: {task_id}",
                    f"run_id: {run_id}",
                    f"status: {result_status}",
                    "worker: materialized_execution",
                    "summary_short: collected",
                    "changed_files: []",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_dir.joinpath("worker_notes.md").write_text("note", encoding="utf-8")
        out_dir.joinpath("stdout.log").write_text("", encoding="utf-8")
        out_dir.joinpath("stderr.log").write_text("", encoding="utf-8")

    def test_collect_success_emits_continuation_task(self):
        with WorkspaceTempDir() as tmp:
            with self._patch_workspace(tmp):
                task_id = f"cont_parent_{uuid.uuid4().hex[:8]}"
                continuation_task_id = f"cont_next_{uuid.uuid4().hex[:8]}"
                run_id = f"{task_id}__run"
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
                    path=tmp / "registry.json",
                )
                continuation = f"""continuation:
  when: success
  emit_task:
    task_id: {continuation_task_id}
    worker: materialized_execution
    title: Continuation from {task_id}
"""
                self._write_task(task_id, run_id, continuation=continuation, result_status="success")

                result = core.collect(task_id)

                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["continuation"].get("emitted"))
                self.assertEqual(result["continuation"].get("task_id"), continuation_task_id)
                continuation_task = task_dir("pending", continuation_task_id) / "task.md"
                self.assertTrue(continuation_task.exists())

                latest = result_latest_dir(task_id)
                self.assertTrue((latest / "result.yaml").exists())
                # Continuation should not require action-request fallback.
                self.assertIsNone(result["continuation"].get("action_request"))

    def test_collect_when_continuation_missing_task_id_creates_action_request(self):
        with WorkspaceTempDir() as tmp:
            with self._patch_workspace(tmp):
                task_id = f"cont_parent_{uuid.uuid4().hex[:8]}"
                run_id = f"{task_id}__run"
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
                    path=tmp / "registry.json",
                )
                continuation = """continuation:
  when: success
  emit_task: {}
"""
                self._write_task(task_id, run_id, continuation=continuation, result_status="success")

                result = core.collect(task_id)

                self.assertEqual(result["status"], "completed")
                self.assertFalse(result["continuation"].get("emitted"))
                self.assertEqual(result["continuation"].get("reason"), "continuation_emit_task_id_missing")
                pending = core.list_action_requests("pending")
                self.assertEqual(pending["counts"].get("pending"), 1)
                request = pending["requests"][0]
            self.assertEqual(request["action_type"], "continuation_waiting")
            self.assertEqual(request["payload"]["source_task_id"], task_id)
            self.assertEqual(request["payload"]["result_status"], "success")

    def test_collect_when_continuation_missing_task_id_is_idempotent(self):
        with WorkspaceTempDir() as tmp:
            with self._patch_workspace(tmp):
                task_id = f"cont_parent_{uuid.uuid4().hex[:8]}"
                run_id = f"{task_id}__run"
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
                    path=tmp / "registry.json",
                )
                continuation = """continuation:
  when: success
  emit_task: {}
"""
                self._write_task(task_id, run_id, continuation=continuation, result_status="success")

                first = core.collect(task_id)
                second = core.collect(task_id)
                listed = core.list_action_requests("pending")

                self.assertEqual(first["status"], "completed")
                self.assertEqual(second["status"], "completed")
                self.assertFalse(first["continuation"].get("emitted"))
                self.assertFalse(second["continuation"].get("emitted"))
                self.assertEqual(first["continuation"].get("reason"), "continuation_emit_task_id_missing")
                self.assertEqual(second["continuation"].get("reason"), "continuation_emit_task_id_missing")
                self.assertEqual(listed["counts"].get("pending"), 1)


if __name__ == "__main__":
    unittest.main()

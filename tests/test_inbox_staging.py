"""Tests for the dashboard inbox staging flow.

Covers:
  * folder + zip containers with TASK_BATCH_INDEX*.md are reported as valid lines;
  * folder/zip without an index appears as an anomaly;
  * promote_from_staging() writes pending + archives index + task into processed;
  * discard_from_staging() moves a task / container into _discarded/.
"""

from __future__ import annotations

import shutil
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from helpers import WorkspaceTempDir
from solution_agent import web
from solution_agent.orchestrator import core
from solution_agent.orchestrator.registry import save_registry


TASK_MD = """---
task_id: {task_id}
title: {title}
priority: normal
worker: codex
status: pending
created_at: '2026-05-01T00:00:00+00:00'
objective: do a thing
success_criteria:
  - Produce result.yaml
expected_outputs:
  - result.yaml
---

# Task

Body for {task_id}.
"""


def _write_task_md(target: Path, task_id: str, title: str = "", extra_front_matter: str = "") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = TASK_MD.format(task_id=task_id, title=title or task_id)
    if extra_front_matter:
        text = text.replace("---\n\n# Task", extra_front_matter + "---\n\n# Task")
    target.write_text(text, encoding="utf-8")


def _write_index(target: Path, body: str = "# Batch index\n\nfoo bar\n") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


class InboxStagingTests(unittest.TestCase):
    def _patch_paths(self, tmp: Path):
        return patch.multiple(
            "solution_agent.orchestrator.core",
            state_dir=lambda: tmp / "state",
            registry_path=lambda: tmp / "registry.json",
            root=lambda: tmp,
        )

    def _patch_paths_module(self, tmp: Path):
        # `task_file` in paths.py uses its own `root()` import; patch there too.
        return patch("solution_agent.orchestrator.paths.project_root", return_value=tmp)

    # ------------------------------------------------------------------
    # scan_inbox_staging
    # ------------------------------------------------------------------

    def test_folder_with_index_is_a_valid_line(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "recipe_batch_03"
            _write_index(line / "TASK_BATCH_INDEX_RECIPE_ABLATION.md")
            _write_task_md(line / "exp_010_prepare.md", "exp_010_prepare")
            _write_task_md(line / "exp_010_run.md", "exp_010_run")

            state = core.scan_inbox_staging()

        self.assertEqual(len(state["lines"]), 1)
        self.assertEqual(state["anomalies"], [])
        line_info = state["lines"][0]
        self.assertEqual(line_info["kind"], "folder")
        self.assertEqual(line_info["name"], "recipe_batch_03")
        self.assertTrue(line_info["valid"])
        self.assertEqual(
            line_info["index_root"], "TASK_BATCH_INDEX_RECIPE_ABLATION"
        )
        ids = {t["task_id"] for t in line_info["tasks"]}
        self.assertEqual(ids, {"exp_010_prepare", "exp_010_run"})

    def test_zip_with_index_is_a_valid_line(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "overnight_batch_05.zip"

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("inner/TASK_BATCH_INDEX.md", "# Batch index\n")
                zf.writestr(
                    "inner/exp_012_prepare.md",
                    TASK_MD.format(task_id="exp_012_prepare", title="exp_012_prepare"),
                )
                zf.writestr(
                    "inner/exp_012_run.md",
                    TASK_MD.format(task_id="exp_012_run", title="exp_012_run"),
                )

            state = core.scan_inbox_staging()

        self.assertEqual(len(state["lines"]), 1)
        self.assertEqual(state["anomalies"], [])
        line_info = state["lines"][0]
        self.assertEqual(line_info["kind"], "zip")
        self.assertEqual(line_info["name"], "overnight_batch_05")
        ids = {t["task_id"] for t in line_info["tasks"]}
        self.assertEqual(ids, {"exp_012_prepare", "exp_012_run"})

    def test_folder_task_with_bom_keeps_explicit_front_matter(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "bom_batch"
            _write_index(line / "TASK_BATCH_INDEX.md")
            task_path = line / "bom_task.md"
            task_path.parent.mkdir(parents=True, exist_ok=True)
            task_path.write_bytes(
                b"\xef\xbb\xbf"
                + TASK_MD.format(task_id="bom_task", title="bom_task").encode("utf-8")
            )

            state = core.scan_inbox_staging()

        task = state["lines"][0]["tasks"][0]
        self.assertEqual(task["task_id"], "bom_task")
        self.assertEqual(task["meta"]["worker"], "codex")
        self.assertNotIn(
            "task file has no explicit YAML front matter; promote would synthesize defaults",
            task["lint"]["warnings"],
        )

    def test_zip_task_with_bom_keeps_explicit_front_matter(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "bom_batch.zip"

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("inner/TASK_BATCH_INDEX.md", "# Batch index\n")
                zf.writestr(
                    "inner/bom_task.md",
                    b"\xef\xbb\xbf"
                    + TASK_MD.format(task_id="zip_bom_task", title="zip_bom_task").encode("utf-8"),
                )

            state = core.scan_inbox_staging()

        task = state["lines"][0]["tasks"][0]
        self.assertEqual(task["task_id"], "zip_bom_task")
        self.assertEqual(task["meta"]["worker"], "codex")
        self.assertNotIn(
            "task file has no explicit YAML front matter; promote would synthesize defaults",
            task["lint"]["warnings"],
        )

    def test_zip_with_multiple_indexes_is_split_into_lines(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "multi_batch.zip"

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("line_a/TASK_BATCH_INDEX_A.md", "# A\n")
                zf.writestr(
                    "line_a/exp_a.md",
                    TASK_MD.format(task_id="exp_a", title="exp_a"),
                )
                zf.writestr("line_b/TASK_BATCH_INDEX_B.md", "# B\n")
                zf.writestr(
                    "line_b/exp_b.md",
                    TASK_MD.format(task_id="exp_b", title="exp_b"),
                )

            state = core.scan_inbox_staging()

        self.assertEqual(len(state["lines"]), 2)
        grouped = {
            line["index_root"]: {t["task_id"] for t in line["tasks"]}
            for line in state["lines"]
        }
        self.assertEqual(grouped["TASK_BATCH_INDEX_A"], {"exp_a"})
        self.assertEqual(grouped["TASK_BATCH_INDEX_B"], {"exp_b"})

    def test_folder_without_index_is_an_anomaly(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "broken_batch"
            _write_task_md(line / "exp_x.md", "exp_x")

            state = core.scan_inbox_staging()

        self.assertEqual(state["lines"], [])
        self.assertEqual(len(state["anomalies"]), 1)
        anomaly = state["anomalies"][0]
        self.assertEqual(anomaly["name"], "broken_batch")
        self.assertEqual(anomaly["kind"], "folder")
        self.assertIn("TASK_BATCH_INDEX", anomaly["reason"])

    def test_zip_without_index_is_an_anomaly(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "loose.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(
                    "exp_y.md",
                    TASK_MD.format(task_id="exp_y", title="exp_y"),
                )

            state = core.scan_inbox_staging()

        self.assertEqual(state["lines"], [])
        self.assertEqual(len(state["anomalies"]), 1)
        self.assertEqual(state["anomalies"][0]["kind"], "zip")

    def test_loose_md_is_an_anomaly(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            _write_task_md(inbox / "stray_task.md", "stray_task")

            state = core.scan_inbox_staging()

        self.assertEqual(state["lines"], [])
        self.assertEqual(len(state["anomalies"]), 1)
        self.assertEqual(state["anomalies"][0]["kind"], "loose_md")

    def test_loose_index_at_inbox_root_is_a_valid_line(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            _write_index(inbox / "TASK_BATCH_INDEX.md", body="# leftover\n")

            state = core.scan_inbox_staging()

        self.assertEqual(len(state["lines"]), 1)
        line_info = state["lines"][0]
        self.assertEqual(line_info["kind"], "loose_index")
        self.assertEqual(line_info["name"], "TASK_BATCH_INDEX")

    def test_processed_archive_is_not_scanned_as_staging(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            processed = tmp / "tasks" / "inbox" / "processed" / "TASK_BATCH_INDEX"
            _write_index(processed / "TASK_BATCH_INDEX.md")
            _write_task_md(processed / "archived_task.md", "archived_task")

            state = core.scan_inbox_staging()

        self.assertEqual(state["lines"], [])
        self.assertEqual(state["anomalies"], [])

    def test_proposed_continuation_staging_is_not_scanned_as_regular_inbox(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            proposed = tmp / "tasks" / "inbox" / "_proposed" / "continuation_batch"
            _write_index(proposed / "TASK_BATCH_INDEX.md")
            _write_task_md(proposed / "proposed_task.md", "proposed_task")

            state = core.scan_inbox_staging()

        self.assertEqual(state["lines"], [])
        self.assertEqual(state["anomalies"], [])

    def test_inbox_lint_blocks_materialized_execution_bypass(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "unsafe_batch"
            _write_index(line / "TASK_BATCH_INDEX.md")
            _write_task_md(
                line / "unsafe_task.md",
                "unsafe_task",
                extra_front_matter="materialized_execution_bypass_materializer: true\n",
            )

            state = core.scan_inbox_staging()
            task = state["lines"][0]["tasks"][0]
            result = core.promote_from_staging(line_name="unsafe_batch")

        self.assertIn("materialized_execution_bypass_materializer is not allowed in inbox tasks", task["lint"]["errors"])
        self.assertEqual(result["promoted"], [])
        self.assertEqual(result["skipped"][0]["reason"], "unsafe_inbox_task")

    def test_dashboard_inbox_task_files_do_not_read_processed_archive(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp), patch(
            "solution_agent.web.root", return_value=tmp
        ):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            _write_task_md(inbox / "fresh_loose.md", "fresh_loose")
            processed = inbox / "processed" / "TASK_BATCH_INDEX"
            _write_task_md(processed / "archived_task.md", "archived_task")

            files = web._inbox_task_files()

        self.assertEqual([path.name for path in files], ["fresh_loose.md"])

    # ------------------------------------------------------------------
    # promote_from_staging
    # ------------------------------------------------------------------

    def test_promote_folder_archives_index_and_task(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "recipe_batch_03"
            _write_index(line / "TASK_BATCH_INDEX_RECIPE_ABLATION.md")
            _write_task_md(line / "exp_010_prepare.md", "exp_010_prepare")
            _write_task_md(line / "exp_010_run.md", "exp_010_run")

            result = core.promote_from_staging(
                line_name="recipe_batch_03",
                task_ids=["exp_010_prepare"],
            )

            # Pending receives the task.
            pending_md = tmp / "tasks" / "pending" / "exp_010_prepare" / "task.md"
            self.assertTrue(pending_md.exists())

            # Processed receives both the index and the task md.
            processed_root = (
                tmp / "tasks" / "inbox" / "processed" / "TASK_BATCH_INDEX_RECIPE_ABLATION"
            )
            self.assertTrue(
                (processed_root / "TASK_BATCH_INDEX_RECIPE_ABLATION.md").exists()
            )
            self.assertTrue(
                (processed_root / "exp_010_prepare" / "exp_010_prepare.md").exists()
            )

            # Source md is gone from staging; the other task remains.
            self.assertFalse((line / "exp_010_prepare.md").exists())
            self.assertTrue((line / "exp_010_run.md").exists())

            self.assertEqual(len(result["promoted"]), 1)
            self.assertEqual(result["index_root"], "TASK_BATCH_INDEX_RECIPE_ABLATION")

    def test_promote_zip_strips_entry(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "overnight_batch_05.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("inner/TASK_BATCH_INDEX.md", "# Batch\n")
                zf.writestr(
                    "inner/exp_012_prepare.md",
                    TASK_MD.format(task_id="exp_012_prepare", title="exp_012_prepare"),
                )
                zf.writestr(
                    "inner/exp_012_run.md",
                    TASK_MD.format(task_id="exp_012_run", title="exp_012_run"),
                )

            result = core.promote_from_staging(
                line_name="overnight_batch_05",
                task_ids=["exp_012_prepare"],
            )

            pending_md = tmp / "tasks" / "pending" / "exp_012_prepare" / "task.md"
            self.assertTrue(pending_md.exists())

            processed_root = tmp / "tasks" / "inbox" / "processed" / "TASK_BATCH_INDEX"
            self.assertTrue((processed_root / "TASK_BATCH_INDEX.md").exists())
            self.assertTrue(
                (processed_root / "exp_012_prepare" / "exp_012_prepare.md").exists()
            )

            # Zip should still exist (one task remains) but no longer include the promoted entry.
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertNotIn("inner/exp_012_prepare.md", names)
            self.assertIn("inner/exp_012_run.md", names)

            self.assertEqual(len(result["promoted"]), 1)

    def test_promote_one_zip_line_does_not_consume_other_line(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            zip_path = inbox / "multi_batch.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("line_a/TASK_BATCH_INDEX_A.md", "# A\n")
                zf.writestr(
                    "line_a/exp_a.md",
                    TASK_MD.format(task_id="exp_a", title="exp_a"),
                )
                zf.writestr("line_b/TASK_BATCH_INDEX_B.md", "# B\n")
                zf.writestr(
                    "line_b/exp_b.md",
                    TASK_MD.format(task_id="exp_b", title="exp_b"),
                )
            state = core.scan_inbox_staging()
            line_a = next(line for line in state["lines"] if line["index_root"] == "TASK_BATCH_INDEX_A")

            result = core.promote_from_staging(line_name=str(line_a["line_key"]))

            self.assertEqual([item["task_id"] for item in result["promoted"]], ["exp_a"])
            self.assertTrue((tmp / "tasks" / "pending" / "exp_a" / "task.md").exists())
            self.assertFalse((tmp / "tasks" / "pending" / "exp_b" / "task.md").exists())
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            self.assertNotIn("line_a/TASK_BATCH_INDEX_A.md", names)
            self.assertNotIn("line_a/exp_a.md", names)
            self.assertIn("line_b/TASK_BATCH_INDEX_B.md", names)
            self.assertIn("line_b/exp_b.md", names)

    # ------------------------------------------------------------------
    # discard_from_staging
    # ------------------------------------------------------------------

    def test_discard_full_folder_container(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            line = inbox / "stale_batch"
            _write_index(line / "TASK_BATCH_INDEX.md")
            _write_task_md(line / "exp_z.md", "exp_z")

            result = core.discard_from_staging(line_name="stale_batch")

            self.assertTrue(result["discarded_full_container"])
            moved = Path(result["moved_to"])
            self.assertTrue(moved.exists())
            self.assertFalse(line.exists())
            self.assertIn("_discarded", str(moved))

    def test_discard_anomaly_loose_md(self):
        with WorkspaceTempDir() as tmp, self._patch_paths(tmp), self._patch_paths_module(tmp):
            save_registry({"tasks": {}}, path=tmp / "registry.json")
            inbox = tmp / "tasks" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            loose = inbox / "stray.md"
            _write_task_md(loose, "stray")

            result = core.discard_from_staging(line_name="stray.md")

            self.assertTrue(result["discarded_full_container"])
            moved = Path(result["moved_to"])
            self.assertTrue(moved.exists())
            self.assertFalse(loose.exists())


if __name__ == "__main__":
    unittest.main()

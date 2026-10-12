"""Tests for task_contract.py — parse task.md."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.small_task_worker.task_contract import parse_task_md, _ALLOWED_TASK_TYPES


def _write_task(tmp: Path, content: str) -> Path:
    p = tmp / "task.md"
    p.write_text(content, encoding="utf-8")
    return p


_FULL_TASK = """\
---
task_id: test_task_001
title: Fix probe output
task_type: small_probe
worker: small_task_worker
status: pending
---

## Goal

Fix the probe script to output valid JSON.

## Context Paths

- scripts/probes/run_probe.py
- tests/test_probe.py

## Current Problem

The probe crashes with a KeyError.

## Expected Behavior

The probe exits 0 and writes metrics.json.

## Allowed Modify

- scripts/probes/
- tests/

## Allowed Commands

```bash
python -m pytest tests/test_probe.py
python -m py_compile scripts/probes/run_probe.py
```

## Forbidden Actions

- no full training
- no dataset split changes

## Expected Outputs

- result.yaml
- worker_notes.md
- patch.diff

## Done Definition

pytest exits 0, metrics.json is present.

## Escalation Rules

Escalate if change touches more than 3 modules.
"""


class TestParseFullTask(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = WorkspaceTempDir()
        self.tmp = self._tmp_ctx.__enter__()
        self.task_path = _write_task(self.tmp, _FULL_TASK)
        self.contract = parse_task_md(self.task_path)

    def tearDown(self):
        self._tmp_ctx.__exit__(None, None, None)

    def test_task_id(self):
        self.assertEqual(self.contract.task_id, "test_task_001")

    def test_title(self):
        self.assertEqual(self.contract.title, "Fix probe output")

    def test_task_type(self):
        self.assertEqual(self.contract.task_type, "small_probe")

    def test_goal_nonempty(self):
        self.assertIn("valid JSON", self.contract.goal)

    def test_context_paths(self):
        self.assertIn("scripts/probes/run_probe.py", self.contract.context_paths)
        self.assertIn("tests/test_probe.py", self.contract.context_paths)

    def test_current_problem(self):
        self.assertIn("KeyError", self.contract.current_problem)

    def test_allowed_modify(self):
        self.assertIn("scripts/probes/", self.contract.allowed_modify)
        self.assertIn("tests/", self.contract.allowed_modify)

    def test_allowed_commands(self):
        cmds = "\n".join(self.contract.allowed_commands)
        self.assertIn("pytest", cmds)
        self.assertIn("py_compile", cmds)

    def test_forbidden_actions(self):
        text = "\n".join(self.contract.forbidden_actions)
        self.assertIn("training", text.lower())

    def test_expected_outputs(self):
        self.assertIn("result.yaml", self.contract.expected_outputs)

    def test_done_definition_nonempty(self):
        self.assertIn("pytest", self.contract.done_definition)

    def test_escalation_rules_nonempty(self):
        self.assertTrue(len(self.contract.escalation_rules) > 0)


class TestParseMinimalTask(unittest.TestCase):
    """Task with no sections — must not crash, must return defaults."""

    def test_minimal_front_matter_only(self):
        with WorkspaceTempDir() as tmp:
            p = _write_task(tmp, "---\ntask_id: t001\ntitle: Test\n---\n")
            c = parse_task_md(p)
        self.assertEqual(c.task_id, "t001")
        self.assertEqual(c.title, "Test")
        self.assertIsInstance(c.allowed_commands, list)
        self.assertIsInstance(c.forbidden_actions, list)
        self.assertTrue(len(c.allowed_commands) > 0)

    def test_no_front_matter_uses_filename(self):
        with WorkspaceTempDir() as tmp:
            p = tmp / "my_task.md"
            p.write_text("# Just a title\n", encoding="utf-8")
            c = parse_task_md(p)
        self.assertEqual(c.task_id, "my_task")

    def test_unknown_task_type_defaults_to_small_probe(self):
        with WorkspaceTempDir() as tmp:
            p = _write_task(tmp, "---\ntask_id: t\ntitle: T\ntask_type: gpu_training\n---\n")
            c = parse_task_md(p)
        self.assertEqual(c.task_type, "small_probe")

    def test_all_allowed_task_types_accepted(self):
        for t in _ALLOWED_TASK_TYPES:
            with WorkspaceTempDir() as tmp:
                p = _write_task(tmp, f"---\ntask_id: t\ntitle: T\ntask_type: {t}\n---\n")
                c = parse_task_md(p)
            self.assertEqual(c.task_type, t)

    def test_bom_front_matter_is_supported(self):
        with WorkspaceTempDir() as tmp:
            p = tmp / "bom_task.md"
            p.write_bytes("---\ntask_id: bom_task\ntitle: BOM Task\n---\n".encode("utf-8-sig"))
            c = parse_task_md(p)
        self.assertEqual(c.task_id, "bom_task")


class TestParseListSections(unittest.TestCase):
    def test_list_items_stripped_of_markers(self):
        content = "---\ntask_id: t\ntitle: T\n---\n## Allowed Modify\n\n- scripts/\n- tests/\n"
        with WorkspaceTempDir() as tmp:
            c = parse_task_md(_write_task(tmp, content))
        self.assertIn("scripts/", c.allowed_modify)
        self.assertIn("tests/", c.allowed_modify)
        for item in c.allowed_modify:
            self.assertFalse(item.startswith("- "), f"marker not stripped: {item!r}")

    def test_code_fence_lines_excluded(self):
        content = "---\ntask_id: t\ntitle: T\n---\n## Allowed Commands\n\n```bash\npython -m pytest\n```\n"
        with WorkspaceTempDir() as tmp:
            c = parse_task_md(_write_task(tmp, content))
        for item in c.allowed_commands:
            self.assertNotIn("```", item)


if __name__ == "__main__":
    unittest.main()

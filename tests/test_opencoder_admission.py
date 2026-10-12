from __future__ import annotations

import json
import yaml

from solution_agent.small_task_worker.admission import check_opencoder_admission
from solution_agent.small_task_worker.runner import run_small_task_worker
from solution_agent.small_task_worker.task_contract import parse_task_md
from tests.helpers import WorkspaceTempDir


def _write_task(tmp, text: str):
    path = tmp / "task.md"
    path.write_text(text, encoding="utf-8")
    return path


GOOD_TASK = """\
---
task_id: good_opencoder
title: Good OpenCoder task
worker: small_task_worker
status: pending
---

## Goal

Patch a bounded helper.

## Context Paths

- solution_agent/tools/patch_tools.py

## Allowed Modify

- solution_agent/tools/patch_tools.py

## Allowed Commands

- python -m py_compile solution_agent/tools/patch_tools.py

## Expected Outputs

- result.yaml
- worker_notes.md
- patch.diff

## Done Definition

py_compile passes.
"""


def test_opencoder_admission_accepts_bounded_patch_task():
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, GOOD_TASK))

    result = check_opencoder_admission(task)

    assert result.ok is True
    assert result.errors == []


def test_opencoder_admission_rejects_broad_modify_path():
    task_text = GOOD_TASK.replace("- solution_agent/tools/patch_tools.py\n\n## Allowed Commands", "- .\n\n## Allowed Commands")
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert result.errors[0].code == "broad_allowed_modify"


def test_opencoder_admission_rejects_dangerous_allowed_command():
    task_text = GOOD_TASK.replace("- python -m py_compile solution_agent/tools/patch_tools.py", "- git reset --hard")
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "dangerous_allowed_command" for issue in result.errors)


def test_opencoder_admission_rejects_long_experiment_allowed_command_for_patch_task():
    task_text = GOOD_TASK.replace("- python -m py_compile solution_agent/tools/patch_tools.py", "- torchrun train.py --epochs 10")
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "long_running_experiment_command" for issue in result.errors)


def test_opencoder_admission_rejects_unsafe_context_path():
    task_text = GOOD_TASK.replace("- solution_agent/tools/patch_tools.py\n\n## Allowed Modify", "- ../outside.py\n\n## Allowed Modify")
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "unsafe_context_path" for issue in result.errors)


def test_opencoder_admission_rejects_run_stage_source_edit_scope():
    task_text = GOOD_TASK.replace(
        "status: pending\n---",
        "status: pending\nexecution_stage: run\nallow_long_running_commands: true\n---",
    )
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is False
    assert any(issue.code == "run_stage_declares_allowed_modify" for issue in result.errors)


def test_opencoder_admission_allows_run_stage_without_source_edit_scope():
    task_text = """\
---
task_id: run_only_opencoder
title: Run only OpenCoder task
worker: small_task_worker
status: pending
execution_stage: run
allow_long_running_commands: true
---

## Goal

Run a bounded smoke command.

## Context Paths

- scripts/probes/example.py

## Run Command

python scripts/probes/example.py --help

## Expected Outputs

- result.yaml
- worker_notes.md

## Done Definition

The command exits cleanly or reports a bounded failure.
"""
    with WorkspaceTempDir() as tmp:
        task = parse_task_md(_write_task(tmp, task_text))

    result = check_opencoder_admission(task)

    assert result.ok is True
    assert result.errors == []


def test_runner_writes_admission_and_escalates_before_patch_loop():
    bad_task = GOOD_TASK.replace("- solution_agent/tools/patch_tools.py\n\n## Allowed Commands", "- .\n\n## Allowed Commands")
    with WorkspaceTempDir() as tmp:
        task_path = _write_task(tmp, bad_task)
        out_dir = tmp / "_worker_out"

        result = run_small_task_worker(task_path=task_path, worktree=tmp, out_dir=out_dir)

        admission = json.loads((out_dir / "opencoder_admission.json").read_text(encoding="utf-8"))
        action_requests = json.loads((out_dir / "action_requests.json").read_text(encoding="utf-8"))
        result_yaml = yaml.safe_load((out_dir / "result.yaml").read_text(encoding="utf-8"))
        assert admission["ok"] is False
        assert result["status"] == "escalation_required"
        assert result_yaml["status"] == "escalation_required"
        assert result_yaml["patch"]["applied"] is False
        assert action_requests["action_requests"][0]["kind"] == "request_codex_review"
        assert action_requests["action_requests"][0]["missing_or_needed"] == "codex_handoff_required"
        assert action_requests["action_requests"][0]["blocks_execution"] is True

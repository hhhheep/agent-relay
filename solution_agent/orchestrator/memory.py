from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .paths import (
    codex_worker_profile_path,
    file_index_path,
    last_summary_path,
    memory_path,
    project_context_path,
    task_history_path,
    workflow_profile_path,
)
from .schema import TaskBook, WorkerResult, now_iso


WORKFLOW_PROFILE_CORE_RULES = (
    "Start from the local clone and existing task artifacts before using external references.",
    "Preserve the task book's intent; metabolize routing, contracts, and validation scaffolding instead of rewriting scientific instructions.",
    "For repair work, score modules first, pick the lowest-scoring bounded fix, then make one focused change.",
    "Run narrow tests for the changed layer before the wider governance regression.",
    "Missing cache, script, or capability is a partial result with a concrete action request, not an experiment failure.",
    "Do not invent cache paths, metric files, or scientific conclusions.",
    "Treat worker, line, batch, dag root, and prefix as admission filters before dispatch.",
    "Do not launch unrelated experiment lines as backfill unless the task explicitly allows it.",
    "Stop for user judgment when a fix becomes a large design decision.",
)


def _repair_workflow_profile_core_rules() -> None:
    path = workflow_profile_path()
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    missing = [rule for rule in WORKFLOW_PROFILE_CORE_RULES if rule.lower() not in lowered]
    if not missing:
        return
    append_lines(
        path,
        [
            "",
            "## Managed Core Rules",
            "",
            "The orchestrator restored these stable workflow rules because they were missing from the local profile:",
            *[f"- {rule}" for rule in missing],
            "",
        ],
    )


def ensure_local_memory_files() -> None:
    project_context_path().parent.mkdir(parents=True, exist_ok=True)
    if not project_context_path().exists():
        project_context_path().write_text(
            "# Project Context\n\nAdd stable project background, paths, conventions, and long-running goals here.\n",
            encoding="utf-8",
        )
    if not last_summary_path().exists():
        last_summary_path().write_text("# Last Summary\n\nNo completed task yet.\n", encoding="utf-8")
    if not memory_path().exists():
        memory_path().write_text(
            "\n".join(
                [
                    "# Project Memory",
                    "",
                    "## Stable Facts",
                    "- Main remote workdir can be supplied with `--remote-workdir`.",
                    "- Task outputs are written to the task-specific `_worker_out` path supplied in the prompt.",
                    "",
                    "## Current Experiment Direction",
                    "- Add durable facts here as the task sequence evolves.",
                    "",
                    "## Known Pitfalls",
                    "- Remote non-interactive Codex should run with stdin redirected from `/dev/null`.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if not task_history_path().exists():
        task_history_path().write_text("# Task History\n\n", encoding="utf-8")
    if not file_index_path().exists():
        file_index_path().write_text("# File Index\n\nNot generated yet.\n", encoding="utf-8")
    if not codex_worker_profile_path().exists():
        codex_worker_profile_path().write_text(
            "\n".join(
                [
                    "# Codex Worker Profile",
                    "",
                    "This file is the local canonical worker profile. The orchestrator syncs it to each remote workdir at `.codex-orchestrator/codex_worker_profile.md` before launching remote Codex.",
                    "",
                    "## Role",
                    "- Codex is the worker/executor.",
                    "- The local orchestrator is the dispatcher, collector, registry, and report authority.",
                    "- Do not write long final reports unless the task explicitly asks for a short worker handoff note.",
                    "",
                    "## Execution",
                    "- Work inside the configured remote workdir.",
                    "- Read the task book and persistent context before changing code or running experiments.",
                    "- If code changes are allowed, make the required code/config changes before starting long GPU runs.",
                    "- Run long training/evaluation commands in the foreground; the orchestrator supervises the process.",
                    "- Always honor the provided `_worker_out` output path.",
                    "",
                    "## GPU",
                    "- The orchestrator selects the GPU before launch.",
                    "- If `CUDA_VISIBLE_DEVICES` is already set, do not override it.",
                    "- Treat the visible GPU as process-local GPU 0 unless the task command requires a different local index.",
                    "- If the assigned GPU is truly unusable or OOM, write a partial result explaining the resource issue.",
                    "",
                    "## Safety",
                    "- Do not delete datasets, checkpoints, or artifacts.",
                    "- Do not run destructive git commands such as `git reset`, `git clean`, or broad checkout/revert.",
                    "- Do not install packages or use network access unless the task explicitly allows it.",
                    "- Do not use DF40/NTIRE target labels for training, threshold selection, model selection, or hard-set construction unless the task marks the result diagnostic-only.",
                    "",
                    "## Result Contract",
                    "- Always create `result.yaml`, `worker_notes.md`, `stdout.log`, and `stderr.log` in `_worker_out`.",
                    "- Produce machine-readable `metrics.json` when metrics exist.",
                    "- If code changed, write `patch.diff` when possible.",
                    "- If the task cannot start, still write `result.yaml` with `status: partial` and a clear reason.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if not workflow_profile_path().exists():
        workflow_profile_path().write_text(
            "\n".join(
                [
                    "# Workflow Profile",
                    "",
                    "This profile captures stable local working preferences that should survive task handoffs and remote execution.",
                    "",
                    "## Default Order",
                    "- Start from the local clone and existing task artifacts before using external references.",
                    "- Preserve the task book's intent; metabolize routing, contracts, and validation scaffolding instead of rewriting scientific instructions.",
                    "- For repair work, score modules first, pick the lowest-scoring bounded fix, then make one focused change.",
                    "- Run narrow tests for the changed layer before the wider governance regression.",
                    "",
                    "## Experiment Claims",
                    "- Missing cache, script, or capability is a partial result with a concrete action request, not an experiment failure.",
                    "- Do not invent cache paths, metric files, or scientific conclusions.",
                    "- If required metrics are missing, stop at partial and name `missing_artifact` or `missing_capability` precisely.",
                    "",
                    "## Scheduler Preferences",
                    "- Treat worker, line, batch, dag root, and prefix as admission filters before dispatch.",
                    "- Keep CPU and GPU lanes independent when dependencies and resource limits allow parallel progress.",
                    "- Do not launch unrelated experiment lines as backfill unless the task explicitly allows it.",
                    "",
                    "## Stop Conditions",
                    "- Stop for user judgment when a fix becomes a large design decision.",
                    "- Stop instead of guessing when the next action needs new data, credentials, or a missing artifact.",
                    "- Avoid destructive git operations and unrelated source edits.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    _repair_workflow_profile_core_rules()


def append_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def update_after_collect(task: TaskBook, result: WorkerResult, report_path: Path) -> None:
    ensure_local_memory_files()
    last_summary_path().write_text(
        "\n".join(
            [
                "# Last Summary",
                "",
                f"- Task ID: {task.task_id}",
                f"- Title: {task.title}",
                f"- Status: {result.status}",
                f"- Summary: {result.summary_short}",
                "",
                "## Changed files",
                *[f"- {item}" for item in result.changed_files],
                "",
                "## Risks",
                *[f"- {item}" for item in result.risks],
                "",
                "## Next actions",
                *[f"- {item}" for item in result.next_actions],
                "",
                f"Report: {report_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    append_lines(
        task_history_path(),
        [
            f"## {now_iso()} - {task.task_id}",
            "",
            f"- Title: {task.title}",
            f"- Status: {result.status}",
            f"- Summary: {result.summary_short}",
            f"- Report: {report_path}",
            "",
        ],
    )
    long_term_items = []
    for item in result.next_actions:
        if item:
            long_term_items.append(f"- Next action from {task.task_id}: {item}")
    for item in result.risks:
        if item:
            long_term_items.append(f"- Risk from {task.task_id}: {item}")
    if long_term_items:
        append_lines(memory_path(), [f"## Updates from {task.task_id}", "", *long_term_items, ""])


def remote_context_paths(remote_workdir: str) -> list[str]:
    base = remote_workdir.rstrip("/\\") + "/.codex-orchestrator"
    return [
        remote_workdir.rstrip("/\\") + "/AGENTS.md",
        base + "/shared_context.md",
        base + "/codex_worker_profile.md",
        base + "/workflow_profile.md",
        base + "/maintenance_policy_v1.md",
        base + "/scheduler_optimization_notes.md",
        base + "/opencoder_task_execution_guide.md",
        base + "/inbox_task_format.md",
        base + "/memory.md",
        base + "/last_summary.md",
        base + "/task_history.md",
        base + "/file_index.md",
    ]

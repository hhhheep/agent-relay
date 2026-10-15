from __future__ import annotations

from typing import Iterable

from .schema import TaskBook


def _context_section(context_files: Iterable[str]) -> str:
    files = [item for item in context_files if item]
    if not files:
        return "No persistent context files were provided."
    lines = ["Before executing the current task, read these context files if they exist:"]
    lines.extend(f"- {item}" for item in files)
    lines.append("")
    lines.append("Use them as continuity context. Do not re-scan the whole project unless the task requires it.")
    lines.append("If `codex_worker_profile.md` is present, treat it as the shared worker rulebook for this workspace.")
    return "\n".join(lines)


def build_worker_prompt(
    task: TaskBook,
    task_markdown: str,
    output_dir: str = "_worker_out",
    context_files: Iterable[str] = (),
    phase: str = "full",
) -> str:
    phase = (phase or "full").strip().lower()
    if phase == "prepare_code":
        phase_section = f"""## Execution phase

You are running phase: `prepare_code`.

This is the first action inside the same task. Your job is to prepare code, configs, scripts, and lightweight validation only.

Phase rules:

1. You may modify code/configs if the task allows code changes.
2. Do not start long GPU training, overnight evaluation, or expensive benchmark commands.
3. Do not consume a GPU slot unless a tiny import/smoke check truly requires it.
4. Run only quick sanity checks that prove the prepared code/config is runnable.
5. Write `{output_dir}/phase_state.json` with:

```json
{{"phase":"prepare_code","ready_for_execute":true,"needs_execute_run":true}}
```

6. In `result.yaml`, use `status: partial` if the task still needs the later execute phase. This is not a failure.
7. In `worker_notes.md`, clearly list the exact command(s) the execute phase should run next.
"""
    elif phase == "execute_run":
        phase_section = f"""## Execution phase

You are running phase: `execute_run`.

This is the second action inside the same task. The code/config preparation phase should already be done. Your job is to execute the experiment or evaluation and collect outputs.

Phase rules:

1. Prefer not to modify source code in this phase.
2. Do not redesign the implementation unless the prepared code cannot run; if that happens, return `status: partial` and explain the required prepare fix.
3. Run the actual experiment/evaluation commands requested by the task.
4. Write `{output_dir}/phase_state.json` with:

```json
{{"phase":"execute_run","ready_for_execute":false,"needs_execute_run":false}}
```

5. `result.yaml` should describe the final task outcome: `success`, `partial`, or `failed`.
"""
    else:
        phase_section = f"""## Execution phase

You are running phase: `full`.

Complete the authorized implementation, execution and checks in this invocation. A bounded complete CPU task does not require a separate prepare phase. Stay within the task's resource authorization; long or expensive experiments retain their approved staging and budget. This phase does not grant extra execution permission.
"""
    return f"""# Worker Execution Prompt

You are the worker for task: {task.task_id}.

You are continuing work in a configured project workspace.

{phase_section}

## Persistent context

{_context_section(context_files)}

## Your job

Read the task book below, perform the requested implementation or experiment, run the necessary commands, and write a fixed result package.

The task book may be written as natural Markdown notes that become complete section by section. Treat the front matter and explicit sections as authoritative, and infer the concrete execution plan from the current chapter content. Do not require the task book to repeat the standard safety rules or output contract; this worker prompt supplies those fixed rules.

## Hard rules

1. Do not write the final long report.
2. Do not modify files outside the current worktree.
3. Do not delete datasets, checkpoints, or artifacts.
4. Do not touch production deployment scripts unless the task explicitly requests it.
5. Always write outputs to:

```text
{output_dir}/
```

6. Always create:

```text
{output_dir}/result.yaml
{output_dir}/worker_notes.md
{output_dir}/stdout.log
{output_dir}/stderr.log
```

7. If the task fails, still write `result.yaml` with `status: failed` and explain why.
8. If code changed, write:

```bash
git diff > {output_dir}/patch.diff
```

9. If metrics are produced, write:

```text
{output_dir}/metrics.json
```

10. GPU execution is controlled by the local orchestrator. If `CUDA_VISIBLE_DEVICES` is already set, do not override it, do not set a different `CUDA_VISIBLE_DEVICES` in a command prefix, and do not choose another GPU based on your own `nvidia-smi` scan. `CUDA_VISIBLE_DEVICES` may be a GPU UUID, not a numeric CUDA index. Inside the restricted process, pass `--gpu 0` or the program's local visible-device equivalent.
11. Do not start detached/background training or evaluation processes with `nohup`, trailing `&`, `setsid`, `tmux`, `screen`, or similar tools. If a long command must run, run it in the foreground and let the orchestrator supervise it.
12. The orchestrator has already applied the current GPU sharing policy before launching you. Do not reject the run only because another user's process exists on the assigned GPU if the orchestrator assigned it and `CUDA_VISIBLE_DEVICES` is set.
13. If the assigned GPU is truly unavailable, out of memory, or lacks the required driver/runtime, return `status: partial` and explain the resource conflict instead of trying another GPU.

## Required output schema

Use this exact result.yaml structure:

```yaml
task_id: {task.task_id}
status: success | failed | partial
worker: codex
started_at: ...
finished_at: ...
summary_short: ...
changed_files: []
commands_run: []
outputs:
  worker_notes: worker_notes.md
  metrics: metrics.json
  patch: patch.diff
  stdout: stdout.log
  stderr: stderr.log
risks: []
next_actions: []
```

## Task book

{task_markdown}
"""

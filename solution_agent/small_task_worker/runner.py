"""Main entry point — orchestrates the full small-task worker loop."""
from __future__ import annotations

import json
import yaml
from pathlib import Path
from typing import Optional

from .admission import check_opencoder_admission
from .artifact_writer import write_artifacts
from .background_runner import launch_background
from .executor import FakePatchAdapter, PatchAdapter, execute
from .feedback import build_feedback
from .scope_planner import FakeScopeAdapter, ProposedScope, ScopeAdapter, plan_scope
from .spec_builder import build_execution_spec
from .task_contract import TaskContract, parse_task_md
from .worktree_manager import WorktreeError, create_worktree, remove_worktree

_MAX_ATTEMPTS = 2
_MAX_CONTEXT_CHARS = 8000


def _read_context(paths: list[str], base: Path) -> str:
    """Read relevant source files and return them as a context block."""
    parts: list[str] = []
    total = 0
    for rel in paths:
        p = base / rel
        if not p.exists() or not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = f"### {rel}\n```\n{content}\n```"
        if total + len(block) > _MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def _run_execute_stage(task: TaskContract, worktree: Path, out_dir: Path) -> dict:
    """Execute Agent path — produce execution_spec.yaml or fall back to legacy launch."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # New path: task.md has an `execution:` block with script_id → execution_port
    if task.execution.get("script_id"):
        spec_data = build_execution_spec(task, worktree)
        spec_path = out_dir / "execution_spec.yaml"
        spec_path.write_text(
            yaml.safe_dump(spec_data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        result = {
            "task_id": task.task_id,
            "status": "spec_ready",
            "execution_spec": str(spec_path),
        }
        (out_dir / "result.yaml").write_text(
            yaml.safe_dump(result, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        return result

    # Legacy path: free-form run_command → launch_background
    command = task.run_command
    if not command:
        result = {
            "task_id": task.task_id,
            "status": "escalation_required",
            "reason": "execution_stage is 'run' but no script_id or ## Run Command in task.md",
        }
        (out_dir / "result.yaml").write_text(
            yaml.safe_dump(result, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        return result

    launched = launch_background(command, out_dir=out_dir, worktree=worktree)
    launched["task_id"] = task.task_id
    return launched


def run_small_task_worker(
    task_path: Path,
    worktree: Path,
    out_dir: Path,
    scope_adapter: Optional[ScopeAdapter] = None,
    patch_adapter: Optional[PatchAdapter] = None,
    worktrees_base: Optional[Path] = None,
) -> dict:
    """
    Run one small-task worker cycle.

    For execution_stage='run' tasks: launches the run_command non-blocking
    and returns immediately with status='launched'.

    For execution_stage='prepare' tasks: calls the Work Agent loop (scope plan →
    patch generate → apply → smoke test → bounded repair).

    Args:
        task_path:      path to task.md
        worktree:       working directory where patches are applied / commands run
        out_dir:        _worker_out destination (result.yaml etc. written here)
        scope_adapter:  model adapter for scope proposal (prepare stage only)
        patch_adapter:  model adapter for patch generation (prepare stage only)

    Returns:
        result dict (same content as result.yaml)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    task: TaskContract = parse_task_md(task_path)

    admission = check_opencoder_admission(task)
    (out_dir / "opencoder_admission.json").write_text(
        json.dumps(admission.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if not admission.ok:
        scope = ProposedScope(
            risk="high",
            inspect_paths=[],
            modify_paths=[],
            commands_to_run=[],
            reason="opencoder admission rejected task before patch execution",
            escalation_required=True,
            escalation_reason="; ".join(issue.message for issue in admission.errors),
        )
        return write_artifacts(
            task,
            scope,
            exec_result=None,
            out_dir=out_dir,
            attempt_count=0,
            extra_notes="OpenCoder admission failed. See opencoder_admission.json.",
        )

    # ── Optional git worktree isolation ──────────────────────────────────────
    isolated_worktree: Optional[Path] = None
    if worktrees_base is not None and task.execution_stage == "prepare":
        repo = worktree  # the caller's worktree IS the git repo root
        isolated_worktree = create_worktree(repo, task.task_id, worktrees_base)
        worktree = isolated_worktree

    # ── Execute Agent path ────────────────────────────────────────────────────
    if task.execution_stage == "run":
        return _run_execute_stage(task, worktree, out_dir)

    # ── Work Agent path (prepare / analysis) ─────────────────────────────────
    if scope_adapter is None:
        scope_adapter = FakeScopeAdapter()
    if patch_adapter is None:
        patch_adapter = FakePatchAdapter()

    try:
        scope = plan_scope(task, scope_adapter, scope_dir=out_dir)

        if scope.escalation_required:
            return write_artifacts(
                task, scope, exec_result=None,
                out_dir=out_dir, attempt_count=0,
            )

        context_paths = list(dict.fromkeys(scope.inspect_paths + scope.modify_paths))
        context = _read_context(context_paths, worktree)

        feedback_text = ""
        feedback_used = False
        exec_result = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            patch_text = patch_adapter.generate_patch(
                task, scope, context=context, feedback=feedback_text
            )

            exec_result = execute(task, scope, patch_text, worktree, out_dir)

            if exec_result.all_commands_passed:
                break

            if attempt < _MAX_ATTEMPTS:
                packet = build_feedback(exec_result, attempt=attempt, out_dir=out_dir)
                if packet is None:
                    break
                feedback_text = packet.feedback_text
                feedback_used = True

        result = write_artifacts(
            task, scope, exec_result,
            out_dir=out_dir,
            attempt_count=attempt,
            feedback_used=feedback_used,
        )

    except Exception:
        # On any exception, clean up the isolated worktree before re-raising
        if isolated_worktree is not None:
            try:
                remove_worktree(isolated_worktree, repo=repo)
            except WorktreeError:
                pass
        raise

    # Clean up failed/escalated worktrees; keep succeeded for review
    if isolated_worktree is not None and result.get("status") not in {"succeeded"}:
        try:
            remove_worktree(isolated_worktree, repo=repo)
        except WorktreeError:
            pass

    return result

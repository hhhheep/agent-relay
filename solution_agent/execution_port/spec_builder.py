"""spec_builder.py — build execution_spec dict from a raw execution block + worktree."""
from __future__ import annotations

from pathlib import Path

import yaml


def build_spec_from_execution_block(
    execution: dict,
    task_id: str,
    task_type: str,
    worktree: Path,
) -> dict:
    """
    Build an execution_spec dict from the `execution:` front-matter block.

    Callers (small_task_worker, remote_materializer, CLI) pass the raw dict
    extracted from task.md's front matter.  They are responsible for writing
    the result to disk and calling register_execution().

    Expected block shape:
        script_id: run_eval_probe
        args:
          dataset: deepfake_v2
          output_dir: results/eval
        timeout_sec: 3600
        gpu_required: false
        gpu_count: 0
        required_artifacts:
          - results/eval/metrics.json
        success_criteria:
          exit_code: 0
          metrics_file: results/eval/metrics.json
          metric_keys: [auc, loss]
    """
    sc = dict(execution.get("success_criteria") or {})
    return {
        "task_id": task_id,
        "job_type": task_type,
        "workspace": {
            "root": str(worktree),
            "cwd": str(worktree),
            "artifact_roots": [str(worktree), *[str(item) for item in execution.get("allowed_artifact_roots", []) or []]],
        },
        "command": {
            "script_id": str(execution.get("script_id", "")),
            "args": dict(execution.get("args") or {}),
            "timeout_sec": int(execution.get("timeout_sec", 0)),
        },
        "resources": {
            "gpu_required": bool(execution.get("gpu_required", False)),
            "gpu_count": int(execution.get("gpu_count", 0)),
        },
        "artifacts": {
            "required": list(execution.get("required_artifacts") or []),
        },
        "success_criteria": {
            "exit_code": int(sc.get("exit_code", 0)),
            "metrics_file": str(sc.get("metrics_file", "")),
            "metric_keys": list(sc.get("metric_keys") or []),
        },
        "evidence": {
            "token_guard_receipt_required": bool(
                (execution.get("evidence") or {}).get("token_guard_receipt_required", False)
            ),
            "token_guard_receipt": str(
                (execution.get("evidence") or {}).get("token_guard_receipt", "")
            ),
        },
    }


def write_execution_spec(
    execution: dict,
    task_id: str,
    task_type: str,
    worktree: Path,
    dest: Path,
) -> Path:
    """Build and write execution_spec.yaml to dest. Returns dest."""
    spec = build_spec_from_execution_block(execution, task_id, task_type, worktree)
    dest.write_text(
        yaml.safe_dump(spec, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return dest

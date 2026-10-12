"""review_packet.py — produce a compact review packet from a failed execution job."""
from __future__ import annotations

from pathlib import Path

import yaml


_TAIL_LINES = 100


def _tail(path: Path, n: int = _TAIL_LINES) -> str:
    if not path.exists():
        return "(file not found)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]) if lines else "(empty)"


def build_review_packet(job_dir: Path, dest: Path | None = None) -> Path:
    """
    Assemble a review_packet/ directory from a finished job_dir.

    Contents:
        execution_spec.yaml      — original spec (re-runnable)
        final_result.yaml        — authoritative outcome
        collect_report.yaml      — full collection detail
        state.json               — lifecycle state
        stdout_tail.txt          — last 100 lines of stdout
        stderr_tail.txt          — last 100 lines of stderr
        patch.diff               — if present in job_dir

    Returns the path to the review_packet/ directory.
    """
    dest = dest or job_dir / "review_packet"
    dest.mkdir(parents=True, exist_ok=True)

    # ── Copy structured files ─────────────────────────────────────────────────
    for name in ("execution_spec.yaml", "final_result.yaml",
                 "collect_report.yaml", "state.json"):
        src = job_dir / name
        if src.exists():
            (dest / name).write_bytes(src.read_bytes())

    # ── Tail logs ─────────────────────────────────────────────────────────────
    (dest / "stdout_tail.txt").write_text(
        _tail(job_dir / "stdout.log"), encoding="utf-8"
    )
    (dest / "stderr_tail.txt").write_text(
        _tail(job_dir / "stderr.log"), encoding="utf-8"
    )

    # ── patch.diff if present ─────────────────────────────────────────────────
    patch = job_dir / "patch.diff"
    if patch.exists():
        (dest / "patch.diff").write_bytes(patch.read_bytes())

    # ── Summary index for quick reading ──────────────────────────────────────
    final: dict = {}
    final_path = job_dir / "final_result.yaml"
    if final_path.exists():
        final = yaml.safe_load(final_path.read_text(encoding="utf-8")) or {}

    summary = {
        "job_id": final.get("job_id"),
        "task_id": final.get("task_id"),
        "status": final.get("status"),
        "exit_code": final.get("exit_code"),
        "artifacts_missing": final.get("artifacts_missing") or [],
        "metrics_missing": final.get("metrics_missing") or [],
        "started_at": final.get("started_at"),
        "finished_at": final.get("finished_at"),
        "collected_at": final.get("collected_at"),
        "files": sorted(str(p.name) for p in dest.iterdir()),
    }
    (dest / "review_summary.yaml").write_text(
        yaml.safe_dump(summary, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    return dest

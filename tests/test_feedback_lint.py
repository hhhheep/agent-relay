from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "solution-agent-harness"
    / "skills"
    / "solution-agent-harness"
    / "scripts"
    / "lint_feedback.py"
)
SPEC = importlib.util.spec_from_file_location("feedback_lint", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_canonical_feedback_passes(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.md"
    feedback.write_text(
        "# Feedback\n\n"
        "## Open issues\n\n"
        "- HF-002 - remote route unavailable\n\n"
        "## Issues\n\n"
        "- [HF-001][environment][resolved][2026-09-27] route missing - Evidence: no profile; Impact: blocked; Next: add profile.\n"
        "  - [HF-001-C001][resolved][2026-09-28][reply-to:HF-001] Added profile; wrapper preflight passed.\n"
        "- [HF-002][environment][open][2026-09-28] remote route unavailable - Evidence: timeout; Impact: no lifecycle; Next: verify host.\n",
        encoding="utf-8",
    )
    result = MODULE.lint(feedback)
    assert result["ok"] is True
    assert result["open_ids"] == ["HF-002"]


def test_partial_resolution_and_index_drift_fail(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.md"
    feedback.write_text(
        "# Feedback\n\n"
        "## Open issues\n\n"
        "- HF-001 - stale index\n\n"
        "## Issues\n\n"
        "- [HF-001][environment][resolved][2026-09-27] route missing - Evidence: no profile; Impact: blocked; Next: add profile.\n"
        "  - [HF-001-C001][resolved][2026-09-28][reply-to:HF-001] Source fixed but assets remain pending.\n",
        encoding="utf-8",
    )
    result = MODULE.lint(feedback)
    assert result["ok"] is False
    assert any("open index mismatch" in error for error in result["errors"])
    assert any("still describes open work" in error for error in result["errors"])


def test_reply_to_reply_and_reopen_round_trip_passes(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.md"
    feedback.write_text(
        "# Feedback\n\n"
        "## Open issues\n\n"
        "## Issues\n\n"
        "- [HF-001][defect][resolved][2026-09-27] lifecycle failed - Evidence: error; Impact: blocked; Next: repair.\n"
        "  - [HF-001-C001][resolved][2026-09-27][reply-to:HF-001] First repair passed.\n"
        "  - [HF-001-C002][reply][2026-09-28][reply-to:HF-001-C001] A later task reproduced a related failure.\n"
        "  - [HF-001-C003][reopened][2026-09-28][reply-to:HF-001-C002] New evidence restores the impact; inspect the new trace.\n"
        "  - [HF-001-C004][progress][2026-09-28][reply-to:HF-001-C003] Corrected the transport binding.\n"
        "  - [HF-001-C005][resolved][2026-09-28][reply-to:HF-001-C004] A fresh task verified the repair.\n",
        encoding="utf-8",
    )
    result = MODULE.lint(feedback)
    assert result["ok"] is True
    assert result["comment_count"] == 5
    assert result["reopened_count"] == 1


def test_reply_parent_and_derived_state_are_validated(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.md"
    feedback.write_text(
        "# Feedback\n\n"
        "## Open issues\n\n"
        "- HF-001 - lifecycle failed\n\n"
        "## Issues\n\n"
        "- [HF-001][defect][open][2026-09-27] lifecycle failed - Evidence: error; Impact: blocked; Next: repair.\n"
        "  - [HF-001-C001][resolved][2026-09-28][reply-to:HF-001-C999] Repair passed.\n",
        encoding="utf-8",
    )
    result = MODULE.lint(feedback)
    assert result["ok"] is False
    assert any("is not an earlier item" in error for error in result["errors"])
    assert any("does not match comment-derived state" in error for error in result["errors"])

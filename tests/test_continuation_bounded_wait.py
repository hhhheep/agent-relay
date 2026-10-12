from __future__ import annotations

import json
from pathlib import Path

from solution_agent.continuation.bounded_wait import WaitKind, run_bounded_wait, write_wait_decision_report


def test_bounded_wait_file_exists_ready_local_only(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"ok": true}\n', encoding="utf-8")

    report = run_bounded_wait(
        wait_kind=WaitKind.FILE_EXISTS,
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        reason="wait for local artifact",
        target_path=target,
    )

    assert report["status"] == "ready"
    assert report["wait_kind"] == "file_exists"
    assert report["observed_status"] == "target_exists"
    assert str(target) in report["partial_artifacts"]
    assert report["side_effects"]["dispatch"] is False
    assert report["side_effects"]["ssh"] is False
    assert report["side_effects"]["registry_mutation"] is False


def test_bounded_wait_rejects_missing_target() -> None:
    report = run_bounded_wait(
        wait_kind=WaitKind.FILE_EXISTS,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
        reason="invalid request missing target",
        target_path=None,
    )

    assert report["status"] == "rejected"
    assert report["observed_status"] == "invalid_request"
    assert "missing_wait_target" in report["reason_codes"]


def test_bounded_wait_rejects_unbounded_wait() -> None:
    report = run_bounded_wait(
        wait_kind=WaitKind.RESULT_ARTIFACT,
        timeout_seconds=0.0,
        poll_interval_seconds=0.1,
        reason="unbounded timeout",
        target_path="some/path",
    )

    assert report["status"] == "rejected"
    assert "invalid_timeout_seconds" in report["reason_codes"]


def test_bounded_wait_timeout_collects_partial_evidence(tmp_path: Path) -> None:
    target = tmp_path / "changing.log"
    target.write_text("first\n", encoding="utf-8")

    report = run_bounded_wait(
        wait_kind=WaitKind.STABLE_FILE_SIZE,
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        reason="wait until stable size",
        target_path=target,
        stable_observations_required=5,
    )

    assert report["status"] == "timeout"
    assert report["attempts"] == 2
    assert report["observed_status"] in {"file_size_changing", "target_missing"}
    assert str(target) in report["partial_artifacts"]
    assert report["next_recommendation"] == "collect_partial_evidence_then_continue_repair_or_hold"


def test_write_wait_decision_report_writes_artifact(tmp_path: Path) -> None:
    target = tmp_path / "ready.txt"
    target.write_text("ok\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    written = write_wait_decision_report(
        output_dir=output_dir,
        wait_kind=WaitKind.FILE_EXISTS,
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        reason="artifact readiness",
        target_path=target,
    )

    assert written["status"] == "written"
    path = Path(written["report_path"])
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "wait_decision_report"

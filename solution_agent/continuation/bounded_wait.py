from __future__ import annotations

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from .schema import StrictModel
from ..utils.process_utils import process_is_alive


class WaitKind(str, Enum):
    FILE_EXISTS = "file_exists"
    RESULT_ARTIFACT = "result_artifact"
    PROCESS_EXIT = "process_exit"
    STABLE_FILE_SIZE = "stable_file_size"


class WaitDecisionReport(StrictModel):
    schema_version: int = 1
    artifact_type: str = "wait_decision_report"
    status: str
    wait_kind: WaitKind
    waited_for: str
    timeout_seconds: float
    poll_interval_seconds: float
    attempts: int
    max_attempts: int
    observed_status: str
    partial_artifacts: list[str] = Field(default_factory=list)
    next_recommendation: str
    reason_codes: list[str] = Field(default_factory=list)
    side_effects: dict[str, bool] = Field(
        default_factory=lambda: {
            "dispatch": False,
            "ssh": False,
            "remote_execution": False,
            "registry_mutation": False,
            "active_inbox_write": False,
        }
    )


def _check_process_exited(pid: int) -> bool:
    return pid > 0 and not process_is_alive(pid)


def run_bounded_wait(
    *,
    wait_kind: WaitKind | str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    reason: str,
    target_path: Path | str | None = None,
    expected_artifact: str | None = None,
    process_pid: int | None = None,
    stable_observations_required: int = 2,
) -> dict[str, Any]:
    kind = WaitKind(wait_kind)
    path = Path(target_path) if target_path is not None else None
    normalized_reason = str(reason or "").strip()
    reason_codes: list[str] = []

    if timeout_seconds <= 0:
        reason_codes.append("invalid_timeout_seconds")
    if poll_interval_seconds <= 0:
        reason_codes.append("invalid_poll_interval_seconds")
    if not normalized_reason:
        reason_codes.append("missing_reason")

    if kind in {WaitKind.FILE_EXISTS, WaitKind.RESULT_ARTIFACT, WaitKind.STABLE_FILE_SIZE} and path is None:
        reason_codes.append("missing_wait_target")
    if kind == WaitKind.PROCESS_EXIT and process_pid is None:
        reason_codes.append("missing_wait_target")
    if kind == WaitKind.STABLE_FILE_SIZE and stable_observations_required < 2:
        reason_codes.append("invalid_stable_observations_required")

    if reason_codes:
        report = WaitDecisionReport(
            status="rejected",
            wait_kind=kind,
            waited_for=str(path or process_pid or ""),
            timeout_seconds=max(timeout_seconds, 0),
            poll_interval_seconds=max(poll_interval_seconds, 0),
            attempts=0,
            max_attempts=0,
            observed_status="invalid_request",
            next_recommendation="provide_bounded_wait_target_and_timeout",
            reason_codes=sorted(set(reason_codes)),
        )
        return report.model_dump(mode="json")

    max_attempts = max(1, int(timeout_seconds / poll_interval_seconds))
    attempts = 0
    partial_artifacts: list[str] = []
    last_size: int | None = None
    stable_count = 0
    observed_status = "waiting"

    while attempts < max_attempts:
        attempts += 1
        if kind in {WaitKind.FILE_EXISTS, WaitKind.RESULT_ARTIFACT}:
            if path.exists():
                observed_status = "target_exists"
                partial_artifacts.append(str(path))
                report = WaitDecisionReport(
                    status="ready",
                    wait_kind=kind,
                    waited_for=str(path),
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    observed_status=observed_status,
                    partial_artifacts=sorted(set(partial_artifacts)),
                    next_recommendation="continue_with_local_validation_or_next_gate",
                )
                return report.model_dump(mode="json")
            observed_status = "target_missing"
        elif kind == WaitKind.PROCESS_EXIT:
            if _check_process_exited(int(process_pid)):
                observed_status = "process_exited"
                report = WaitDecisionReport(
                    status="ready",
                    wait_kind=kind,
                    waited_for=str(process_pid),
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    observed_status=observed_status,
                    next_recommendation="collect_process_artifacts_and_decide_next_action",
                )
                return report.model_dump(mode="json")
            observed_status = "process_running"
        else:
            if path.exists():
                partial_artifacts.append(str(path))
                size = path.stat().st_size
                if last_size is None or size != last_size:
                    stable_count = 1
                else:
                    stable_count += 1
                last_size = size
                if stable_count >= stable_observations_required:
                    observed_status = "file_size_stable"
                    report = WaitDecisionReport(
                        status="ready",
                        wait_kind=kind,
                        waited_for=str(path),
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        attempts=attempts,
                        max_attempts=max_attempts,
                        observed_status=observed_status,
                        partial_artifacts=sorted(set(partial_artifacts)),
                        next_recommendation="continue_with_local_validation_or_next_gate",
                    )
                    return report.model_dump(mode="json")
                observed_status = "file_size_changing"
            else:
                observed_status = "target_missing"

        if attempts < max_attempts:
            time.sleep(poll_interval_seconds)

    timed_out_recommendation = (
        "collect_partial_evidence_then_continue_repair_or_hold"
        if partial_artifacts
        else "collect_timeout_evidence_then_run_inventory_or_hold"
    )
    report = WaitDecisionReport(
        status="timeout",
        wait_kind=kind,
        waited_for=str(path or process_pid or ""),
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        attempts=attempts,
        max_attempts=max_attempts,
        observed_status=observed_status,
        partial_artifacts=sorted(set(partial_artifacts)),
        next_recommendation=timed_out_recommendation,
        reason_codes=["bounded_wait_timeout"],
    )
    return report.model_dump(mode="json")


def write_wait_decision_report(
    *,
    output_dir: Path | str,
    wait_kind: WaitKind | str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    reason: str,
    target_path: Path | str | None = None,
    expected_artifact: str | None = None,
    process_pid: int | None = None,
    stable_observations_required: int = 2,
) -> dict[str, Any]:
    report = run_bounded_wait(
        wait_kind=wait_kind,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        reason=reason,
        target_path=target_path,
        expected_artifact=expected_artifact,
        process_pid=process_pid,
        stable_observations_required=stable_observations_required,
    )
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "wait_decision_report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"status": "written", "report_path": str(path), "report": report}


__all__ = [
    "WaitDecisionReport",
    "WaitKind",
    "run_bounded_wait",
    "write_wait_decision_report",
]

from __future__ import annotations

"""Orchestrator-layer bounded feedback retry for remote_materializer.

Architecture
------------
RetryController owns the attempt loop. The materializer itself is called in
single-attempt mode on each outer iteration (feedback_text="" on attempt 1,
non-empty on attempt 2+). This keeps the materializer as a stateless
generate-once unit while giving the orchestrator full visibility into:

  - how many attempts were made
  - what feedback was sent
  - which attempt produced the final result
  - per-attempt artifact directories

Not responsible for:
  - Editing source files automatically
  - Running training or evaluation commands
  - Mutating the task registry
  - Auto-approving escalation requests
"""

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters import run_remote_materializer
from .schema import now_iso
from ..harness.failure_fingerprint import failure_fingerprint

_MAX_FEEDBACK_ERRORS = 10
_RETRY_VALIDATOR_VERSION = "remote_materializer_retry/v2"


def _retry_failure_fingerprint(task_id: str, status: str, validation_errors: List[str]) -> str:
    """Identify a materially unchanged failure without hashing volatile attempt text."""

    stable_errors = sorted({error.strip() for error in validation_errors if error and error.strip()})
    command_sha256 = hashlib.sha256(b"solution-agent:remote-materializer").hexdigest()
    input_sha256 = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return failure_fingerprint(
        error_type=status,
        command_sha256=command_sha256,
        declared_input_fingerprint=input_sha256,
        validator_version=_RETRY_VALIDATOR_VERSION,
        stable_details={"validation_errors": stable_errors},
    )


@dataclass
class RetryResult:
    mat_result: Dict[str, Any]
    final_attempt_dir: Path
    attempts: int
    feedback_used: bool
    raw_exc_info: Optional[str] = None  # last unscrubbable exception message; caller must scrub before logging


def _build_feedback_text(status: str, validation_errors: List[str], attempt: int) -> str:
    """Build a compact feedback packet for the next materializer attempt.

    Returns an empty string when there are no actionable errors (caller should
    not retry if this returns "").
    """
    errors = [e for e in validation_errors if e and e.strip()][:_MAX_FEEDBACK_ERRORS]
    if not errors:
        return ""
    lines = [
        f"# Feedback from attempt {attempt}",
        "",
        f"Previous attempt status: {status}",
        "Fix ONLY the issues listed below.",
        "Do not modify source code, training scripts, dataset splits, or checkpoints.",
        "",
        "## Validation errors",
        "",
    ]
    for err in errors:
        lines.append(f"- {err}")
    lines.extend([
        "",
        "## Required action",
        "",
        "Regenerate all required_outputs correcting the errors above.",
        "Write result.yaml with: status, summary_short, generated_files,",
        "validation_summary, and escalation_required fields.",
    ])
    return "\n".join(lines)


def _exception_to_mat_result(task_id: str, exc: Exception) -> Dict[str, Any]:
    exc_type = type(exc).__name__
    return {
        "task_id": task_id,
        "status": "escalation_required",
        "summary_short": f"remote_materializer raised {exc_type} before producing result",
        "escalation_reason": f"provider_or_api_exception: {exc_type}",
        "validation_summary": {"status": "fail", "failed_validators": []},
        "validation_errors": [f"{exc_type}: {exc}"],
        "generated_files": [],
        "retry_count": {"reader": 0, "qwen": 0},
        "created_at": now_iso(),
    }


class RetryController:
    """Run remote_materializer with up to max_attempts, injecting feedback on retry.

    Each attempt writes to its own subdirectory:
        mat_out/attempts/attempt_001/<task_id>/
        mat_out/attempts/attempt_002/<task_id>/

    The final attempt's directory is returned in RetryResult.final_attempt_dir
    so that write_materializer_worker_out() can read artifacts from there.
    """

    def __init__(
        self,
        task_id: str,
        task_path: Path,
        mat_out: Path,
        policy_path: Path,
        env_file: Path,
        max_attempts: int = 2,
        workspace_root: Path | None = None,
        stop_repeated_failure: bool = False,
    ) -> None:
        self.task_id = task_id
        self.task_path = task_path
        self.mat_out = mat_out
        self.policy_path = policy_path
        self.env_file = env_file
        self.max_attempts = max(1, max_attempts)
        self.workspace_root = workspace_root
        self.stop_repeated_failure = stop_repeated_failure

    def run_attempts(self) -> RetryResult:
        feedback_text = ""
        last_mat_result: Dict[str, Any] = {}
        attempt_statuses: List[str] = []
        last_attempt_dir = self.mat_out / "attempts" / "attempt_001" / self.task_id
        last_raw_exc: Optional[str] = None
        failure_fingerprints: List[str] = []
        halted_reason: Optional[str] = None

        for attempt in range(1, self.max_attempts + 1):
            attempt_out = self.mat_out / "attempts" / f"attempt_{attempt:03d}"
            last_attempt_dir = attempt_out / self.task_id

            try:
                kwargs: Dict[str, Any] = {
                    "task_path": self.task_path,
                    "output_root": attempt_out,
                    "env_file": self.env_file,
                    "policy_path": self.policy_path,
                    "feedback_text": feedback_text,
                }
                if self.workspace_root is not None:
                    kwargs["workspace_root"] = self.workspace_root
                mat_result = run_remote_materializer(**kwargs)
                last_raw_exc = None
            except Exception as exc:
                last_raw_exc = f"{type(exc).__name__}: {exc}"
                mat_result = _exception_to_mat_result(self.task_id, exc)

            last_mat_result = mat_result
            status = str(mat_result.get("status") or "escalation_required")
            attempt_statuses.append(status)

            if status == "materialized":
                break

            errors = list(mat_result.get("validation_errors") or [])
            fingerprint = _retry_failure_fingerprint(self.task_id, status, errors)
            failure_fingerprints.append(fingerprint)
            if (
                self.stop_repeated_failure
                and len(failure_fingerprints) >= 2
                and failure_fingerprints[-1] == failure_fingerprints[-2]
            ):
                # One corrective retry was already attempted. Repeating the same
                # validator outcome with the same declared task is no progress.
                halted_reason = "repeated_failure_fingerprint"
                break

            if attempt < self.max_attempts:
                feedback_text = _build_feedback_text(status, errors, attempt)
                if not feedback_text:
                    # No actionable errors; additional attempts cannot improve outcome.
                    break

        feedback_used = len(attempt_statuses) > 1 and bool(feedback_text)
        last_mat_result.setdefault("retry_metadata", {
            "total_attempts": len(attempt_statuses),
            "feedback_used": feedback_used,
            "attempt_statuses": attempt_statuses,
            "failure_fingerprints": failure_fingerprints,
            "halted_reason": halted_reason,
        })

        return RetryResult(
            mat_result=last_mat_result,
            final_attempt_dir=last_attempt_dir,
            attempts=len(attempt_statuses),
            feedback_used=feedback_used,
            raw_exc_info=last_raw_exc,
        )

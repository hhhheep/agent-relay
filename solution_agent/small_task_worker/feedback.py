"""Build a compact feedback packet from failed execution results."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .executor import ExecutionResult

_MAX_STDERR_CHARS = 3000
_MAX_ERRORS = 10


@dataclass
class FeedbackPacket:
    attempt: int
    patch_error: str
    failed_command: str
    stderr_excerpt: str
    errors: List[str]
    feedback_text: str

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "patch_error": self.patch_error,
            "failed_command": self.failed_command,
            "stderr_excerpt": self.stderr_excerpt,
            "errors": self.errors,
            "feedback_text": self.feedback_text,
        }


def _extract_errors(stderr: str) -> List[str]:
    errors = []
    for line in stderr.splitlines():
        lower = line.lower()
        if any(k in lower for k in ("error", "exception", "failed", "traceback", "assert")):
            errors.append(line.strip())
        if len(errors) >= _MAX_ERRORS:
            break
    return errors


def build_feedback(
    exec_result: ExecutionResult,
    attempt: int,
    out_dir: Optional[Path] = None,
) -> Optional[FeedbackPacket]:
    """Return None if there is nothing actionable to feed back."""
    if exec_result.all_commands_passed:
        return None

    patch_error = exec_result.patch_error
    failed_cmd = ""
    stderr_raw = ""

    failure = exec_result.first_failure
    if failure:
        failed_cmd = failure.command
        stderr_raw = failure.stderr

    errors = _extract_errors(patch_error + "\n" + stderr_raw)
    stderr_excerpt = (patch_error + "\n" + stderr_raw).strip()
    if len(stderr_excerpt) > _MAX_STDERR_CHARS:
        stderr_excerpt = stderr_excerpt[:_MAX_STDERR_CHARS] + "\n[truncated]"

    if not errors and not stderr_excerpt.strip():
        return None

    lines = [
        f"## Repair Feedback — attempt {attempt}",
        "",
        "The previous attempt failed. Produce a corrected unified diff only.",
        "Do not expand scope beyond the original allowed_modify paths.",
        "Do not modify source code outside the proposed scope.",
        "Do not run training. Do not change dataset splits or checkpoints.",
        "",
    ]
    if patch_error:
        lines += ["### Patch Error", patch_error, ""]
    if failed_cmd:
        lines += [f"### Failed Command", f"```", failed_cmd, "```", ""]
    if errors:
        lines += ["### Errors (up to 10)"]
        lines += [f"- {e}" for e in errors]
        lines += [""]
    if stderr_excerpt:
        lines += ["### stderr excerpt", "```", stderr_excerpt, "```"]

    feedback_text = "\n".join(lines)

    packet = FeedbackPacket(
        attempt=attempt,
        patch_error=patch_error,
        failed_command=failed_cmd,
        stderr_excerpt=stderr_excerpt,
        errors=errors,
        feedback_text=feedback_text,
    )

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "feedback_packet.json").write_text(
            json.dumps(packet.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return packet

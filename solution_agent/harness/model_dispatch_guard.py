from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from solution_agent.execution_port.io_utils import atomic_write_json

from .budget import evaluate_structural_budget
from .telemetry_contracts import PhaseBudget, StructuralUsage

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_PROCESS_LOCK = threading.Lock()
_SCHEMA_VERSION = "harness-model-dispatch-state/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def model_request_fingerprint(
    *,
    provider: str,
    model: str,
    phase: str,
    payload_sha256: str,
    context_manifest_sha256: str | None = None,
) -> str:
    """Return a stable identity without persisting prompt or response bodies."""

    return _canonical_sha256(
        {
            "provider": provider,
            "model": model,
            "phase": phase,
            "payload_sha256": payload_sha256,
            "context_manifest_sha256": context_manifest_sha256,
        }
    )


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": run_id,
        "requests": [],
        "updated_at": _now_iso(),
    }


def _load_state(path: Path, run_id: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_state(run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported model-dispatch state schema")
    if payload.get("run_id") != run_id:
        raise ValueError("model-dispatch state run_id mismatch")
    if not isinstance(payload.get("requests"), list):
        raise ValueError("model-dispatch state requests must be a list")
    return payload


class ModelDispatchDenied(RuntimeError):
    def __init__(self, reason: str, decision: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


@dataclass(frozen=True)
class ModelDispatchPermit:
    run_id: str
    request_id: str
    fingerprint: str
    state_path: Path
    context_estimated_tokens: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "state_path": str(self.state_path),
            "context_estimated_tokens": self.context_estimated_tokens,
        }


def admit_model_dispatch(
    run_dir: Path,
    *,
    run_id: str,
    provider: str,
    model: str,
    phase: str,
    fingerprint: str,
    budget: PhaseBudget,
    context_estimated_tokens: int | None = None,
    corrective: bool = False,
    failure_fingerprint: str | None = None,
) -> ModelDispatchPermit:
    """Atomically reserve one model request or fail before provider invocation."""

    state_path = Path(run_dir) / "control" / "model_dispatch_state.json"
    with _state_lock(state_path):
        state = _load_state(state_path, run_id)
        requests = [item for item in state["requests"] if isinstance(item, dict)]
        prior_same = next((item for item in requests if item.get("fingerprint") == fingerprint), None)
        if prior_same is not None:
            raise ModelDispatchDenied(
                "duplicate_model_request",
                {
                    "allowed": False,
                    "violations": ["duplicate_model_request_fingerprint"],
                    "prior_request_id": prior_same.get("request_id"),
                    "fingerprint": fingerprint,
                },
            )
        if failure_fingerprint:
            prior_failure = next(
                (item for item in requests if item.get("failure_fingerprint") == failure_fingerprint),
                None,
            )
            if prior_failure is not None:
                raise ModelDispatchDenied(
                    "repeated_failure_fingerprint",
                    {
                        "allowed": False,
                        "violations": ["repeated_failure_fingerprint"],
                        "prior_request_id": prior_failure.get("request_id"),
                        "failure_fingerprint": failure_fingerprint,
                    },
                )

        usage = StructuralUsage(
            model_requests=len(requests),
            corrective_model_requests=sum(bool(item.get("corrective")) for item in requests),
        )
        decision = evaluate_structural_budget(
            usage,
            budget,
            proposed_model_requests=1,
            proposed_corrective_model_requests=1 if corrective else 0,
            next_context_estimated_tokens=context_estimated_tokens,
        )
        if not decision.allowed:
            raise ModelDispatchDenied("model_dispatch_budget_exceeded", decision.model_dump(mode="json"))

        request_id = str(uuid4())
        requests.append(
            {
                "request_id": request_id,
                "fingerprint": fingerprint,
                "provider": provider,
                "model": model,
                "phase": phase,
                "corrective": corrective,
                "failure_fingerprint": failure_fingerprint,
                "context_estimated_tokens": context_estimated_tokens,
                "status": "admitted",
                "started_at": _now_iso(),
            }
        )
        state["requests"] = requests
        state["updated_at"] = _now_iso()
        state["last_budget_decision"] = decision.model_dump(mode="json")
        atomic_write_json(state_path, state)

    return ModelDispatchPermit(
        run_id=run_id,
        request_id=request_id,
        fingerprint=fingerprint,
        state_path=state_path,
        context_estimated_tokens=context_estimated_tokens,
    )


def complete_model_dispatch(
    permit: ModelDispatchPermit,
    *,
    status: str,
    returncode: int | None = None,
) -> None:
    """Commit terminal request metadata without storing generated content."""

    if status not in {"succeeded", "failed", "unknown"}:
        raise ValueError("unsupported model-dispatch completion status")
    with _state_lock(permit.state_path):
        state = _load_state(permit.state_path, permit.run_id)
        for item in state["requests"]:
            if isinstance(item, dict) and item.get("request_id") == permit.request_id:
                item["status"] = status
                item["returncode"] = returncode
                item["finished_at"] = _now_iso()
                break
        else:
            raise ValueError("model-dispatch permit is missing from durable state")
        state["updated_at"] = _now_iso()
        atomic_write_json(permit.state_path, state)


__all__ = [
    "ModelDispatchDenied",
    "ModelDispatchPermit",
    "admit_model_dispatch",
    "complete_model_dispatch",
    "model_request_fingerprint",
]

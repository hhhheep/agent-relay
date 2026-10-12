from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from solution_agent.execution_port.io_utils import atomic_write_json
from solution_agent.utils.process_utils import process_is_alive
from solution_agent.utils.subprocess_utils import hidden_subprocess_kwargs

from .wait_contracts import (
    WaitAcknowledgement,
    WaitDisposition,
    WaitEvent,
    WaitRunState,
    utc_now,
)


_PRE_COLLECT_TERMINAL = {"exited", "timeout", "killed", "lost_process", "failed_launch"}
_FINAL_SUCCESS = {"succeeded"}
_FINAL_INCOMPLETE = {
    "failed_exit_code",
    "artifact_missing",
    "metrics_missing",
    "metrics_invalid",
    "missing_evidence",
    "missing_capability",
    "invalid_model_execution_evidence",
}
_MATERIAL_KINDS = {
    "COMPLETION_CANDIDATE",
    "EXECUTION_FAILURE_VERIFIED",
    "STALLED",
    "DECISION_REQUIRED",
    "BUDGET_EXCEEDED",
    "COLLECTION_VERIFIED",
    "COLLECTION_REJECTED",
}


def _wait_dir(job_dir: Path) -> Path:
    return job_dir / "wait"


def _state_path(job_dir: Path) -> Path:
    return _wait_dir(job_dir) / "state.json"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _risk_route(state: WaitRunState, event: WaitEvent | None = None) -> dict[str, str]:
    if state.lifecycle_state != "REVIEW_REQUIRED":
        return {"route": "no_model", "reason": "deterministic_lifecycle_transition"}
    payload = event.payload if event is not None else {}
    decision_type = str(payload.get("decision_type") or "").strip().lower()
    risk = str(payload.get("risk") or "").strip().lower()
    if decision_type in {"statistics", "scientific_interpretation", "repair_decision"} or risk == "high":
        return {"route": "sol", "reason": "high_risk_or_scientific_review"}
    if decision_type in {"prose_normalization", "low_risk_triage"} and risk in {"", "low"}:
        return {"route": "luna", "reason": "bounded_low_risk_triage"}
    return {"route": "human", "reason": "review_required_without_declared_model_route"}


def _write_compact_views(job_dir: Path, state: WaitRunState, event: WaitEvent | None = None) -> None:
    route = _risk_route(state, event)
    disposition = wait_disposition(job_dir, state)
    receipt = {
        "schema_version": "harness-wait-receipt/v1",
        "run_id": state.run_id,
        "attempt_id": state.attempt_id,
        "spec_id": state.spec_id,
        "spec_version": state.spec_version,
        "spec_sha256": state.spec_sha256,
        "state_version": state.state_version,
        "lifecycle_state": state.lifecycle_state,
        "execution_status": state.execution_status,
        "observation_status": state.observation_status,
        "evidence_status": state.evidence_status,
        "scientific_status": state.scientific_status,
        "last_event_id": event.event_id if event is not None else None,
        "last_event_kind": event.kind if event is not None else None,
        "counters": {
            "execution_submissions": state.execution_submission_count,
            "execution_retries": state.execution_retry_count,
            "observation_retries": state.observation_retry_count,
            "collect_transitions": state.collect_transition_count,
            "model_dispatches": state.model_dispatch_count,
        },
        "route": route,
        "wait_disposition": disposition,
        "canonical_state_path": str(_state_path(job_dir)),
        "updated_at": state.updated_at.isoformat(),
    }
    atomic_write_json(_wait_dir(job_dir) / "receipt.json", receipt)
    handoff = "\n".join(
        [
            "# Harness wait handoff",
            "",
            f"- Run: `{state.run_id}` / `{state.attempt_id}`",
            f"- Lifecycle: `{state.lifecycle_state}`",
            f"- Execution: `{state.execution_status}`",
            f"- Observation: `{state.observation_status}`",
            f"- Evidence: `{state.evidence_status}`",
            f"- Scientific: `{state.scientific_status}`",
            f"- Route: `{route['route']}` — {route['reason']}",
            f"- Next action: `{disposition['next_model_action']}`",
            f"- Canonical state: `{_state_path(job_dir)}`",
            "",
        ]
    )
    temp = _wait_dir(job_dir) / "HANDOFF.md.tmp"
    temp.write_text(handoff, encoding="utf-8")
    temp.replace(_wait_dir(job_dir) / "HANDOFF.md")


@contextmanager
def _transaction_lock(job_dir: Path, timeout_seconds: float = 5.0) -> Iterator[None]:
    wait_dir = _wait_dir(job_dir)
    wait_dir.mkdir(parents=True, exist_ok=True)
    lock_path = wait_dir / ".transaction.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"wait-state transaction lock is busy: {lock_path}")
            time.sleep(0.02)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def load_wait_state(job_dir: Path) -> WaitRunState:
    return WaitRunState.model_validate_json(_state_path(job_dir).read_text(encoding="utf-8"))


def initialize_wait(
    job_dir: Path,
    execution_state: dict[str, Any],
    *,
    stall_after_seconds: float | None = None,
) -> WaitRunState:
    with _transaction_lock(job_dir):
        state_path = _state_path(job_dir)
        if state_path.is_file():
            return WaitRunState.model_validate_json(state_path.read_text(encoding="utf-8"))
        wait_dir = _wait_dir(job_dir)
        (wait_dir / "events").mkdir(parents=True, exist_ok=True)
        (wait_dir / "acks").mkdir(parents=True, exist_ok=True)
        run_id = str(execution_state.get("job_id") or job_dir.name)
        task_id = str(execution_state.get("task_id") or run_id)
        spec_sha256 = str(execution_state.get("source_spec_sha256") or "")
        if len(spec_sha256) != 64 or any(char not in "0123456789abcdef" for char in spec_sha256):
            spec_sha256 = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        now = utc_now()
        if stall_after_seconds is None:
            configured_stall = os.environ.get("SOLUTION_AGENT_STALL_AFTER_SECONDS", "").strip()
            stall_after_seconds = float(configured_stall) if configured_stall else None
        state = WaitRunState(
            run_id=run_id,
            attempt_id=f"{run_id}:1",
            task_id=task_id,
            spec_id=task_id,
            spec_sha256=spec_sha256,
            execution_status=str(execution_state.get("status") or "unknown"),
            monitor_heartbeat_at=now,
            transport_observed_at=now,
            process_observed_at=now,
            progress_observed_at=now,
            stall_after_seconds=stall_after_seconds,
        )
        atomic_write_json(state_path, state.model_dump(mode="json"))
        _write_compact_views(job_dir, state)
        return state


def wait_disposition(
    job_dir: Path,
    state: WaitRunState | None = None,
    *,
    release_id: str | None = None,
    host_resume_mode: str | None = None,
) -> dict[str, Any]:
    current = state or load_wait_state(job_dir)
    if current.lifecycle_state == "WAITING_EXTERNAL":
        suppress, wake, action = True, False, "END_TURN"
    elif current.lifecycle_state == "COLLECTING":
        suppress, wake, action = False, True, "COLLECT"
    elif current.lifecycle_state == "REVIEW_REQUIRED":
        suppress, wake, action = False, True, "REVIEW"
    else:
        suppress, wake, action = True, False, "NONE"
    mode = host_resume_mode or os.environ.get("SOLUTION_AGENT_HOST_RESUME_MODE", "manual")
    if mode not in {"supported", "manual", "unavailable"}:
        mode = "manual"
    release = release_id or os.environ.get("SOLUTION_AGENT_RELEASE_ID", "runtime-unbound")
    handle_digest = hashlib.sha256(
        f"{current.run_id}\0{current.attempt_id}\0{release}".encode("utf-8")
    ).hexdigest()
    triggers = {
        "WAITING_EXTERNAL": ["completion", "failure", "stalled", "budget", "manual_check"],
        "COLLECTING": ["collection_verified", "collection_rejected"],
        "REVIEW_REQUIRED": ["manual_review"],
        "CLOSED": [],
    }[current.lifecycle_state]
    return WaitDisposition(
        lifecycle_state=current.lifecycle_state,
        state_version=current.state_version,
        suppress_model_continuation=suppress,
        wake_required=wake,
        next_model_action=action,
        state_path=str(_state_path(job_dir)),
        wait_handle=f"wait:{handle_digest}",
        run_id=current.run_id,
        attempt_id=current.attempt_id,
        resume_trigger=triggers,
        manual_resume_required=mode != "supported",
        host_resume_mode=mode,
    ).model_dump(mode="json")


def _event_path(job_dir: Path, event_id: str) -> Path:
    safe_name = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return _wait_dir(job_dir) / "events" / f"{safe_name}.json"


def _ack_path(job_dir: Path, event_id: str) -> Path:
    safe_name = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return _wait_dir(job_dir) / "acks" / f"{safe_name}.json"


def persist_event(job_dir: Path, event: WaitEvent) -> None:
    path = _event_path(job_dir, event.event_id)
    payload = event.model_dump(mode="json")
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if _json_bytes(existing) != _json_bytes(payload):
            raise ValueError(f"event_id collision with different payload: {event.event_id}")
        return
    atomic_write_json(path, payload)


def _ack(
    job_dir: Path,
    event: WaitEvent,
    state: WaitRunState,
    result: str,
    *,
    wake_required: bool = False,
    detail: str | None = None,
) -> WaitAcknowledgement:
    ack = WaitAcknowledgement(
        event_id=event.event_id,
        run_id=event.run_id,
        result=result,
        duplicate=result == "duplicate",
        committed_state_version=state.state_version,
        wake_required=wake_required,
        model_dispatch_allowed=False,
        detail=detail,
    )
    atomic_write_json(_ack_path(job_dir, event.event_id), ack.model_dump(mode="json"))
    return ack


def apply_event(job_dir: Path, event: WaitEvent) -> WaitAcknowledgement:
    with _transaction_lock(job_dir):
        state = load_wait_state(job_dir)
        # Redelivery may reconstruct the same deterministic event after state has
        # advanced.  Check committed IDs before comparing envelope timestamps or
        # versions; the committed canonical state is the idempotency authority.
        if event.event_id in state.applied_event_ids:
            return _ack(job_dir, event, state, "duplicate", detail="event already committed")
        persist_event(job_dir, event)
        if event.run_id != state.run_id or event.attempt_id != state.attempt_id:
            return _ack(job_dir, event, state, "quarantined", detail="run_id or attempt_id mismatch")
        if event.state_version < state.state_version:
            return _ack(job_dir, event, state, "stale", detail="event targets an older state version")
        if event.state_version > state.state_version:
            return _ack(job_dir, event, state, "quarantined", detail="event targets a future state version")

        execution_status = event.payload.get("execution_status")
        if execution_status:
            state.execution_status = str(execution_status)
        if event.kind == "OBSERVATION_UNAVAILABLE":
            state.observation_status = "unavailable"
            state.observation_retry_count += 1
        elif event.kind == "OBSERVATION_RESTORED":
            state.observation_status = "available"
            state.transport_observed_at = event.occurred_at
            state.process_observed_at = event.occurred_at
        elif event.kind == "PROGRESS_ADVANCED":
            marker = event.payload.get("progress_marker")
            state.last_progress_marker = str(marker) if marker is not None else state.last_progress_marker
            state.progress_observed_at = event.occurred_at
            state.stalled_event_emitted = False
        elif event.kind in {"COMPLETION_CANDIDATE", "EXECUTION_FAILURE_VERIFIED"}:
            if state.lifecycle_state == "WAITING_EXTERNAL":
                state.lifecycle_state = "COLLECTING"
                state.evidence_status = "collecting"
                state.collect_transition_count += 1
        elif event.kind in {"STALLED", "DECISION_REQUIRED", "BUDGET_EXCEEDED", "COLLECTION_REJECTED"}:
            state.lifecycle_state = "REVIEW_REQUIRED"
            if event.kind == "STALLED":
                state.stalled_event_emitted = True
        elif event.kind == "COLLECTION_VERIFIED":
            state.lifecycle_state = "CLOSED"
        evidence_status = event.payload.get("evidence_status")
        scientific_status = event.payload.get("scientific_status")
        if evidence_status:
            state.evidence_status = str(evidence_status)
        if scientific_status:
            state.scientific_status = str(scientific_status)

        state.applied_event_ids.append(event.event_id)
        state.state_version += 1
        state.updated_at = utc_now()
        atomic_write_json(_state_path(job_dir), state.model_dump(mode="json"))
        _write_compact_views(job_dir, state, event)
        return _ack(
            job_dir,
            event,
            state,
            "applied",
            wake_required=event.kind in _MATERIAL_KINDS,
        )


def _deterministic_event_id(run_id: str, kind: str, marker: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{kind}\0{marker}".encode("utf-8")).hexdigest()[:24]
    return f"{kind.lower()}:{digest}"


def _build_event(
    state: WaitRunState,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    producer_id: str = "local-monitor",
    occurred_at: datetime | None = None,
) -> WaitEvent:
    body = payload or {}
    return WaitEvent(
        event_id=event_id,
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        state_version=state.state_version,
        kind=kind,
        producer_id=producer_id,
        payload_sha256=hashlib.sha256(_json_bytes(body)).hexdigest(),
        payload=body,
        occurred_at=occurred_at or utc_now(),
    )


def synchronize_execution_state(job_dir: Path, execution_state: dict[str, Any]) -> WaitRunState:
    state = initialize_wait(job_dir, execution_state)
    execution_status = str(execution_state.get("status") or "unknown")
    if execution_status == state.execution_status and execution_status not in _PRE_COLLECT_TERMINAL:
        return state
    if execution_status in _PRE_COLLECT_TERMINAL:
        kind = "COMPLETION_CANDIDATE" if execution_status == "exited" else "EXECUTION_FAILURE_VERIFIED"
        marker = str(execution_state.get("finished_at") or execution_status)
    else:
        kind = "PROGRESS_ADVANCED"
        marker = execution_status
    event = _build_event(
        state,
        event_id=_deterministic_event_id(state.run_id, kind, marker),
        kind=kind,
        payload={"execution_status": execution_status, "progress_marker": marker},
    )
    ack = apply_event(job_dir, event)
    if ack.result in {"applied", "duplicate", "stale"}:
        return load_wait_state(job_dir)
    raise RuntimeError(f"could not synchronize execution state: {ack.result}: {ack.detail}")


def synchronize_observation(job_dir: Path, *, available: bool, detail: str | None = None) -> WaitRunState:
    state = load_wait_state(job_dir)
    desired = "available" if available else "unavailable"
    if state.observation_status == desired:
        return state
    kind = "OBSERVATION_RESTORED" if available else "OBSERVATION_UNAVAILABLE"
    marker = f"{state.state_version}:{kind}"
    event = _build_event(
        state,
        event_id=_deterministic_event_id(state.run_id, kind, marker),
        kind=kind,
        payload={"detail": detail} if detail else {},
    )
    apply_event(job_dir, event)
    return load_wait_state(job_dir)


def record_progress(
    job_dir: Path,
    progress_marker: str,
    *,
    observed_at: datetime | None = None,
) -> WaitRunState:
    state = load_wait_state(job_dir)
    marker = str(progress_marker)
    if marker == state.last_progress_marker:
        return state
    occurred_at = observed_at or utc_now()
    event = _build_event(
        state,
        event_id=_deterministic_event_id(state.run_id, "PROGRESS_ADVANCED", marker),
        kind="PROGRESS_ADVANCED",
        payload={"progress_marker": marker},
        occurred_at=occurred_at,
    )
    apply_event(job_dir, event)
    return load_wait_state(job_dir)


def evaluate_stuck_breaker(job_dir: Path, *, observed_at: datetime | None = None) -> WaitRunState:
    """Emit at most one STALLED event after a declared no-progress threshold.

    Observation loss is deliberately excluded: a transport outage cannot be
    promoted into a stuck or failed execution.
    """

    now = observed_at or utc_now()
    with _transaction_lock(job_dir):
        state = load_wait_state(job_dir)
        state.monitor_heartbeat_at = now
        state.updated_at = now
        atomic_write_json(_state_path(job_dir), state.model_dump(mode="json"))
        _write_compact_views(job_dir, state)
        should_trip = (
            state.lifecycle_state == "WAITING_EXTERNAL"
            and state.execution_status == "running"
            and state.observation_status == "available"
            and state.stall_after_seconds is not None
            and state.progress_observed_at is not None
            and not state.stalled_event_emitted
            and (now - state.progress_observed_at).total_seconds() >= state.stall_after_seconds
        )
    if not should_trip:
        return state
    event = _build_event(
        state,
        event_id=_deterministic_event_id(
            state.run_id,
            "STALLED",
            f"{state.attempt_id}:{state.progress_observed_at.isoformat()}",
        ),
        kind="STALLED",
        payload={
            "last_progress_marker": state.last_progress_marker,
            "progress_observed_at": state.progress_observed_at.isoformat(),
            "stall_after_seconds": state.stall_after_seconds,
        },
        occurred_at=now,
    )
    acknowledgement = apply_event(job_dir, event)
    if acknowledgement.result not in {"applied", "duplicate", "stale"}:
        return load_wait_state(job_dir)
    return load_wait_state(job_dir)


def synchronize_collection_result(job_dir: Path, result: dict[str, Any]) -> WaitRunState:
    state = load_wait_state(job_dir)
    status = str(result.get("status") or "unknown")
    evidence_complete = status in _FINAL_SUCCESS
    if evidence_complete:
        metrics = result.get("metrics_summary") if isinstance(result.get("metrics_summary"), dict) else {}
        raw_science = str(metrics.get("science_status") or "not_assessed").strip().lower()
        science_aliases = {
            "positive": "hypothesis_supported",
            "negative": "hypothesis_not_supported",
            "supported": "hypothesis_supported",
            "not_supported": "hypothesis_not_supported",
        }
        scientific_status = science_aliases.get(raw_science, raw_science)
        if scientific_status not in {
            "hypothesis_supported",
            "hypothesis_not_supported",
            "inconclusive",
            "not_applicable",
        }:
            scientific_status = "not_assessed"
        kind, evidence = "COLLECTION_VERIFIED", "complete"
    else:
        kind, evidence, scientific_status = "COLLECTION_REJECTED", "incomplete", "invalid_evidence"
    marker = f"{status}:{result.get('collected_at') or ''}"
    event = _build_event(
        state,
        event_id=_deterministic_event_id(state.run_id, kind, marker),
        kind=kind,
        payload={
            "execution_status": status,
            "evidence_status": evidence,
            "scientific_status": scientific_status,
        },
    )
    apply_event(job_dir, event)
    return load_wait_state(job_dir)


def start_wait_monitor(job_dir: Path, *, poll_interval_seconds: float = 5.0) -> dict[str, Any]:
    wait_dir = _wait_dir(job_dir)
    wait_dir.mkdir(parents=True, exist_ok=True)
    pid_path = wait_dir / "monitor.pid"
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            if process_is_alive(pid):
                return {"started": False, "pid": pid, "reason": "already_running"}
            pid_path.unlink(missing_ok=True)
        except ValueError:
            pid_path.unlink(missing_ok=True)

    package_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    pythonpath = [item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item]
    if str(package_root) not in pythonpath:
        environment["PYTHONPATH"] = os.pathsep.join([str(package_root), *pythonpath])
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "solution_agent.harness.wait_monitor",
            str(job_dir),
            "--interval",
            str(poll_interval_seconds),
        ],
        cwd=package_root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        **hidden_subprocess_kwargs(new_process_group=True),
    )
    atomic_write_json(wait_dir / "monitor_launch.json", {"pid": proc.pid, "started_at": utc_now().isoformat()})
    return {"started": True, "pid": proc.pid, "reason": "launched"}


__all__ = [
    "apply_event",
    "evaluate_stuck_breaker",
    "initialize_wait",
    "load_wait_state",
    "persist_event",
    "record_progress",
    "start_wait_monitor",
    "synchronize_collection_result",
    "synchronize_execution_state",
    "synchronize_observation",
    "wait_disposition",
]

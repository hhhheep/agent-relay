from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from solution_agent.execution_port.io_utils import atomic_write_json

from .contracts import ProviderUsage, VerifiedReceipt
from .telemetry_contracts import UsageEvent

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_WRITE_LOCK = threading.Lock()


class UsageLedgerError(RuntimeError):
    pass


def _canonical_payload(event: UsageEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude_none=True)


def _canonical_line(event: UsageEvent) -> str:
    return json.dumps(_canonical_payload(event), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _duplicate_identity_line(event: UsageEvent) -> str:
    """Compare retransmitted events while treating observation time as metadata."""

    payload = _canonical_payload(event)
    payload.pop("created_at", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@contextmanager
def _ledger_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
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


def _read_events(path: Path) -> list[UsageEvent]:
    if not path.exists():
        return []
    events: list[UsageEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                events.append(UsageEvent.model_validate_json(line))
            except Exception as exc:
                raise UsageLedgerError(f"invalid usage ledger line {line_number}: {exc}") from exc
    return events


def provider_usage_event(
    *,
    run_id: str,
    side: str,
    phase: str,
    usage: ProviderUsage,
    attempt_id: str | None = None,
    spec_version: int = 1,
    requested_model: str | None = None,
    request_id: str | None = None,
    reasoning_class: str = "unknown",
    model_turn_delta: int = 1,
    context_manifest_sha256: str | None = None,
    tool_manifest_sha256: str | None = None,
) -> UsageEvent:
    return UsageEvent(
        run_id=run_id,
        attempt_id=attempt_id,
        spec_version=spec_version,
        side=side,
        phase=phase,
        operation="model_request",
        provider=usage.provider,
        requested_model=requested_model,
        served_model=usage.model,
        reasoning_class=reasoning_class,
        request_id=request_id,
        input_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.measured_total_tokens,
        cost_usd=usage.cost_usd,
        usage_source="provider",
        model_turn_delta=model_turn_delta,
        context_manifest_sha256=context_manifest_sha256,
        tool_manifest_sha256=tool_manifest_sha256,
    )


def token_guard_receipt_event(
    *,
    run_id: str,
    side: str,
    phase: str,
    receipt: VerifiedReceipt,
    spec_version: int = 1,
    result_payload_bytes: int | None = None,
) -> UsageEvent:
    """Convert one verified Token Guard receipt into an idempotent ledger event."""

    identity = {
        "run_id": run_id,
        "token_guard_run_id": receipt.run_id,
        "command_sha256": receipt.command_sha256,
        "streams": [
            {"name": stream.name, "sha256": stream.sha256, "bytes": stream.bytes}
            for stream in receipt.streams
        ],
    }
    event_digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return UsageEvent(
        usage_event_id=f"token-guard:{event_digest}",
        run_id=run_id,
        attempt_id=receipt.run_id,
        spec_version=spec_version,
        side=side,
        phase=phase,
        operation="tool_call",
        provider="none",
        usage_source="deterministic_counter",
        tool_call_delta=1,
        result_payload_bytes=result_payload_bytes,
        raw_evidence_bytes=sum(stream.bytes for stream in receipt.streams),
        included_evidence_sha256=tuple(stream.sha256 for stream in receipt.streams),
    )


def _empty_group() -> dict[str, int | float]:
    return {
        "events": 0,
        "model_turns": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "provider_usage_events": 0,
        "unavailable_usage_events": 0,
        "model_facing_result_bytes": 0,
        "raw_evidence_bytes": 0,
    }


def _add(group: dict[str, int | float], event: UsageEvent) -> None:
    group["events"] += 1
    group["model_turns"] += event.model_turn_delta
    group["tool_calls"] += event.tool_call_delta
    group["model_facing_result_bytes"] += event.result_payload_bytes or 0
    group["raw_evidence_bytes"] += event.raw_evidence_bytes or 0
    if event.usage_source == "provider":
        group["provider_usage_events"] += 1
        group["input_tokens"] += event.input_tokens or 0
        group["cache_read_tokens"] += event.cache_read_tokens or 0
        group["cache_write_tokens"] += event.cache_write_tokens or 0
        group["output_tokens"] += event.output_tokens or 0
        group["reasoning_tokens"] += event.reasoning_tokens or 0
        group["total_tokens"] += event.total_tokens or 0
        group["cost_usd"] += event.cost_usd or 0.0
    elif event.usage_source == "unavailable":
        group["unavailable_usage_events"] += 1


def summarize_usage_events(events: Iterable[UsageEvent]) -> dict[str, Any]:
    rows = list(events)
    total = _empty_group()
    by_side: dict[str, dict[str, int | float]] = {}
    by_phase: dict[str, dict[str, int | float]] = {}
    by_provider: dict[str, dict[str, int | float]] = {}
    by_model: dict[str, dict[str, int | float]] = {}
    quota_observations: list[dict[str, Any]] = []

    for event in rows:
        _add(total, event)
        for mapping, key in (
            (by_side, event.side),
            (by_phase, event.phase),
            (by_provider, event.provider),
            (by_model, event.served_model or event.requested_model or "none"),
        ):
            group = mapping.setdefault(key, _empty_group())
            _add(group, event)
        if event.quota_window is not None:
            quota_observations.append(event.quota_window.model_dump(mode="json", exclude_none=True))

    def rounded(mapping: dict[str, dict[str, int | float]]) -> dict[str, dict[str, int | float]]:
        for group in mapping.values():
            group["cost_usd"] = round(float(group["cost_usd"]), 8)
        return dict(sorted(mapping.items()))

    total["cost_usd"] = round(float(total["cost_usd"]), 8)
    return {
        "schema_version": "harness-usage-summary/v1",
        "event_count": len(rows),
        "totals": total,
        "by_side": rounded(by_side),
        "by_phase": rounded(by_phase),
        "by_provider": rounded(by_provider),
        "by_model": rounded(by_model),
        "quota_window_observations": quota_observations,
    }


class UsageLedger:
    def __init__(self, run_dir: Path, *, run_id: str) -> None:
        if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\\0"):
            raise ValueError("run_id contains unsupported path characters")
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.usage_dir = self.run_dir / "usage"
        self.events_path = self.usage_dir / "usage_events.jsonl"
        self.summary_path = self.usage_dir / "usage_summary.json"

    def read(self) -> list[UsageEvent]:
        return _read_events(self.events_path)

    def append(self, event: UsageEvent) -> bool:
        if event.run_id != self.run_id:
            raise UsageLedgerError(f"event run_id {event.run_id!r} does not match ledger {self.run_id!r}")
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        line = _canonical_line(event)
        with _ledger_lock(self.events_path):
            existing = _read_events(self.events_path)
            for prior in existing:
                if prior.usage_event_id != event.usage_event_id:
                    continue
                if _duplicate_identity_line(prior) == _duplicate_identity_line(event):
                    return False
                raise UsageLedgerError(f"conflicting duplicate usage_event_id: {event.usage_event_id}")
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return True

    def merge(self, events: Iterable[UsageEvent]) -> dict[str, int]:
        appended = 0
        duplicate = 0
        for event in events:
            if self.append(event):
                appended += 1
            else:
                duplicate += 1
        return {"appended": appended, "duplicate": duplicate}

    def summarize(self, *, write: bool = True) -> dict[str, Any]:
        summary = summarize_usage_events(self.read())
        if self.events_path.exists():
            summary["events_sha256"] = hashlib.sha256(self.events_path.read_bytes()).hexdigest()
        else:
            summary["events_sha256"] = hashlib.sha256(b"").hexdigest()
        summary["run_id"] = self.run_id
        if write:
            self.usage_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.summary_path, summary)
        return summary

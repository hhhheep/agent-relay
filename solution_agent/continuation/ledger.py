from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .paths import continuation_run_ledger_path, continuation_run_ledger_dir


VALID_STAGES = {
    "interpret",
    "plan",
  "write",
  "gate",
  "promote",
  "api_council",
  "proposal_candidate",
  "api_adapter",
  "activate",
  "scheduler_explain",
  "dispatch_request",
  "knowledge_pack",
  "remote_preflight",
    "work_order",
    "launch_intent",
    "collect",
    "decision",
    "proposal_staging",
    "action_request",
    "unknown",
}


VALID_STATUSES = {
    "started",
    "succeeded",
    "blocked",
    "hold",
    "manual_review",
    "failed",
    "skipped",
    "replayed",
}


DEFAULT_SIDE_EFFECTS: Dict[str, bool] = {
    "dispatch": False,
    "registry_mutation": False,
    "remote_execution": False,
    "running_state": False,
    "subprocess": False,
    "scheduler_dispatch": False,
    "remote_write": False,
}


FORBIDDEN_SIDE_EFFECT_KEYS = {
    "dispatch",
    "registry_mutation",
    "remote_execution",
    "running_state",
    "subprocess",
    "scheduler_dispatch",
    "remote_write",
}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _normalize_reasons(reasons: Iterable[Any] | None) -> List[str]:
    if reasons is None:
        return []
    output = []
    for item in reasons:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            output.append(text)
    return output


def _normalize_side_effects(side_effects: Dict[str, Any] | None) -> Dict[str, bool]:
    output: Dict[str, bool] = {key: False for key in DEFAULT_SIDE_EFFECTS}
    if not isinstance(side_effects, dict):
        return output
    for key, value in side_effects.items():
        output[str(key)] = _coerce_bool(value)
    return output


def continuation_run_ledger_path_for_run(run_id: str | None = None, *, path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return continuation_run_ledger_path(run_id=run_id)


def append_continuation_run_event(
    *,
    stage: str,
    status: str,
    actor: str,
    idempotency_key: str,
    run_id: str | None = None,
    source_task_id: str | None = None,
    task_id: str | None = None,
    input_hash: str | None = None,
    output_hash: str | None = None,
    artifact_path: str | None = None,
    reasons: Iterable[Any] | None = None,
    side_effects: Dict[str, Any] | None = None,
    allowed_forbidden_side_effects: Iterable[str] | None = None,
    path: Path | None = None,
    event_id: str | None = None,
    timestamp_utc: str | None = None,
) -> Dict[str, Any]:
    normalized_stage = str(stage or "").strip()
    if normalized_stage not in VALID_STAGES:
        raise ValueError(f"invalid stage: {normalized_stage}")

    normalized_status = str(status or "").strip()
    if normalized_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {normalized_status}")

    normalized_actor = str(actor or "").strip()
    if not normalized_actor:
        raise ValueError("actor is required")

    normalized_idempotency_key = str(idempotency_key or "").strip()
    if not normalized_idempotency_key:
        raise ValueError("idempotency_key is required")

    normalized_reasons = _normalize_reasons(reasons)
    normalized_side_effects = _normalize_side_effects(side_effects)
    allowed = {str(value).strip() for value in allowed_forbidden_side_effects or []}

    for key in FORBIDDEN_SIDE_EFFECT_KEYS:
        if normalized_side_effects.get(key) and key not in allowed:
            raise ValueError(f"forbidden side effect enabled: {key}")

    run_key = (run_id or "default").strip() or "default"
    event = {
        "schema_version": 1,
        "event_id": event_id or f"{run_key}:{uuid.uuid4().hex}",
        "run_id": run_key,
        "source_task_id": str(source_task_id or "").strip() or None,
        "task_id": str(task_id or "").strip() or None,
        "stage": normalized_stage,
        "status": normalized_status,
        "timestamp_utc": timestamp_utc or _now_utc_iso(),
        "actor": normalized_actor,
        "idempotency_key": normalized_idempotency_key,
        "input_hash": str(input_hash or "").strip() or None,
        "output_hash": str(output_hash or "").strip() or None,
        "artifact_path": str(artifact_path or "").strip() or None,
        "reasons": list(normalized_reasons),
        "side_effects": normalized_side_effects,
    }

    target_path = continuation_run_ledger_path_for_run(run_id=run_key, path=path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        for line in target_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(existing, dict):
                continue
            if str(existing.get("idempotency_key", "")).strip() != normalized_idempotency_key:
                continue

            candidate = dict(event)
            existing_candidate = dict(existing)
            candidate.pop("event_id", None)
            candidate.pop("timestamp_utc", None)
            existing_candidate.pop("event_id", None)
            existing_candidate.pop("timestamp_utc", None)
            if candidate == existing_candidate:
                return {
                    "written": False,
                    "path": str(target_path),
                    "event_id": str(existing.get("event_id") or event["event_id"]),
                    "replayed": True,
                    "event": existing,
                }
            raise ValueError(f"idempotency key collision for {normalized_idempotency_key}")

    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    return {
        "written": True,
        "path": str(target_path),
        "event_id": event["event_id"],
        "replayed": False,
        "event": event,
    }


def read_continuation_run_events(
    *,
    run_id: str | None = None,
    path: Path | None = None,
) -> List[Dict[str, Any]]:
    target_path = continuation_run_ledger_path_for_run(run_id=run_id or "default", path=path)
    if not target_path.exists():
        return []

    events: List[Dict[str, Any]] = []
    for line in target_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def summarize_continuation_run_events(
    *,
    run_id: str | None = None,
    path: Path | None = None,
) -> Dict[str, Any]:
    events = read_continuation_run_events(run_id=run_id, path=path)

    by_stage = Counter(event.get("stage") for event in events if event.get("stage"))
    by_status = Counter(event.get("status") for event in events if event.get("status"))
    latest_blocking_reason = None
    latest_blocking_event_id = None
    for event in events:
        if event.get("status") in {"blocked", "hold", "manual_review", "failed"}:
            reasons = _normalize_reasons(event.get("reasons", []))
            if reasons:
                latest_blocking_reason = reasons[-1]
                latest_blocking_event_id = event.get("event_id")
            elif event.get("event_id"):
                latest_blocking_event_id = event.get("event_id")
                latest_blocking_reason = event.get("status")
    return {
        "run_id": run_id or "default",
        "path": str(continuation_run_ledger_path_for_run(run_id=run_id or "default", path=path)),
        "count": len(events),
        "stage_counts": dict(by_stage),
        "status_counts": dict(by_status),
        "latest_blocking_reason": latest_blocking_reason,
        "latest_blocking_event_id": latest_blocking_event_id,
    }


__all__ = [
    "append_continuation_run_event",
    "read_continuation_run_events",
    "summarize_continuation_run_events",
    "continuation_run_ledger_path",
    "continuation_run_ledger_dir",
]
__all__

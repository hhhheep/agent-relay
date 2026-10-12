from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .paths import continuation_observation_log_path


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_normalize(value)] if _normalize(value) else []
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value if _normalize(item)]
    return [_normalize(value)]


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_continuation_observation(
    *,
    run_id: str | None,
    stage: str,
    actor: str,
    status: str,
    idempotency_key: str,
    source_task_id: str | None = None,
    reasons: Iterable[Any] | None = None,
    artifact_path: str | None = None,
    payload: Dict[str, Any] | None = None,
    path: Path | None = None,
) -> Dict[str, Any]:
    normalized_stage = _normalize(stage)
    if not normalized_stage:
        raise ValueError("stage is required")
    normalized_status = _normalize(status)
    if not normalized_status:
        raise ValueError("status is required")
    normalized_actor = _normalize(actor)
    if not normalized_actor:
        normalized_actor = "system"
    normalized_idempotency = _normalize(idempotency_key)
    if not normalized_idempotency:
        raise ValueError("idempotency_key is required")

    normalized_payload = dict(payload or {})
    normalized_payload["_meta"] = {
        "idempotency_key": normalized_idempotency,
        "reason_count": len(_normalize_str_list(reasons)),
    }

    record = {
        "schema_version": 1,
        "event_id": f"{_normalize(run_id) or 'default'}:{uuid.uuid4().hex}",
        "run_id": _normalize(run_id) or "default",
        "stage": normalized_stage,
        "status": normalized_status,
        "timestamp_utc": _now_utc_iso(),
        "actor": normalized_actor,
        "idempotency_key": normalized_idempotency,
        "source_task_id": _normalize(source_task_id) or None,
        "artifact_path": _normalize(artifact_path) or None,
        "reasons": _normalize_str_list(reasons),
        "payload": normalized_payload,
    }

    target_path = continuation_observation_log_path(run_id=run_id) if path is None else Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        existing_lines = [line.strip() for line in target_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for line in existing_lines:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(existing, dict):
                continue
            if str(existing.get("idempotency_key", "")) != normalized_idempotency:
                continue

            candidate = dict(record)
            existing_candidate = dict(existing)
            candidate.pop("event_id", None)
            candidate.pop("timestamp_utc", None)
            existing_candidate.pop("event_id", None)
            existing_candidate.pop("timestamp_utc", None)
            if _canonical_json(candidate) == _canonical_json(existing_candidate):
                return {
                    "written": False,
                    "path": str(target_path),
                    "event_id": str(existing.get("event_id") or record["event_id"]),
                    "replayed": True,
                    "record": existing,
                }
            raise ValueError(f"idempotency key collision for {normalized_idempotency}")

    # write_text is not append-safe; explicitly append one record at a time.
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(record) + "\n")

    return {
        "written": True,
        "path": str(target_path),
        "event_id": record["event_id"],
        "replayed": False,
        "record": record,
    }


def read_continuation_observations(
    *,
    run_id: str | None = None,
    path: Path | None = None,
) -> List[Dict[str, Any]]:
    target_path = continuation_observation_log_path(run_id=run_id) if path is None else Path(path)
    if not target_path.exists():
        return []

    observations: List[Dict[str, Any]] = []
    for line in target_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            observations.append(payload)
    return observations


def summarize_continuation_observations(
    *,
    observations: List[Dict[str, Any]] | None = None,
    run_id: str | None = None,
    path: Path | None = None,
) -> Dict[str, Any]:
    records = list(observations) if observations is not None else read_continuation_observations(run_id=run_id, path=path)
    latest = records[-1] if records else {}
    latest_payload = latest.get("payload")
    if not isinstance(latest_payload, dict):
        latest_payload = {}
    else:
        latest_payload = {key: value for key, value in latest_payload.items() if key != "_meta"}
    return {
        "count": len(records),
        "stages": list(dict.fromkeys(_normalize(item.get("stage") or "") for item in records if _normalize(item.get("stage")))),
        "latest_stage": _normalize(latest.get("stage")),
        "latest_status": _normalize(latest.get("status")),
        "latest_source_task_id": _normalize(latest.get("source_task_id")),
        "latest_artifact_path": _normalize(latest.get("artifact_path")),
        "latest_reasons": _normalize_str_list(latest.get("reasons")),
        "latest_payload": latest_payload,
        "actors": list(dict.fromkeys(_normalize(item.get("actor") or "") for item in records if _normalize(item.get("actor")))),
        "paths": list(
            dict.fromkeys(
                _normalize(item.get("artifact_path") or "")
                for item in records
                if _normalize(item.get("artifact_path"))
            )
        ),
    }


__all__ = [
    "append_continuation_observation",
    "read_continuation_observations",
    "summarize_continuation_observations",
]

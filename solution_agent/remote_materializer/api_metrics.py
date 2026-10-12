from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


API_METRICS_SCHEMA_VERSION = 2
_METRICS_WRITE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def api_metrics_path() -> Path:
    configured = os.environ.get("SOLUTION_AGENT_API_METRICS_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path.cwd() / ".solution" / "api_model_calls.jsonl"


def api_metrics_enabled() -> bool:
    if os.environ.get("SOLUTION_AGENT_API_METRICS_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("SOLUTION_AGENT_API_METRICS_PATH"):
        return False
    return True


@contextmanager
def _api_metrics_file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _METRICS_WRITE_LOCK:
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


def _truncate(value: object, limit: int = 300) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _message_chars(messages: list[dict[str, str]]) -> int:
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += len(str(message.get("content") or ""))
    return total


def write_api_call_metric(
    *,
    provider: str,
    model: str,
    role: str,
    status: str,
    latency_ms: int,
    messages: list[dict[str, str]],
    response_text: str = "",
    usage: dict[str, Any] | None = None,
    error_type: str = "",
    error_message: str = "",
    response_format: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    cost_usd: float | None = None,
) -> None:
    if not api_metrics_enabled():
        return
    normalized_usage: dict[str, Any] = {}
    if usage:
        try:
            from solution_agent.harness.usage import normalize_provider_usage

            normalized = normalize_provider_usage(
                {"model": model, "usage": usage},
                provider=provider,
                requested_model=model,
                cost_usd=cost_usd,
            )
            if normalized is not None:
                normalized_usage = {
                    "input_tokens": normalized.input_tokens,
                    "new_input_tokens": normalized.new_input_tokens,
                    "cache_read_tokens": normalized.cache_read_tokens,
                    "cache_write_tokens": normalized.cache_write_tokens,
                    "output_tokens": normalized.output_tokens,
                    "total_tokens": normalized.measured_total_tokens,
                    "cost_usd": normalized.cost_usd,
                }
        except (TypeError, ValueError):
            # Preserve the raw provider payload even when it cannot be normalized.
            normalized_usage = {}
    event = {
        "schema_version": API_METRICS_SCHEMA_VERSION,
        "created_at": _now_iso(),
        "provider": provider,
        "model": model,
        "role": role or "unknown",
        "status": status,
        "latency_ms": int(max(0, latency_ms)),
        "prompt_chars": _message_chars(messages),
        "completion_chars": len(response_text or ""),
        "usage": usage or {},
        "normalized_usage": normalized_usage,
        "error_type": error_type,
        "error_message": _truncate(error_message),
        "response_format": response_format,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        path = api_metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with _api_metrics_file_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        return


def summarize_api_metrics(path: Path | None = None) -> dict[str, Any]:
    source = path or api_metrics_path()
    if not source.exists():
        return {
            "schema_version": API_METRICS_SCHEMA_VERSION,
            "path": str(source),
            "total_calls": 0,
            "groups": [],
        }
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    total_calls = 0
    with source.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            provider = str(event.get("provider") or "unknown")
            model = str(event.get("model") or "unknown")
            role = str(event.get("role") or "unknown")
            key = (provider, model, role)
            group = groups.setdefault(
                key,
                {
                    "provider": provider,
                    "model": model,
                    "role": role,
                    "calls": 0,
                    "success": 0,
                    "error": 0,
                    "latency_ms_total": 0,
                    "prompt_chars_total": 0,
                    "completion_chars_total": 0,
                    "prompt_tokens_total": 0,
                    "completion_tokens_total": 0,
                    "new_input_tokens_total": 0,
                    "cache_read_tokens_total": 0,
                    "cache_write_tokens_total": 0,
                    "total_tokens_total": 0,
                    "cost_usd_total": 0.0,
                    "cost_observation_count": 0,
                    "errors_by_type": {},
                },
            )
            total_calls += 1
            group["calls"] += 1
            status = str(event.get("status") or "")
            if status == "success":
                group["success"] += 1
            else:
                group["error"] += 1
                error_type = str(event.get("error_type") or "unknown")
                group["errors_by_type"][error_type] = int(group["errors_by_type"].get(error_type) or 0) + 1
            group["latency_ms_total"] += int(event.get("latency_ms") or 0)
            group["prompt_chars_total"] += int(event.get("prompt_chars") or 0)
            group["completion_chars_total"] += int(event.get("completion_chars") or 0)
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            normalized = event.get("normalized_usage") if isinstance(event.get("normalized_usage"), dict) else {}
            if not normalized and usage:
                try:
                    from solution_agent.harness.usage import normalize_provider_usage

                    parsed = normalize_provider_usage(
                        {"model": model, "usage": usage},
                        provider=provider,
                        requested_model=model,
                    )
                    if parsed is not None:
                        normalized = {
                            "input_tokens": parsed.input_tokens,
                            "new_input_tokens": parsed.new_input_tokens,
                            "cache_read_tokens": parsed.cache_read_tokens,
                            "cache_write_tokens": parsed.cache_write_tokens,
                            "output_tokens": parsed.output_tokens,
                            "total_tokens": parsed.measured_total_tokens,
                            "cost_usd": parsed.cost_usd,
                        }
                except (TypeError, ValueError):
                    normalized = {}
            group["prompt_tokens_total"] += int(normalized.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            group["completion_tokens_total"] += int(normalized.get("output_tokens") or usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            group["new_input_tokens_total"] += int(normalized.get("new_input_tokens") or 0)
            group["cache_read_tokens_total"] += int(normalized.get("cache_read_tokens") or 0)
            group["cache_write_tokens_total"] += int(normalized.get("cache_write_tokens") or 0)
            group["total_tokens_total"] += int(normalized.get("total_tokens") or 0)
            if normalized.get("cost_usd") is not None:
                group["cost_usd_total"] += float(normalized["cost_usd"])
                group["cost_observation_count"] += 1
    output_groups = []
    for group in groups.values():
        calls = int(group["calls"] or 0)
        output_groups.append(
            {
                "provider": group["provider"],
                "model": group["model"],
                "role": group["role"],
                "calls": calls,
                "success": group["success"],
                "error": group["error"],
                "success_rate": round(float(group["success"]) / calls, 4) if calls else 0.0,
                "avg_latency_ms": round(float(group["latency_ms_total"]) / calls, 1) if calls else 0.0,
                "avg_prompt_chars": round(float(group["prompt_chars_total"]) / calls, 1) if calls else 0.0,
                "avg_completion_chars": round(float(group["completion_chars_total"]) / calls, 1) if calls else 0.0,
                "prompt_tokens_total": group["prompt_tokens_total"],
                "completion_tokens_total": group["completion_tokens_total"],
                "new_input_tokens_total": group["new_input_tokens_total"],
                "cache_read_tokens_total": group["cache_read_tokens_total"],
                "cache_write_tokens_total": group["cache_write_tokens_total"],
                "total_tokens_total": group["total_tokens_total"],
                "cost_usd_total": round(float(group["cost_usd_total"]), 8),
                "cost_observation_count": group["cost_observation_count"],
                "errors_by_type": dict(sorted(group["errors_by_type"].items())),
            }
        )
    output_groups.sort(key=lambda item: (str(item["provider"]), str(item["model"]), str(item["role"])))
    return {
        "schema_version": API_METRICS_SCHEMA_VERSION,
        "path": str(source),
        "total_calls": total_calls,
        "groups": output_groups,
    }

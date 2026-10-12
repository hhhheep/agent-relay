#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from solution_agent.harness.usage import normalize_provider_usage
from solution_agent.harness.telemetry_contracts import UsageEvent
from solution_agent.harness.usage_ledger import UsageLedger, provider_usage_event


FIXED_PROVIDER = "bailian"
FIXED_MODEL = "deepseek-v4-flash-0731"
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, metadata: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.metadata = metadata


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    resolved = path.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside workspace: {path}") from exc
    return resolved


def _configured_roots(name: str, fallback: Path) -> list[Path]:
    values = [item for item in os.environ.get(name, "").split(os.pathsep) if item.strip()]
    roots = [Path(item).resolve() for item in values]
    return roots or [fallback.resolve()]


def _inside_any(path: Path, roots: list[Path], *, label: str, must_exist: bool = False) -> Path:
    resolved = path.resolve(strict=must_exist)
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(f"{label} path is outside declared roots: {path}")


def _atomic_write(path: Path, text: str) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard-link publish is atomic and fails if another writer created the
        # destination after the initial check.  The temporary file is on the
        # same filesystem because it lives in path.parent.
        os.link(temporary, path)
        os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path, max_bytes: int) -> tuple[dict[str, Any], str]:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"evidence exceeds maximum size ({size} > {max_bytes})")
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence packet must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "task_id",
        "title",
        "question",
        "report_type",
        "execution_status",
        "scientific_status",
        "claims",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"evidence packet missing fields: {', '.join(missing)}")
    if value["schema_version"] != "1.0":
        raise ValueError("unsupported evidence schema_version")
    claims = value["claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a non-empty list")
    claim_ids: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or not all(claim.get(key) for key in ("claim_id", "kind", "text", "source")):
            raise ValueError("every claim needs claim_id, kind, text, and source")
        claim_ids.append(str(claim["claim_id"]))
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim_id values must be unique")
    return value


def _validate_base_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    shared_hosts = {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "cn-hongkong.dashscope.aliyuncs.com",
    }
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Bailian base URL must be credential-free HTTPS")
    if not host.endswith(".maas.aliyuncs.com") and host not in shared_hosts:
        raise ValueError("Bailian base URL must use an official workspace or shared host")
    if parsed.path.rstrip("/") != "/compatible-mode/v1" or parsed.query or parsed.fragment:
        raise ValueError("Bailian base URL must end with /compatible-mode/v1")
    return value


def _parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("provider response does not contain a JSON object") from None
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("provider response must be a JSON object")
    return value


def _extract_response_text(message: dict[str, Any]) -> tuple[str, str]:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content, "content"
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("output_text")
                if isinstance(value, str):
                    parts.append(value)
        joined = "\n".join(part for part in parts if part.strip())
        if joined.strip():
            return joined, "structured_content"
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning, "reasoning_content"
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if isinstance(arguments, str) and arguments.strip():
                return arguments, "tool_call_arguments"
    return "", "none"


def _response_metadata(payload: dict[str, Any], *, http_status: int, source: str, parse_status: str) -> dict[str, Any]:
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    return {
        "response_id": payload.get("id"),
        "executed_model": payload.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        "http_status": http_status,
        "content_present": bool(isinstance(content, str) and content.strip()) or bool(isinstance(content, list) and content),
        "reasoning_content_present": bool(str(message.get("reasoning_content") or "").strip()),
        "structured_content_present": source in {"structured_content", "tool_call_arguments"},
        "selected_source": source,
        "parse_status": parse_status,
    }


def _aggregate_usage(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cached_tokens = 0
    has_usage = False
    for payload in payloads:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        has_usage = True
        for key in aggregate:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                aggregate[key] += value
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
            cached_tokens += details["cached_tokens"]
    if not has_usage:
        return {}
    if cached_tokens:
        aggregate["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return aggregate


def _http_error_metadata(error: urllib.error.HTTPError, *, attempt: int) -> dict[str, Any]:
    """Return a bounded, credential-free summary of an OpenAI-compatible error body."""
    limit = 65536
    try:
        observed = error.read(limit + 1)
    except Exception:
        observed = b""
    truncated = len(observed) > limit
    captured = observed[:limit]
    metadata: dict[str, Any] = {
        "http_status": int(error.code),
        "parse_status": "http_error",
        "attempt": attempt,
        "response_body_bytes_observed": len(observed),
        "response_body_truncated": truncated,
    }
    digest_key = "response_body_prefix_sha256" if truncated else "response_body_sha256"
    metadata[digest_key] = hashlib.sha256(captured).hexdigest()

    try:
        payload = json.loads(captured.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        error_value = payload.get("error")
        details = error_value if isinstance(error_value, dict) else payload
        safe: dict[str, Any] = {}
        for key in ("code", "type", "param", "message"):
            value = details.get(key)
            if value is None or isinstance(value, (bool, int, float)):
                safe[key] = value
            elif isinstance(value, str):
                safe[key] = value[:2048]
        request_id = payload.get("request_id") or details.get("request_id")
        if isinstance(request_id, str):
            safe["request_id"] = request_id[:256]
        if safe:
            metadata["provider_error"] = safe
            message = str(safe.get("message") or "")
            if "InternalError.Algo" in message or "Backend buffer overflow" in message:
                metadata["failure_category"] = "provider_internal"
                metadata["retryable"] = True
    return metadata


def _provider_request(system: str, user: str, *, max_output_tokens: int) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY is not configured")
    base_url = _validate_base_url(os.environ.get("BAILIAN_BASE_URL", DEFAULT_BASE_URL))
    messages = [
        {"role": "system", "content": system + "\nReturn one JSON object only."},
        {"role": "user", "content": user},
    ]
    attempts: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    for attempt_index in range(2):
        body = json.dumps(
            {
                "model": FIXED_MODEL,
                "messages": messages,
                "response_format": {"type": "json_object"},
                # Bailian JSON mode and thinking mode are not a reliable combination.
                # Report drafting is evidence transformation, so request deterministic
                # structured output and leave scientific reasoning with Codex.
                "enable_thinking": False,
                "temperature": 0.2,
                "max_tokens": max_output_tokens,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                http_status = int(getattr(response, "status", 200))
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            metadata = [_http_error_metadata(error, attempt=attempt_index + 1)]
            raise ProviderResponseError(f"provider HTTP {error.code}", metadata) from None
        if not isinstance(payload, dict):
            attempts.append({"http_status": http_status, "parse_status": "non_object_response", "attempt": attempt_index + 1})
            break
        payloads.append(payload)
        choices = payload.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text, source = _extract_response_text(message)
        try:
            parsed = _parse_model_json(text) if text else None
        except (ValueError, json.JSONDecodeError):
            parsed = None
        parse_status = "parsed" if isinstance(parsed, dict) else "empty" if not text else "non_json"
        metadata = _response_metadata(payload, http_status=http_status, source=source, parse_status=parse_status)
        metadata["attempt"] = attempt_index + 1
        attempts.append(metadata)
        if isinstance(parsed, dict):
            returned = dict(payload)
            returned["usage"] = _aggregate_usage(payloads)
            returned["_harness_response_metadata"] = attempts
            return parsed, returned
        if attempt_index == 0:
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": "Correct the response format. Return the requested JSON object only; do not leave it empty or wrap it in prose.",
                },
            ]
    raise ProviderResponseError("provider returned no parseable JSON object after one corrective retry", attempts)


def run(
    *,
    evidence_path: Path,
    output_dir: Path,
    report_name: str = "report.md",
    rules_path: Path | None = None,
    destination_kind: str = "draft_only",
    destination_path: Path | None = None,
    destination_record_id: str | None = None,
    expected_destination_sha256: str | None = None,
    update_reason: str | None = None,
    max_input_bytes: int = 524288,
    max_output_tokens: int = 8192,
    provider_call: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = _provider_request,
) -> dict[str, Any]:
    workspace = Path.cwd().resolve()
    input_roots = _configured_roots("SOLUTION_AGENT_INPUT_ROOTS", workspace)
    output_roots = _configured_roots("SOLUTION_AGENT_OUTPUT_ROOTS", workspace)
    if os.environ.get("SOLUTION_AGENT_AUTHORITY_ROOTS", "").strip():
        authority_roots = _configured_roots("SOLUTION_AGENT_AUTHORITY_ROOTS", workspace)
    else:
        authority_roots = [workspace / "research" / "records", workspace / ".research"]
    evidence_path = _inside_any(evidence_path, input_roots, label="input", must_exist=True)
    output_dir = _inside_any(output_dir, output_roots, label="output")
    if any(output_dir == root or root in output_dir.parents for root in authority_roots):
        raise ValueError("report writer output cannot target a managed authority root")
    if Path(report_name).name != report_name or not report_name.lower().endswith(".md"):
        raise ValueError("report_name must be a Markdown basename")
    destination_kind = destination_kind.strip().lower()
    if destination_kind not in {"draft_only", "local_file", "researchflow"}:
        raise ValueError("destination_kind must be draft_only, local_file, or researchflow")
    if destination_kind != "draft_only" and destination_path is None:
        raise ValueError("destination_path is required for a managed publication")
    if destination_kind == "researchflow" and not (destination_record_id or "").strip():
        raise ValueError("destination_record_id is required for ResearchFlow publication")
    if expected_destination_sha256 and (
        len(expected_destination_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_destination_sha256)
    ):
        raise ValueError("expected_destination_sha256 must be a 64-character hexadecimal SHA-256")
    rules_text = ""
    rules_hash = None
    if rules_path is not None:
        rules_path = _inside_any(rules_path, input_roots, label="rules", must_exist=True)
        if rules_path.stat().st_size > max_input_bytes:
            raise ValueError("rules file exceeds maximum size")
        rules_text = rules_path.read_text(encoding="utf-8")
        rules_hash = _sha256(rules_path)
    evidence, evidence_hash = _read_json(evidence_path, max_input_bytes)
    _validate_evidence(evidence)
    claim_ids = {str(item["claim_id"]) for item in evidence["claims"]}
    report_path = output_dir / report_name
    sidecar_path = report_path.with_suffix(report_path.suffix + ".writer.json")
    metrics_path = output_dir / "report_writer_metrics.json"
    publication_path = output_dir / "publication_packet.json"
    if any(path.exists() for path in (report_path, sidecar_path, metrics_path, publication_path)):
        raise FileExistsError("report writer output already exists")

    system = "\n\n".join(
        filter(
            None,
            [
                "Write a concise research report grounded only in the supplied evidence packet.",
                "Separate measured observations, interpretations, limitations, and adopted decisions.",
                "Do not turn missing artifacts, interrupted execution, or inconclusive results into negative scientific findings.",
                "Every quantitative or consequential statement must be traceable to a supplied claim_id or artifact path.",
                "Return report_markdown, claims_used, warnings, and unsupported_claims.",
                rules_text,
            ],
        )
    )
    user = "EVIDENCE PACKET:\n" + json.dumps(evidence, ensure_ascii=False)
    try:
        generated, raw_response = provider_call(system, user, max_output_tokens=max_output_tokens)
    except ProviderResponseError as exc:
        _record_writer_usage(
            usage=None,
            raw_response={"_harness_response_metadata": exc.metadata},
        )
        failure_path = output_dir / "report_writer_failure.json"
        _atomic_write(
            failure_path,
            json.dumps(
                {
                    "schema_version": "1.0",
                    "failure_type": "provider_response_unusable",
                    "message": str(exc),
                    "response_metadata": exc.metadata,
                    "prompt_persisted": False,
                    "credential_persisted": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        raise
    report = generated.get("report_markdown")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("provider field report_markdown must be a non-empty string")
    for field in ("claims_used", "warnings", "unsupported_claims"):
        if not isinstance(generated.get(field), list) or not all(isinstance(item, str) for item in generated[field]):
            raise ValueError(f"provider field {field} must be a string list")
    unknown_claims = sorted(set(generated["claims_used"]) - claim_ids)
    if unknown_claims:
        raise ValueError("provider cited unknown claim_ids: " + ", ".join(unknown_claims))

    usage = normalize_provider_usage(
        raw_response,
        provider=FIXED_PROVIDER,
        requested_model=FIXED_MODEL,
    )
    _record_writer_usage(usage=usage, raw_response=raw_response)
    _atomic_write(report_path, report.strip() + "\n")
    sidecar = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "credential_transport": "environment_ephemeral",
        "evidence_path": str(evidence_path),
        "evidence_sha256": evidence_hash,
        "rules_path": str(rules_path) if rules_path else None,
        "rules_sha256": rules_hash,
        "output_path": str(report_path),
        "output_sha256": _sha256(report_path),
        "claims_used": generated["claims_used"],
        "warnings": generated["warnings"],
        "unsupported_claims": generated["unsupported_claims"],
        "provider": {
            "provider": FIXED_PROVIDER,
            "requested_model": FIXED_MODEL,
            "executed_model": str(raw_response.get("model") or FIXED_MODEL),
            "response_id": raw_response.get("id"),
            "usage": usage.model_dump(mode="json") if usage is not None else None,
            "response_metadata": raw_response.get("_harness_response_metadata"),
        },
    }
    _atomic_write(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n")
    metrics = {
        "execution_status": "completed",
        "contract_status": "complete",
        "science_status": "report_drafted_not_a_new_experiment",
        "claims_used_count": len(generated["claims_used"]),
        "warning_count": len(generated["warnings"]),
        "unsupported_claim_count": len(generated["unsupported_claims"]),
        "input_tokens": usage.input_tokens if usage is not None else None,
        "new_input_tokens": usage.new_input_tokens if usage is not None else None,
        "cache_read_tokens": usage.cache_read_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "total_tokens": usage.measured_total_tokens if usage is not None else None,
        "model_execution_evidence": {
            "requested_model": FIXED_MODEL,
            "executed_model": str(raw_response.get("model") or FIXED_MODEL),
            "fallback_reason": None,
            "proxy_only": False,
            "formal_evidence": False,
            "allowed_claims": sorted(claim_ids),
            "forbidden_claims": list(evidence.get("forbidden_claims") or []),
            "evidence_scope": "bounded_probe",
        },
    }
    _atomic_write(metrics_path, json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    publication = {
        "schema_version": "1.0",
        "kind": destination_kind,
        "commit_required": destination_kind != "draft_only",
        "commit_protocol": (
            "researchflow_get_sha_put_with_409_reconcile"
            if destination_kind == "researchflow"
            else "local_compare_and_swap"
            if destination_kind == "local_file"
            else "none"
        ),
        "draft_path": str(report_path),
        "draft_sha256": sidecar["output_sha256"],
        "destination_path": str(destination_path) if destination_path else None,
        "destination_record_id": destination_record_id,
        "expected_destination_sha256": expected_destination_sha256,
        "reason": update_reason,
        "writer_must_not_commit": True,
    }
    _atomic_write(publication_path, json.dumps(publication, ensure_ascii=False, indent=2) + "\n")
    return {
        "written": True,
        "report_path": str(report_path),
        "sidecar_path": str(sidecar_path),
        "metrics_path": str(metrics_path),
        "publication_packet_path": str(publication_path),
        "output_sha256": sidecar["output_sha256"],
        "claims_used_count": metrics["claims_used_count"],
        "usage": sidecar["provider"]["usage"],
    }


def _record_writer_usage(*, usage, raw_response: dict) -> None:
    """Append safe provider measurements when launched by the Harness wrapper."""

    run_dir = os.environ.get("SOLUTION_AGENT_USAGE_RUN_DIR", "").strip()
    run_id = os.environ.get("SOLUTION_AGENT_USAGE_RUN_ID", "").strip()
    if not run_dir or not run_id:
        return
    metadata = raw_response.get("_harness_response_metadata")
    request_count = len(metadata) if isinstance(metadata, list) and metadata else 1
    request_id = str(raw_response.get("id") or "") or None
    try:
        if usage is not None:
            event = provider_usage_event(
                run_id=run_id,
                side="external_writer",
                phase="report",
                usage=usage,
                requested_model=FIXED_MODEL,
                request_id=request_id,
                reasoning_class="routine",
                model_turn_delta=request_count,
            )
        else:
            event = UsageEvent(
                run_id=run_id,
                side="external_writer",
                phase="report",
                operation="model_request",
                provider=FIXED_PROVIDER,
                requested_model=FIXED_MODEL,
                served_model=str(raw_response.get("model") or FIXED_MODEL),
                reasoning_class="routine",
                request_id=request_id,
                usage_source="unavailable",
                model_turn_delta=request_count,
            )
        ledger = UsageLedger(Path(run_dir), run_id=run_id)
        ledger.append(event)
        ledger.summarize()
    except (OSError, ValueError, TypeError, RuntimeError):
        # P0 is observation-only; report production remains authoritative.
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence_path", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--report_name", default="report.md")
    parser.add_argument("--rules_path", type=Path)
    parser.add_argument("--destination_kind", default="draft_only")
    parser.add_argument("--destination_path", type=Path)
    parser.add_argument("--destination_record_id")
    parser.add_argument("--expected_destination_sha256")
    parser.add_argument("--update_reason")
    parser.add_argument("--max_input_bytes", type=int, default=524288)
    parser.add_argument("--max_output_tokens", type=int, default=8192)
    args = parser.parse_args()
    result = run(**vars(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Remote report writer. Reads one JSON envelope from stdin and never persists credentials."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_INPUT_BYTES = 512 * 1024
FIXED_PROVIDER = "bailian"
FIXED_MODEL = "deepseek-v4-flash-0731"


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed_path(raw: object, roots: list[str], must_exist: bool = False) -> pathlib.Path:
    if not isinstance(raw, str) or not raw.startswith("/") or any(c in raw for c in "\0\r\n"):
        raise ValueError("path must be an absolute POSIX path")
    candidate = pathlib.Path(raw)
    resolved = candidate.resolve(strict=must_exist)
    permitted = False
    for root_raw in roots:
        root = pathlib.Path(root_raw).resolve(strict=True)
        try:
            resolved.relative_to(root)
            permitted = True
            break
        except ValueError:
            continue
    if not permitted:
        raise ValueError(f"path is outside allowed roots: {candidate}")
    return resolved


def read_limited(path: pathlib.Path, max_bytes: int) -> tuple[str, str]:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"input exceeds maximum size ({size} > {max_bytes})")
    data = path.read_bytes()
    return data.decode("utf-8"), sha256_bytes(data)


def validate_evidence(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("evidence packet must be an object")
    required = [
        "schema_version", "task_id", "title", "question", "report_type",
        "execution_status", "scientific_status", "claims"
    ]
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"evidence packet missing fields: {', '.join(missing)}")
    if value["schema_version"] != "1.0":
        raise ValueError("unsupported evidence schema_version")
    claims = value["claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a non-empty list")
    ids = []
    for claim in claims:
        if not isinstance(claim, dict) or not all(claim.get(key) for key in ("claim_id", "kind", "text", "source")):
            raise ValueError("every claim needs claim_id, kind, text, and source")
        ids.append(claim["claim_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("claim_id values must be unique")
    return value


def atomic_write(path: pathlib.Path, text: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def provider_request(provider: dict, system: str, user: str) -> tuple[dict, dict]:
    api_key = provider.pop("api_key", None)
    if not api_key:
        raise ValueError("provider API key is missing")
    base_url = str(provider.get("base_url") or "").rstrip("/")
    model = provider.get("model")
    name = provider.get("provider")
    if not base_url or name != FIXED_PROVIDER or model != FIXED_MODEL:
        raise ValueError("provider configuration is incomplete")
    parsed_url = urllib.parse.urlparse(base_url)
    host = (parsed_url.hostname or "").lower()
    if parsed_url.scheme != "https" or parsed_url.username or parsed_url.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    shared_hosts = {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "cn-hongkong.dashscope.aliyuncs.com",
    }
    if not host.endswith(".maas.aliyuncs.com") and host not in shared_hosts:
        raise ValueError("Bailian base URL must use an official workspace or shared host")
    if parsed_url.path.rstrip("/") != "/compatible-mode/v1" or parsed_url.query or parsed_url.fragment:
        raise ValueError("Bailian base URL must end with /compatible-mode/v1 and contain no query or fragment")
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": f"{system}\nReturn a JSON object only."},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "enable_thinking": False,
        "temperature": 0.2,
        "max_tokens": 8192,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"provider HTTP {error.code}: {detail}") from None
    choices = payload.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if not content:
        raise RuntimeError("provider returned empty content")
    parsed = json.loads(content)
    provenance = {
        "provider": name,
        "model": model,
        "response_id": payload.get("id"),
        "usage": payload.get("usage"),
    }
    return parsed, provenance


def require_string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"provider field {name} must be a string list")
    return value


def load_envelope() -> dict:
    raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError("request envelope is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request envelope must be an object")
    return value


def main() -> None:
    request = load_envelope()
    roots = request.get("allowed_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("allowed_roots is required")
    deployment_root = pathlib.Path(__file__).resolve().parents[2]
    checked_roots = []
    for root_raw in roots:
        root = pathlib.Path(root_raw).resolve(strict=True)
        try:
            root.relative_to(deployment_root)
        except ValueError:
            raise ValueError(f"allowed root is outside deployment workspace: {root}") from None
        checked_roots.append(str(root))
    roots = checked_roots
    max_bytes = int(request.get("max_input_bytes", DEFAULT_MAX_INPUT_BYTES))
    evidence_path = allowed_path(request.get("evidence_path"), roots, must_exist=True)
    evidence_text, evidence_hash = read_limited(evidence_path, max_bytes)
    evidence = validate_evidence(json.loads(evidence_text))
    mode = request.get("mode")

    if mode == "validate":
        print(json.dumps({
            "valid": True,
            "evidence_path": str(evidence_path),
            "evidence_sha256": evidence_hash,
            "claim_count": len(evidence["claims"]),
        }, ensure_ascii=False))
        return

    provider = request.get("provider")
    if not isinstance(provider, dict):
        raise ValueError("provider configuration is required")

    if mode == "draft":
        rules_text = ""
        rules_hash = None
        rules_path_value = request.get("rules_path")
        rules_path = None
        if rules_path_value:
            rules_path = allowed_path(rules_path_value, roots, must_exist=True)
            rules_text, rules_hash = read_limited(rules_path, max_bytes)
        output_path = allowed_path(request.get("output_path"), roots)
        sidecar_path = pathlib.Path(str(output_path) + ".writer.json")
        overwrite = bool(request.get("overwrite"))
        if not overwrite and (output_path.exists() or sidecar_path.exists()):
            raise FileExistsError(f"output or sidecar already exists: {output_path}")
        system = "\n\n".join(filter(None, [
            "Write a concise research report grounded only in the supplied evidence packet.",
            "Separate measured observations, interpretations, limitations, and adopted decisions.",
            "Do not convert missing artifacts, interrupted execution, or inconclusive results into negative scientific findings.",
            "Every quantitative or consequential statement must be traceable to a supplied claim_id or artifact path.",
            "Do not include forbidden_claims except to state explicitly that they are not established.",
            "Avoid repeating the same limitation or unsupported claim across multiple sections unless the supplied rules require it.",
            rules_text,
        ]))
        user = "Produce JSON with report_markdown, claims_used, warnings, and unsupported_claims.\n\nEVIDENCE PACKET:\n" + json.dumps(evidence, ensure_ascii=False)
        generated, provenance = provider_request(provider, system, user)
        report = generated.get("report_markdown")
        if not isinstance(report, str) or not report.strip():
            raise ValueError("provider field report_markdown must be a non-empty string")
        claims_used = require_string_list(generated.get("claims_used"), "claims_used")
        warnings = require_string_list(generated.get("warnings"), "warnings")
        unsupported = require_string_list(generated.get("unsupported_claims"), "unsupported_claims")
        atomic_write(output_path, report.strip() + "\n", overwrite)
        output_hash = sha256_bytes(output_path.read_bytes())
        sidecar = {
            "schema_version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "credential_transport": "ssh_stdin_ephemeral",
            "evidence_path": str(evidence_path),
            "evidence_sha256": evidence_hash,
            "rules_path": str(rules_path) if rules_path else None,
            "rules_sha256": rules_hash,
            "output_path": str(output_path),
            "output_sha256": output_hash,
            "claims_used": claims_used,
            "warnings": warnings,
            "unsupported_claims": unsupported,
            "provider": provenance,
        }
        atomic_write(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", overwrite)
        print(json.dumps({
            "written": True,
            "output_path": str(output_path),
            "output_sha256": output_hash,
            "sidecar_path": str(sidecar_path),
            "claims_used_count": len(claims_used),
            "warning_count": len(warnings),
            "unsupported_claim_count": len(unsupported),
            "provider": provenance,
        }, ensure_ascii=False))
        return

    raise ValueError(f"unsupported mode: {mode}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        fail(f"remote report writer failed: {error}")

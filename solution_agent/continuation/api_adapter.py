from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .api_council import validate_api_council_response
from .paths import (
    continuation_api_adapter_invocation_path,
    continuation_api_adapter_mock_response_path,
    continuation_api_adapter_validated_response_path,
    solution_continuation_dir,
)

API_ADAPTER_SCHEMA_VERSION = 1

ALLOWED_PROVIDER_MODE = "mock"

ALLOWED_PROVIDER_FIELDS = ("id", "mode", "enabled", "mode_notes", "config")

FORBIDDEN_MARKER_FIELDS = (
    "api_key",
    "secret",
    "credential",
    "password",
    "token",
)

FORBIDDEN_RESPONSE_TEXT = (
    "api_key",
    "secret",
    "credential",
    "password",
    "token",
    "path_guessing",
    "remote_execution",
    "dispatch",
    "promote",
    "registry_mutation",
    "source_edit",
)

URI_SENSITIVE_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\\\\"),
    re.compile(r"^/"),
    re.compile(r"\.\."),
)

INVOCATION_SIDE_EFFECTS = {
    "dispatch": False,
    "promote": False,
    "registry_mutation": False,
    "remote_execution": False,
    "running_state": False,
    "subprocess": False,
    "scheduler_submit": False,
    "source_edit": False,
    "write_inbox": False,
    "credential_request": False,
    "remote_write": False,
}


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _read_payload(payload: dict | Path | str) -> Dict[str, Any]:
    if isinstance(payload, (Path, str)):
        loaded = json.loads(Path(payload).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("payload must be a JSON object")
        return loaded
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("payload must be a dict, Path, or string path")


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _policy_root() -> Path:
    root = solution_continuation_dir()
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation"
    if root.exists():
        return root
    if fallback.exists():
        return fallback
    return root


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def _policy_file(filename: str) -> Path:
    configured_root = _policy_root()
    configured = configured_root / filename
    if configured.exists():
        return configured
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation" / filename
    if fallback.exists():
        return fallback
    package_root = Path(__file__).resolve().parents[1] / "continuation" / filename
    if package_root.exists():
        return package_root
    return configured


def _load_api_adapter_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_file("api_adapter_policy.yaml"))


def _load_api_provider_allowlist() -> Dict[str, Any]:
    return _load_yaml(_policy_file("api_provider_allowlist.yaml"))


def _provider_enabled(provider: Dict[str, Any] | None) -> bool:
    if provider is None:
        return False
    return _normalize_lower(provider.get("enabled", True)) not in {"", "false", "0", "no", "off"}


def _provider_mode(provider: Dict[str, Any] | None) -> str:
    if not provider:
        return ""
    return _normalize_lower(provider.get("mode", "")) or "unknown"


def _provider_by_id(provider_id: str, allowlist: Dict[str, Any]) -> Dict[str, Any] | None:
    providers = allowlist.get("providers")
    if providers is None:
        providers = allowlist.get("allowed_providers")
    if isinstance(providers, dict):
        provider = providers.get(provider_id)
        if isinstance(provider, dict):
            return provider
        return None
    if isinstance(providers, list):
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            if _normalize_lower(entry.get("provider_id") or entry.get("id")) == _normalize_lower(provider_id):
                return entry
    return None


def _looks_like_forbidden_text(text: str) -> bool:
    normalized = _normalize_lower(text)
    if not normalized:
        return False
    for marker in FORBIDDEN_RESPONSE_TEXT:
        if marker in normalized:
            return True
    return False


def _looks_like_secret_key(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = _normalize_lower(value)
    if not normalized:
        return False
    if any(marker in normalized for marker in FORBIDDEN_MARKER_FIELDS):
        return True
    return any(pattern.search(normalized) for pattern in URI_SENSITIVE_PATH_PATTERNS)


def _looks_like_secret_key_name(name: str) -> bool:
    normalized = _normalize_lower(name)
    if not normalized:
        return False
    return any(marker in normalized for marker in FORBIDDEN_MARKER_FIELDS)


def _remove_secret_keys(payload: Any) -> Any:
    if isinstance(payload, dict):
        output = {}
        for key, value in payload.items():
            if _looks_like_secret_key_name(str(key)):
                continue
            output[key] = _remove_secret_keys(value)
        return output
    if isinstance(payload, list):
        return [_remove_secret_keys(item) for item in payload]
    return payload


def _payload_for_replay(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload)
    for field in ("created", "replayed", "invocation_path", "mock_response_path", "validated_response_path", "status"):
        normalized.pop(field, None)
    return normalized


def _has_forbidden_response_marker(payload: Any) -> bool:
    if payload is None:
        return False

    if isinstance(payload, str):
        return _looks_like_forbidden_text(payload)
    if isinstance(payload, (int, float, bool)):
        return False

    if isinstance(payload, list):
        return any(_has_forbidden_response_marker(item) for item in payload)

    if isinstance(payload, dict):
        for key, value in payload.items():
            if _looks_like_secret_key_name(str(key)):
                return True
            if _has_forbidden_response_marker(value):
                return True
        return False
    return False


def _build_response_seed(invocation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": API_ADAPTER_SCHEMA_VERSION,
        "council_id": _normalize(invocation.get("source_council_id")),
        "interpretation_votes": [],
        "risk_flags": [],
        "next_step_candidates": [],
        "disagreement_summary": "No-op mock response placeholder",
        "status": "valid",
    }


def build_api_adapter_invocation_dry_run(
    council_request: dict | Path,
    *,
    provider_id: str,
    actor: str = "system",
) -> dict:
    """Build a deterministic local-only API adapter invocation."""

    request = _read_payload(council_request)
    actor_norm = _normalize(actor) or "system"
    provider_id_norm = _normalize(provider_id)
    source_council_id = _normalize(request.get("council_id"))
    source_request_hash = _sha256_hex(_payload_for_replay(request))
    input_knowledge_subset_hash = _normalize(request.get("knowledge_subset_hash"))
    allowed_outputs = request.get("allowed_outputs")
    forbidden_actions = request.get("forbidden_actions")

    policy = _load_api_adapter_policy()
    allowlist = _load_api_provider_allowlist()
    provider = _provider_by_id(provider_id_norm, allowlist)
    provider_mode = _provider_mode(provider)

    reasons: List[str] = []
    if not source_council_id:
        reasons.append("missing_source_council_id")
    if not provider_id_norm:
        reasons.append("missing_provider_id")
    if _normalize(request.get("status")) != "request_created":
        reasons.append("source_request_not_ready")

    if provider is None:
        reasons.append("provider_not_allowlisted")
    else:
        if not _provider_enabled(provider):
            reasons.append("provider_disabled")
        if provider_mode != ALLOWED_PROVIDER_MODE:
            reasons.append("provider_mode_not_mock")

    if not isinstance(allowed_outputs, list):
        reasons.append("invalid_allowed_outputs")
        allowed_outputs = []
    if not isinstance(forbidden_actions, list):
        reasons.append("invalid_forbidden_actions")
        forbidden_actions = []

    if not reasons:
        invocation_seed = {
            "source_council_id": source_council_id,
            "provider_id": provider_id_norm,
            "provider_mode": provider_mode,
            "actor": actor_norm,
            "source_request_hash": source_request_hash,
        }
        invocation_id = f"aa_{_sha256_hex(invocation_seed)[:16]}"
        idempotency_key = f"aa_{_sha256_hex(invocation_seed)[:24]}"
    else:
        invocation_id = f"aa_{_sha256_hex({'actor': actor_norm, 'provider_id': provider_id_norm})[:16]}"
        idempotency_key = f"aa_{_sha256_hex({'actor': actor_norm, 'provider_id': provider_id_norm, 'reason': '|'.join(sorted(set(reasons)))})[:24]}"

    invocation = {
        "schema_version": API_ADAPTER_SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "idempotency_key": idempotency_key,
        "provider_id": provider_id_norm,
        "provider_mode": provider_mode if provider_mode else ALLOWED_PROVIDER_MODE,
        "actor": actor_norm,
        "source_council_id": source_council_id,
        "source_request_hash": source_request_hash,
        "input_knowledge_subset_hash": input_knowledge_subset_hash,
        "allowed_outputs": list(allowed_outputs),
        "forbidden_actions": list(forbidden_actions),
        "network_enabled": bool(policy.get("network_enabled", False)),
        "api_call_executed": False,
        "status": "invocation_created" if not reasons else "blocked",
        "side_effects": dict(INVOCATION_SIDE_EFFECTS),
        "reasons": sorted(set(reasons)),
    }

    if not invocation["network_enabled"]:
        invocation["network_enabled"] = False
    return invocation


def invoke_mock_api_provider(
    invocation: dict | Path,
    *,
    mock_response: dict | None = None,
) -> dict:
    """Run a mock adapter invocation without any network calls."""

    invocation_payload = _read_payload(invocation)
    source_council_id = _normalize(invocation_payload.get("source_council_id"))
    reasons: List[str] = []
    validation: Dict[str, Any] = {"status": "rejected", "reasons": ["invalid_invocation"]}

    provider_id = _normalize(invocation_payload.get("provider_id"))
    provider_mode = _normalize_lower(invocation_payload.get("provider_mode"))
    provider_cfg = _provider_by_id(provider_id, _load_api_provider_allowlist()) if provider_id else None
    if _normalize(invocation_payload.get("status")) != "invocation_created":
        reasons.append("invocation_not_created")
    if provider_mode != ALLOWED_PROVIDER_MODE:
        reasons.append("provider_mode_not_mock")
    if not _provider_enabled(provider_cfg):
        reasons.append("provider_not_enabled")
    if _normalize(invocation_payload.get("source_council_id")) == "":
        reasons.append("missing_source_council_id")
    if bool(invocation_payload.get("api_call_executed")):
        reasons.append("api_call_already_executed")
    if bool(invocation_payload.get("network_enabled")):
        reasons.append("network_enabled_not_allowed")

    if reasons:
        return {
            **invocation_payload,
            "provider_response": {},
            "provider_response_validation": validation,
            "status": "blocked",
            "reasons": sorted(set(reasons)),
            "called": False,
            "mock_mode": True,
        }

    if mock_response is None:
        response = _build_response_seed(invocation_payload)
    else:
        response = _remove_secret_keys(mock_response)

    if not isinstance(response, dict):
        reasons.append("mock_response_not_mapping")
        response = {}

    if _has_forbidden_response_marker(response):
        reasons.append("mock_response_contains_forbidden_material")

    # Adapter does not execute this call remotely
    response_invocation = validate_api_council_response(response, request={"council_id": source_council_id})
    reasons.extend([reason for reason in response_invocation.get("reasons", []) if reason])

    if reasons:
        status = "rejected"
    else:
        status = "response_generated"

    output = {
        **invocation_payload,
        "provider_response": response,
        "provider_response_validation": response_invocation,
        "status": status,
        "called": False,
        "mock_mode": True,
        "reasons": sorted(set(reasons)),
    }
    return output


def validate_api_adapter_output(adapter_output: dict | Path, *, council_request: dict | Path | None = None) -> dict:
    """Validate a mocked adapter output before writing artifacts."""

    payload = _read_payload(adapter_output)
    request = _read_payload(council_request) if council_request is not None else None
    output = dict(payload)
    reasons: List[str] = []
    invocation_id = _normalize(output.get("invocation_id"))
    source_council_id = _normalize(output.get("source_council_id"))
    provider_id = _normalize(output.get("provider_id"))
    provider_mode = _normalize_lower(output.get("provider_mode"))
    network_enabled = bool(output.get("network_enabled"))
    api_call_executed = bool(output.get("api_call_executed"))
    side_effects = output.get("side_effects")
    provider_response = output.get("provider_response")

    if not invocation_id:
        reasons.append("missing_invocation_id")
    if _normalize(output.get("schema_version")) != str(API_ADAPTER_SCHEMA_VERSION):
        reasons.append("invalid_schema_version")
    if not source_council_id:
        reasons.append("missing_source_council_id")
    if not provider_id:
        reasons.append("missing_provider_id")
    if provider_mode != ALLOWED_PROVIDER_MODE:
        reasons.append("provider_mode_not_mock")
    if network_enabled:
        reasons.append("network_enabled_not_allowed")
    if api_call_executed:
        reasons.append("api_call_executed_forbidden")
    if not isinstance(provider_response, dict):
        reasons.append("provider_response_not_mapping")
        provider_response = {}

    if not isinstance(side_effects, dict):
        reasons.append("side_effects_missing_or_invalid")
        side_effects = dict(INVOCATION_SIDE_EFFECTS)

    for key, expected in INVOCATION_SIDE_EFFECTS.items():
        if bool(side_effects.get(key, expected)):
            reasons.append(f"side_effects_not_false:{key}")
            break

    provider_validation = validate_api_council_response(
        provider_response,
        request={"council_id": request.get("council_id") if request else source_council_id},
    )
    if provider_validation.get("status") != "valid":
        reasons.extend(str(reason) for reason in provider_validation.get("reasons", []))

    if request is not None:
        request_council_id = _normalize(request.get("council_id"))
        if request_council_id and request_council_id != source_council_id:
            reasons.append("source_council_id_mismatch")
        request_allowed_outputs = request.get("allowed_outputs")
        request_forbidden_actions = request.get("forbidden_actions")
        if isinstance(request_allowed_outputs, list) and _normalize(output.get("allowed_outputs")):
            pass
        if isinstance(request_forbidden_actions, list) and output.get("forbidden_actions") is not None:
            pass

    result = {
        "schema_version": API_ADAPTER_SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "source_council_id": source_council_id,
        "provider_id": provider_id,
        "provider_mode": provider_mode,
        "network_enabled": False,
        "api_call_executed": False,
        "status": "valid" if not reasons else "rejected",
        "reasons": sorted(set(reasons)),
        "provider_response": provider_response,
        "provider_response_validation": provider_validation,
    }
    return result


def write_api_adapter_artifacts_dry_run(adapter_output: dict, *, invocation_id: str | None = None) -> dict:
    """Write local adapter invocation + mock/validated response artifacts."""

    payload = _read_payload(adapter_output)
    validation = validate_api_adapter_output(payload)
    resolved_invocation_id = _normalize(invocation_id or payload.get("invocation_id"))
    output = {
        **payload,
        "validation": validation,
    }
    if resolved_invocation_id == "":
        output.update(
            {
                "status": validation["status"],
                "created": False,
                "replayed": False,
                "invocation_path": "",
                "mock_response_path": "",
                "validated_response_path": "",
                "reasons": validation.get("reasons", []),
            }
        )
        return output

    invocation_path = continuation_api_adapter_invocation_path(resolved_invocation_id)
    mock_response_path = continuation_api_adapter_mock_response_path(resolved_invocation_id)
    validated_response_path = continuation_api_adapter_validated_response_path(resolved_invocation_id)
    invocation_path.parent.mkdir(parents=True, exist_ok=True)

    if validation.get("status") != "valid":
        return {
            **output,
            "status": validation["status"],
            "created": False,
            "replayed": False,
            "invocation_path": str(invocation_path),
            "mock_response_path": str(mock_response_path),
            "validated_response_path": str(validated_response_path),
        }

    invocation_payload = {
        "schema_version": API_ADAPTER_SCHEMA_VERSION,
        "invocation_id": resolved_invocation_id,
        "idempotency_key": _normalize(payload.get("idempotency_key")),
        "provider_id": _normalize(payload.get("provider_id")),
        "provider_mode": _normalize_lower(payload.get("provider_mode")),
        "actor": _normalize(payload.get("actor")),
        "source_council_id": _normalize(payload.get("source_council_id")),
        "source_request_hash": _normalize(payload.get("source_request_hash")),
        "input_knowledge_subset_hash": _normalize(payload.get("input_knowledge_subset_hash")),
        "allowed_outputs": payload.get("allowed_outputs", []),
        "forbidden_actions": payload.get("forbidden_actions", []),
        "network_enabled": False,
        "api_call_executed": False,
        "status": "invocation_created",
        "side_effects": dict(INVOCATION_SIDE_EFFECTS),
    }

    mock_response = _remove_secret_keys(payload.get("provider_response", {}))
    validated_response = {
        "schema_version": API_ADAPTER_SCHEMA_VERSION,
        "invocation_id": resolved_invocation_id,
        "source_council_id": _normalize(payload.get("source_council_id")),
        "validation": validation,
    }

    existing_invocation = _read_existing_json(invocation_path)
    existing_mock = _read_existing_json(mock_response_path)
    existing_validation = _read_existing_json(validated_response_path)

    artifacts_exist = any(
        path.exists() for path in (invocation_path, mock_response_path, validated_response_path)
    )
    all_artifacts_exist = all(
        path.exists() for path in (invocation_path, mock_response_path, validated_response_path)
    )

    if artifacts_exist:
        if not all_artifacts_exist:
            return {
                **output,
                "status": validation["status"],
                "created": False,
                "replayed": False,
                "invocation_path": str(invocation_path),
                "mock_response_path": str(mock_response_path),
                "validated_response_path": str(validated_response_path),
                "reasons": sorted(set(list(validation.get("reasons", [])) + ["artifact_set_not_consistent"])),
            }

        if (
            existing_invocation == invocation_payload
            and existing_mock == mock_response
            and existing_validation == validated_response
        ):
            return {
                **output,
                "status": validation["status"],
                "created": False,
                "replayed": True,
                "invocation_path": str(invocation_path),
                "mock_response_path": str(mock_response_path),
                "validated_response_path": str(validated_response_path),
            }
        return {
            **output,
            "status": "rejected",
            "created": False,
            "replayed": False,
            "invocation_path": str(invocation_path),
            "mock_response_path": str(mock_response_path),
            "validated_response_path": str(validated_response_path),
            "reasons": sorted(set(list(validation.get("reasons", [])) + ["idempotency_collision"])),
        }

    invocation_path.write_text(_canonical_json(invocation_payload) + "\n", encoding="utf-8")
    mock_response_path.write_text(_canonical_json(mock_response) + "\n", encoding="utf-8")
    validated_response_path.write_text(_canonical_json(validated_response) + "\n", encoding="utf-8")

    return {
        **output,
        "status": validation["status"],
        "created": True,
        "replayed": False,
        "invocation_path": str(invocation_path),
        "mock_response_path": str(mock_response_path),
        "validated_response_path": str(validated_response_path),
    }


__all__ = [
    "build_api_adapter_invocation_dry_run",
    "invoke_mock_api_provider",
    "validate_api_adapter_output",
    "write_api_adapter_artifacts_dry_run",
]

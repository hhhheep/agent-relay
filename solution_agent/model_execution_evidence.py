from __future__ import annotations

from typing import Any


EVIDENCE_SCOPES = {"formal", "bounded_probe", "proxy_probe", "replay_only"}
FORMAL_CLAIM_MARKERS = (
    "formal validation",
    "formally validated",
    "formal evidence",
    "proves",
    "proved",
    "validated the hypothesis",
)


def extract_model_execution_evidence(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    nested = payload.get("model_execution_evidence")
    if isinstance(nested, dict):
        return dict(nested)
    keys = {
        "requested_model",
        "executed_model",
        "fallback_reason",
        "proxy_only",
        "formal_evidence",
        "allowed_claims",
        "forbidden_claims",
        "evidence_scope",
    }
    if any(key in payload for key in keys):
        return {key: payload.get(key) for key in keys if key in payload}
    return None


def normalize_model_execution_evidence(payload: dict[str, Any] | None) -> dict[str, Any]:
    evidence = extract_model_execution_evidence(payload) or {}
    requested = str(evidence.get("requested_model") or "").strip()
    executed = str(evidence.get("executed_model") or "").strip()
    fallback_reason = evidence.get("fallback_reason")
    scope = str(evidence.get("evidence_scope") or "").strip()
    proxy_only = evidence.get("proxy_only")
    formal_evidence = evidence.get("formal_evidence")
    if proxy_only is None and requested and executed and requested != executed:
        proxy_only = True
    if formal_evidence is None and proxy_only is True:
        formal_evidence = False
    return {
        "requested_model": requested,
        "executed_model": executed,
        "fallback_reason": None if fallback_reason in ("", None) else str(fallback_reason),
        "proxy_only": proxy_only,
        "formal_evidence": formal_evidence,
        "allowed_claims": _string_list(evidence.get("allowed_claims")),
        "forbidden_claims": _string_list(evidence.get("forbidden_claims")),
        "evidence_scope": scope,
    }


def validate_model_execution_evidence(payload: dict[str, Any] | None) -> list[str]:
    raw = extract_model_execution_evidence(payload) or {}
    evidence = normalize_model_execution_evidence(payload)
    errors: list[str] = []
    if not evidence["requested_model"]:
        errors.append("model_execution_evidence.requested_model is required")
    if not evidence["executed_model"]:
        errors.append("model_execution_evidence.executed_model is required")
    if raw.get("proxy_only") is not True and raw.get("proxy_only") is not False:
        errors.append("model_execution_evidence.proxy_only must be boolean")
    if raw.get("formal_evidence") is not True and raw.get("formal_evidence") is not False:
        errors.append("model_execution_evidence.formal_evidence must be boolean")
    if evidence["evidence_scope"] not in EVIDENCE_SCOPES:
        errors.append(
            "model_execution_evidence.evidence_scope must be one of "
            + ", ".join(sorted(EVIDENCE_SCOPES))
        )
    models_differ = bool(
        evidence["requested_model"]
        and evidence["executed_model"]
        and evidence["requested_model"] != evidence["executed_model"]
    )
    if models_differ and raw.get("proxy_only") is not True:
        errors.append("requested_model != executed_model requires proxy_only=true")
    if raw.get("proxy_only") is True and raw.get("formal_evidence") is not False:
        errors.append("proxy_only=true requires formal_evidence=false")
    if evidence["formal_evidence"] is True and evidence["evidence_scope"] != "formal":
        errors.append("formal_evidence=true requires evidence_scope=formal")
    if evidence["proxy_only"] is True and evidence["evidence_scope"] == "formal":
        errors.append("proxy_only=true cannot use evidence_scope=formal")
    return errors


def validate_proxy_claim_text(evidence: dict[str, Any] | None, *values: Any) -> list[str]:
    normalized = normalize_model_execution_evidence(evidence)
    if not (normalized["proxy_only"] is True or normalized["formal_evidence"] is False):
        return []
    text = "\n".join(_walk_strings(list(values))).lower()
    errors: list[str] = []
    for marker in FORMAL_CLAIM_MARKERS:
        if marker in text:
            errors.append(f"proxy/non-formal evidence cannot claim `{marker}`")
    return errors


def proxy_fallback_warning(evidence: dict[str, Any] | None) -> bool:
    normalized = normalize_model_execution_evidence(evidence)
    return bool(
        normalized["proxy_only"] is True
        or (
            normalized["requested_model"]
            and normalized["executed_model"]
            and normalized["requested_model"] != normalized["executed_model"]
        )
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_walk_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_walk_strings(item))
        return out
    return []

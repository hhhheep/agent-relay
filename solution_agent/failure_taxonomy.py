from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_TAXONOMY_PATH = Path(__file__).parent / "orchestrator" / "failure_taxonomy.yaml"


def load_failure_taxonomy(path: Path = _TAXONOMY_PATH) -> dict:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"failure_types": {}}
    return payload if isinstance(payload, dict) else {"failure_types": {}}


def failure_contract(failure_type: str | None, taxonomy: dict | None = None) -> dict:
    if not failure_type:
        return {}
    data = taxonomy if isinstance(taxonomy, dict) else load_failure_taxonomy()
    entries = data.get("failure_types", {})
    if not isinstance(entries, dict):
        return {}
    entry = entries.get(failure_type, {})
    return entry if isinstance(entry, dict) else {}


def recommended_action_for_failure(
    failure_type: str | None,
    *,
    message: str = "",
    taxonomy: dict | None = None,
) -> dict[str, Any]:
    if not failure_type:
        return {"type": "no_retry", "message": message or "No failure."}
    contract = failure_contract(failure_type, taxonomy)
    action = contract.get("recommended_action", {}) if isinstance(contract, dict) else {}
    if isinstance(action, dict) and action:
        result = dict(action)
    elif contract.get("user_action_required"):
        result = {"type": "request_approval", "message": f"User action required: {failure_type}."}
    elif contract.get("codex_recommended"):
        result = {"type": "escalate_to_codex_debug", "message": f"Codex debug recommended: {failure_type}."}
    elif contract.get("repairable"):
        result = {"type": "repair_plan", "message": f"Repair the execution plan: {failure_type}."}
    else:
        result = {"type": "no_retry", "message": f"No automatic retry recommended: {failure_type}."}
    if message:
        result.setdefault("message", message)
    return result


def failure_judge(
    failure_type: str | None,
    *,
    message: str = "",
    taxonomy: dict | None = None,
) -> dict[str, Any]:
    contract = failure_contract(failure_type, taxonomy)
    return {
        "failure_type": failure_type,
        "repairable": bool(contract.get("repairable", False)),
        "user_action_required": bool(contract.get("user_action_required", False)),
        "codex_recommended": bool(contract.get("codex_recommended", False)),
        "execution_success": bool(contract.get("execution_success", False)),
        "warn_only": bool(contract.get("warn_only", False)),
        "recommended_action": recommended_action_for_failure(failure_type, message=message, taxonomy=taxonomy),
    }

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def failure_fingerprint(
    *,
    error_type: str,
    command_sha256: str,
    declared_input_fingerprint: str,
    validator_version: str,
    stable_details: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable identity for deciding whether a retry is genuinely new."""

    payload = {
        "command_sha256": command_sha256,
        "declared_input_fingerprint": declared_input_fingerprint,
        "error_type": error_type,
        "stable_details": dict(stable_details or {}),
        "validator_version": validator_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

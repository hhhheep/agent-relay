from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Tuple


REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS: Dict[str, bool] = {
    "ssh": False,
    "remote_write": False,
    "scheduler_dispatch": False,
    "registry_mutation": False,
    "running_state": False,
    "subprocess": False,
    "remote_execution": False,
}


def normalize_boundary_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def collect_required_text_fields(
    payload: Mapping[str, Any],
    fields: Iterable[str],
) -> Tuple[Dict[str, str], List[str]]:
    values = {field: normalize_boundary_text(payload.get(field)) for field in fields}
    missing = [field for field, value in values.items() if not value]
    return values, missing


def assess_disabled_side_effects(
    payload: Mapping[str, Any],
    *,
    reason_code: str,
    side_effects_key: str = "side_effects",
    expected: Mapping[str, bool] | None = None,
    check_id: str = "side_effects_disabled",
) -> Tuple[List[str], Dict[str, Any]]:
    expected_effects = dict(expected or REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS)
    raw_side_effects = payload.get(side_effects_key)
    if not isinstance(raw_side_effects, Mapping):
        return [reason_code], {
            "id": check_id,
            "status": "failed",
            "reason": "side_effects_missing_or_invalid",
            "expected": expected_effects,
        }

    mismatches = [
        key
        for key, expected_value in expected_effects.items()
        if bool(raw_side_effects.get(key, False)) != expected_value
    ]
    return (
        [reason_code] if mismatches else [],
        {
            "id": check_id,
            "status": "passed" if not mismatches else "failed",
            "mismatches": mismatches,
            "expected": expected_effects,
        },
    )


__all__ = [
    "REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS",
    "assess_disabled_side_effects",
    "collect_required_text_fields",
    "normalize_boundary_text",
]

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .telemetry_contracts import ToolManifest


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_tool_manifest(tools: Iterable[Any]) -> ToolManifest:
    canonical_tools: list[dict[str, Any]] = []
    names: list[str] = []
    for tool in tools:
        if hasattr(tool, "model_dump"):
            payload = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(tool, dict):
            payload = dict(tool)
        else:
            raise TypeError(f"unsupported tool descriptor: {type(tool).__name__}")
        name = str(payload.get("name") or "")
        if not name:
            raise ValueError("tool descriptor is missing name")
        names.append(name)
        canonical_tools.append(payload)
    encoded = canonical_json_bytes(canonical_tools)
    return ToolManifest(
        tool_count=len(canonical_tools),
        canonical_schema_bytes=len(encoded),
        canonical_schema_sha256=hashlib.sha256(encoded).hexdigest(),
        ordered_tool_names=tuple(names),
    )


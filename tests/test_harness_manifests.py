from __future__ import annotations

import asyncio

import pytest
from mcp import Client
from pydantic import ValidationError

from solution_agent.harness.manifests import build_tool_manifest
from solution_agent.harness.mcp_server import mcp
from solution_agent.harness.release_identity import CANONICAL_TOOL_NAMES
from solution_agent.harness.telemetry_contracts import ContextManifest, ContextManifestItem


EXPECTED_TOOLS = CANONICAL_TOOL_NAMES


def _live_tools():
    async def collect():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    return asyncio.run(collect())


def test_live_tool_manifest_is_stable_and_exactly_canonical_tools() -> None:
    tools = _live_tools()
    first = build_tool_manifest(tools)
    second = build_tool_manifest(tools)
    assert first == second
    assert first.tool_count == 11
    assert first.ordered_tool_names == EXPECTED_TOOLS
    assert first.canonical_schema_bytes > 0
    assert len(first.canonical_schema_sha256) == 64


def test_tool_manifest_rejects_duplicate_names() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        build_tool_manifest([{"name": "same"}, {"name": "same"}])


def test_context_manifest_requires_hashes_and_exact_totals() -> None:
    selected = ContextManifestItem(
        name="decision",
        retention="pinned_exact",
        included=True,
        source_bytes=100,
        selected_bytes=100,
        estimated_tokens=25,
        source_sha256="a" * 64,
        selected_sha256="a" * 64,
        inclusion_reason="required decision contract",
    )
    omitted = ContextManifestItem(
        name="raw-log",
        retention="omittable",
        included=False,
        source_bytes=10000,
        selected_bytes=0,
        estimated_tokens=0,
        source_sha256="b" * 64,
        omission_reason="receipt is sufficient",
    )
    manifest = ContextManifest(
        run_id="run-1",
        max_estimated_tokens=100,
        selected_estimated_tokens=25,
        estimator="conservative-cjk-v1",
        items=(selected, omitted),
    )
    assert manifest.selected_estimated_tokens == 25

    with pytest.raises(ValidationError, match="selected_sha256"):
        ContextManifestItem(
            name="bad",
            retention="pinned_exact",
            included=True,
            source_bytes=1,
            selected_bytes=1,
            estimated_tokens=1,
            source_sha256="c" * 64,
        )
    with pytest.raises(ValidationError, match="omitted context"):
        ContextManifestItem(
            name="bad",
            retention="omittable",
            included=False,
            source_bytes=1,
            selected_bytes=1,
            estimated_tokens=0,
            source_sha256="d" * 64,
            selected_sha256="d" * 64,
            omission_reason="omitted",
        )
    with pytest.raises(ValidationError, match="does not match"):
        ContextManifest(
            run_id="run-1",
            max_estimated_tokens=100,
            selected_estimated_tokens=24,
            estimator="conservative-cjk-v1",
            items=(selected,),
        )

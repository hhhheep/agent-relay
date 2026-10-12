from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation import api_council
from solution_agent.continuation.knowledge_pack import (
    build_knowledge_pack_manifest_dry_run,
    project_understanding_protocol_metadata,
    select_remote_safe_knowledge_subset,
    update_knowledge_pack_latest_pointer_dry_run,
    validate_knowledge_pack_manifest,
    write_knowledge_pack_manifest_dry_run,
    write_remote_safe_subset_dry_run,
)
from solution_agent.continuation import ledger as continuation_ledger

import pytest


CANONICAL_SECTIONS = [
    "project_brief",
    "chronology",
    "experiment_lines",
    "workplace_profiles",
    "capability_inventory",
    "result_index",
    "decision_log",
    "failure_taxonomy",
    "action_requests",
]


POLICY_PATHS = {
    "knowledge_pack_policy": Path(".solution/continuation/knowledge_pack_policy.yaml"),
    "knowledge_pack_schema": Path(".solution/continuation/knowledge_pack_schema.yaml"),
    "knowledge_pack_manifest_schema": Path(".solution/continuation/knowledge_pack_manifest_schema.yaml"),
    "run_ledger_schema": Path(".solution/continuation/run_ledger_schema.yaml"),
}


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _base_items(actor: str = "system", line: str = "L1", workplace_profile: str = "line-a") -> list[dict]:
    return [
        {
            "section_id": section,
            "classification": "remote_safe",
            "path": f"knowledge/{section}.json",
            "workplace_profile": workplace_profile,
            "line": line,
        }
        for section in CANONICAL_SECTIONS
    ]


def _section(items: list[dict], section_id: str) -> dict:
    for item in items:
        if item["section_id"] == section_id:
            return item
    raise AssertionError(f"missing section {section_id}")


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} should contain a YAML map")
    return payload


def test_manifest_build_with_all_canonical_sections_succeeds(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())

    assert manifest["status"] == "ready"
    assert manifest["pack_id"].startswith("kp_")
    assert len(manifest["sections"]) == len(CANONICAL_SECTIONS)
    assert [section["section_id"] for section in manifest["sections"]] == sorted(CANONICAL_SECTIONS)
    assert not manifest["action_requests"]
    assert manifest["understanding_protocol"]["protocol_id"] == "project_understanding_protocol_v0"
    assert manifest["understanding_protocol"]["core_rule"] == "taskbook_is_seed_not_line_state"


def test_validate_missing_canonical_sections_reports_needs_update(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()[:-1]
    manifest = build_knowledge_pack_manifest_dry_run(items)

    assert manifest["status"] == "needs_update"
    assert manifest["missing_sections"] == ["action_requests"]
    assert any(item["type"] == "needs_update" for item in manifest["action_requests"])


def test_write_manifest_creates_only_expected_path(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())
    written = write_knowledge_pack_manifest_dry_run(manifest)

    assert written["created"] is True
    assert written["replayed"] is False
    path = Path(written["manifest_path"])
    assert path == tmp_path / "continuation" / "knowledge_packs" / manifest["pack_id"] / "manifest.json"
    assert path.exists()
    assert list(path.parent.iterdir()) == [path]


def test_manifest_replay_is_idempotent(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())
    first = write_knowledge_pack_manifest_dry_run(manifest)
    assert first["created"] is True
    second = write_knowledge_pack_manifest_dry_run(manifest)

    assert second["created"] is False
    assert second["replayed"] is True
    assert second["manifest_path"] == first["manifest_path"]


def test_manifest_same_pack_id_different_content_fails_closed(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())
    first = write_knowledge_pack_manifest_dry_run(manifest)
    assert first["created"] is True

    changed = json.loads(json.dumps(manifest))
    changed["sections"][0]["path"] = "knowledge/project_brief-alt.json"
    blocked = write_knowledge_pack_manifest_dry_run(changed, pack_id=manifest["pack_id"])

    assert blocked["status"] == "blocked"
    assert any(reason == "idempotency_collision" for reason in blocked["reasons"])
    assert blocked["created"] is False
    assert blocked["replayed"] is False


def test_remote_safe_subset_filters_local_only_and_stale_entries(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()
    _section(items, "experiment_lines")["classification"] = "local_only"
    _section(items, "workplace_profiles")["classification"] = "remote_safe"
    _section(items, "workplace_profiles")["stale_or_expired"] = True

    manifest = build_knowledge_pack_manifest_dry_run(items)
    subset = select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")

    remote_ids = {entry["section_id"] for entry in subset["remote_sections"]}
    assert "project_brief" in remote_ids
    assert "experiment_lines" not in remote_ids
    assert "workplace_profiles" not in remote_ids
    assert len(remote_ids) == len(CANONICAL_SECTIONS) - 2


def test_remote_safe_subset_filters_workplace_and_line_specific_entries(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()
    _section(items, "project_brief")["classification"] = "workplace_specific"
    _section(items, "project_brief")["workplace_profile"] = "line-b"
    _section(items, "experiment_lines")["classification"] = "line_specific"
    _section(items, "experiment_lines")["line"] = "line-2"

    manifest = build_knowledge_pack_manifest_dry_run(items)
    subset = select_remote_safe_knowledge_subset(manifest, line="line-1", workplace_profile="line-a")
    remote_ids = [entry["section_id"] for entry in subset["remote_sections"]]

    assert "project_brief" not in remote_ids
    assert "experiment_lines" not in remote_ids
    assert "decision_log" in remote_ids
    assert all(entry["section_id"] not in {"project_brief", "experiment_lines"} for entry in subset["remote_sections"])
    assert subset["understanding_protocol"]["authority_order"][0] == "explicit_current_user_instruction"


def test_write_subset_only_creates_remote_safe_subset_file(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()
    items[1]["classification"] = "local_only"
    manifest = build_knowledge_pack_manifest_dry_run(items)
    subset = select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")

    written = write_remote_safe_subset_dry_run(subset, pack_id=manifest["pack_id"])
    path = Path(written["remote_safe_subset_path"])

    assert written["created"] is True
    assert path == tmp_path / "continuation" / "knowledge_packs" / manifest["pack_id"] / "remote_safe_subset.json"
    assert path.exists()
    assert list(path.parent.iterdir()) == [path]


def test_latest_pointer_update_is_local_only(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())
    write_knowledge_pack_manifest_dry_run(manifest)

    pointer = update_knowledge_pack_latest_pointer_dry_run(manifest["pack_id"], actor="system")
    path = Path(pointer["latest_pointer_path"])

    assert pointer["status"] == "ready"
    assert pointer["created"] is True
    assert pointer["replayed"] is False
    assert pointer["pack_id"] == manifest["pack_id"]
    assert pointer["actor"] == "system"
    assert path == tmp_path / "continuation" / "knowledge_packs" / "latest.json"
    assert path.exists()


def test_secret_looking_path_is_rejected_or_excluded(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()
    items[0]["path"] = "secrets/api_key_dump.json"

    manifest = build_knowledge_pack_manifest_dry_run(items)
    assert manifest["status"] == "blocked"
    assert "unsafe_or_sensitive_reference" in manifest["reasons"]

    written = write_knowledge_pack_manifest_dry_run(manifest)
    assert written["status"] == "blocked"

    subset = select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")
    assert subset["status"] in {"blocked", "ready"}
    if subset["remote_sections"]:
        assert all("api_key" not in (item.get("path") or item.get("content_ref")) for item in subset["remote_sections"])


def test_no_subprocess_network_api_calls(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    items = _base_items()
    manifest = build_knowledge_pack_manifest_dry_run(items)
    subset = select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")

    with (
        patch("solution_agent.continuation.knowledge_pack.subprocess.run") as run_mock,
        patch("solution_agent.continuation.knowledge_pack.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.knowledge_pack.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.knowledge_pack.socket.create_connection") as create_connection_mock,
        patch("solution_agent.continuation.api_council.subprocess.run") as run_mock2,
        patch("solution_agent.continuation.api_council.subprocess.check_output") as check_output_mock2,
        patch("solution_agent.continuation.api_council.subprocess.Popen") as popen_mock2,
        patch("solution_agent.continuation.api_council.socket.create_connection") as create_connection_mock2,
    ):
        run_mock.side_effect = AssertionError("run should be blocked")
        check_output_mock.side_effect = AssertionError("check_output should be blocked")
        popen_mock.side_effect = AssertionError("popen should be blocked")
        create_connection_mock.side_effect = AssertionError("network should be blocked")
        run_mock2.side_effect = AssertionError("run should be blocked")
        check_output_mock2.side_effect = AssertionError("check_output should be blocked")
        popen_mock2.side_effect = AssertionError("popen should be blocked")
        create_connection_mock2.side_effect = AssertionError("network should be blocked")

        _ = validate_knowledge_pack_manifest(manifest)
        _ = select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")
        api_council.build_api_council_request_dry_run(manifest, actor="planner", workplace_profile="line-a", line="L1")

    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called
    assert not run_mock2.called
    assert not check_output_mock2.called
    assert not popen_mock2.called
    assert not create_connection_mock2.called


def test_contracts_and_optional_knowledge_pack_ledger_stage(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    for path in POLICY_PATHS.values():
        assert path.exists(), f"missing contract artifact: {path}"

    knowledge_pack_schema = _load_yaml(POLICY_PATHS["knowledge_pack_schema"])
    knowledge_pack_policy = _load_yaml(POLICY_PATHS["knowledge_pack_policy"])
    run_ledger_schema = _load_yaml(POLICY_PATHS["run_ledger_schema"])
    assert set(knowledge_pack_schema["required_section_list"]) == set(CANONICAL_SECTIONS)
    assert "knowledge_pack" in run_ledger_schema["allowed_stages"]
    assert "section_id" in knowledge_pack_schema["required_section_fields"]
    assert "understanding_protocol" in knowledge_pack_schema["optional_fields"]
    assert "understanding_protocol" in knowledge_pack_policy["required_local_fields"]
    assert knowledge_pack_policy["understanding_protocol"]["protocol_id"] == "project_understanding_protocol_v0"

    result = continuation_ledger.append_continuation_run_event(
        run_id="kp_ledger_test",
        stage="knowledge_pack",
        status="succeeded",
        actor="system",
        idempotency_key="kp_ledger_test:ready",
        source_task_id="task_001",
    )

    assert result["path"] == str(tmp_path / "continuation" / "run_ledger" / "kp_ledger_test.jsonl")
    assert result["written"] is True or result["replayed"] is True


def test_project_understanding_protocol_metadata_matches_agent_protocol() -> None:
    protocol = project_understanding_protocol_metadata()

    assert protocol["protocol_id"] == "project_understanding_protocol_v0"
    assert protocol["core_rule"] == "taskbook_is_seed_not_line_state"
    assert protocol["operating_order"] == [
        "load_scope",
        "resolve_authority_and_chronology",
        "reconstruct_current_state",
        "classify_blockers",
        "choose_next_safe_action",
        "route_repair_or_continuation",
        "stop_or_continue",
    ]
    assert "taskbook_or_handoff_seed" in protocol["authority_order"]
    assert "stop_or_continue" in protocol["required_outputs"]

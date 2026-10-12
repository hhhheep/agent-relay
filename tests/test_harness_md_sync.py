from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from solution_agent.harness.md_sync import (
    SectionBase,
    SyncBase,
    build_sync_artifacts,
    load_mapping,
    parse_markdown_snapshot,
    resolve_under_root,
)
from solution_agent.harness.contracts import ProviderUsage
from solution_agent.harness.reader_ab import ReaderManagerABObservation, evaluate_reader_manager_savings


ROOT = Path(__file__).resolve().parents[1]


def _mapping(path: Path) -> Path:
    path.write_text(
        """schema_version: research-md-sync/v1
entities:
  - entity_id: TASK-1
    local: {path: local.md}
    remote: {path: remote.md}
    sections:
      - section_id: status
        local_block: frontmatter:execution_status
        remote_block: frontmatter:status
        owner: remote
        review_level: A1
        required: true
        priority: 100
        mode: semantic
""",
        encoding="utf-8",
    )
    return path


def _write_pair(root: Path, local_status: str = "running", remote_status: str = "active", extra: str = "") -> tuple[Path, Path]:
    local = root / "local.md"
    remote = root / "remote.md"
    local.write_text(
        f"---\nexecution_status: {local_status}\n---\n# Current\nLocal notes. {extra}\n",
        encoding="utf-8",
    )
    remote.write_text(
        f"---\nstatus: {remote_status}\n---\n<!-- GENERATION_SIGNAL_CURRENT_20260925 -->\n# Current\nRemote notes. {extra}\n",
        encoding="utf-8",
    )
    return local, remote


def _snap(path: Path, endpoint: str):
    return parse_markdown_snapshot(path, entity_id="TASK-1", endpoint=endpoint, source_path=f"{endpoint}.md")


def test_snapshot_is_deterministic_and_indexes_frontmatter_headings_markers_and_links(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text(
        """---
status: active
owner: lab
---
<!-- CURRENT_SIGNAL -->
# Current State
[paper](papers/a.pdf) and [[Evidence Note|evidence]].
```md
# Not a heading
```
<!-- SYNC:summary:START -->
managed text
<!-- SYNC:summary:END -->
## Details
body
""",
        encoding="utf-8",
    )
    first = parse_markdown_snapshot(source, entity_id="TASK-1", endpoint="local", source_path="note.md")
    second = parse_markdown_snapshot(source, entity_id="TASK-1", endpoint="local", source_path="note.md")
    assert first == second
    assert first.block("frontmatter:status").text == "status: active\n"
    current = first.block("marker:current_signal")
    assert current.block_id == "heading:current-state"
    assert first.block("managed:summary").text == "managed text\n"
    assert [item.kind for item in first.links] == ["markdown", "wikilink"]
    assert not any("not-a-heading" in item.block_id for item in first.blocks)


def test_snapshot_fails_closed_on_unsafe_oversize_and_invalid_markers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe relative path"):
        resolve_under_root(tmp_path, "../escape.md")
    big = tmp_path / "big.md"
    big.write_text("x" * 20, encoding="utf-8")
    with pytest.raises(ValueError, match="max_input_bytes"):
        parse_markdown_snapshot(big, entity_id="TASK-1", endpoint="local", source_path="big.md", max_input_bytes=5)
    broken = tmp_path / "broken.md"
    broken.write_text("<!-- SYNC:x:START -->\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unterminated SYNC"):
        parse_markdown_snapshot(broken, entity_id="TASK-1", endpoint="local", source_path="broken.md")


def test_three_way_plan_initial_owner_update_and_non_owner_conflict(tmp_path: Path) -> None:
    mapping_path = _mapping(tmp_path / "mapping.yaml")
    mapping, mapping_hash = load_mapping(mapping_path)
    local_path, remote_path = _write_pair(tmp_path)
    local, remote = _snap(local_path, "local"), _snap(remote_path, "remote")
    initial, _, decision, candidate, metrics = build_sync_artifacts(mapping, mapping_hash, "TASK-1", local, remote)
    assert initial.write_enabled is False
    assert initial.sections[0].action == "initial_alignment_required"
    assert decision["requires_codex"] is True
    assert decision["next_action"] == "approve_initial_alignment"
    assert metrics["provider_usage_measured"] is False
    assert metrics["token_savings_claim_allowed"] is False

    accepted = candidate
    remote_path.write_text("---\nstatus: done\n---\n# Current\nRemote notes.\n", encoding="utf-8")
    changed_remote = _snap(remote_path, "remote")
    owner_plan, reader, owner_decision, _, _ = build_sync_artifacts(
        mapping, mapping_hash, "TASK-1", local, changed_remote, base=accepted
    )
    assert owner_plan.sections[0].action == "reader_update_proposal"
    assert owner_plan.operations == []
    assert reader["tasks"][0]["source_endpoint"] == "remote"
    assert owner_decision["requires_codex"] is False
    assert owner_decision["next_action"] == "run_reader_manager"

    local_path.write_text("---\nexecution_status: done\n---\n# Current\nLocal notes.\n", encoding="utf-8")
    changed_local = _snap(local_path, "local")
    conflict_plan, _, conflict_decision, _, _ = build_sync_artifacts(
        mapping, mapping_hash, "TASK-1", changed_local, remote, base=accepted
    )
    assert conflict_plan.sections[0].action == "conflict"
    assert conflict_decision["requires_codex"] is True
    assert conflict_decision["next_action"] == "resolve_conflicts"


def test_both_changed_conflicts_and_required_reader_packet_never_silently_truncates(tmp_path: Path) -> None:
    mapping_path = _mapping(tmp_path / "mapping.yaml")
    mapping, mapping_hash = load_mapping(mapping_path)
    local_path, remote_path = _write_pair(tmp_path)
    local, remote = _snap(local_path, "local"), _snap(remote_path, "remote")
    _, _, _, accepted, _ = build_sync_artifacts(mapping, mapping_hash, "TASK-1", local, remote)
    local_path.write_text("---\nexecution_status: changed\n---\n# Current\nL\n", encoding="utf-8")
    remote_path.write_text("---\nstatus: changed\n---\n# Current\nR\n", encoding="utf-8")
    both, _, _, _, _ = build_sync_artifacts(
        mapping, mapping_hash, "TASK-1", _snap(local_path, "local"), _snap(remote_path, "remote"), base=accepted
    )
    assert both.sections[0].action == "conflict"

    remote_path.write_text("---\nstatus: " + "x" * 2000 + "\n---\n# Current\nR\n", encoding="utf-8")
    with pytest.raises(ValueError, match="required context exceeds budget"):
        build_sync_artifacts(
            mapping, mapping_hash, "TASK-1", local, _snap(remote_path, "remote"), base=accepted, max_reader_tokens=20
        )


def test_snapshot_and_plan_scripts_are_create_only_and_do_not_modify_sources(tmp_path: Path) -> None:
    mapping_path = _mapping(tmp_path / "mapping.yaml")
    local_path, remote_path = _write_pair(tmp_path)
    originals = {path: path.read_bytes() for path in (local_path, remote_path)}

    def load_script(name: str):
        path = ROOT / "scripts" / "harness" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    snapshot_runner = load_script("run_research_md_snapshot")
    plan_runner = load_script("run_research_md_sync_plan")
    local_out, remote_out = tmp_path / "local-out", tmp_path / "remote-out"
    snapshot_runner.run(mapping_path, "TASK-1", "local", tmp_path, local_out, 10000)
    snapshot_runner.run(mapping_path, "TASK-1", "remote", tmp_path, remote_out, 10000)
    plan_out = tmp_path / "plan-out"
    result = plan_runner.run(
        mapping_path,
        "TASK-1",
        local_out / "md_snapshot.json",
        remote_out / "md_snapshot.json",
        plan_out,
        4096,
    )
    assert result["metrics"]["write_enabled"] is False
    assert json.loads((plan_out / "sync_plan.json").read_text(encoding="utf-8"))["operations"] == []
    assert all(path.read_bytes() == payload for path, payload in originals.items())
    with pytest.raises(FileExistsError, match="create-only"):
        snapshot_runner.run(mapping_path, "TASK-1", "local", tmp_path, local_out, 10000)


def test_registry_exposes_two_allowlisted_stages_without_new_mcp_tools() -> None:
    registry = yaml.safe_load((ROOT / "solution_agent" / "execution_port" / "script_registry.yaml").read_text(encoding="utf-8"))
    assert registry["scripts"]["research_md_snapshot"]["entrypoint"][-1].endswith("run_research_md_snapshot.py")
    assert registry["scripts"]["research_md_sync_plan"]["entrypoint"][-1].endswith("run_research_md_sync_plan.py")
    server = (ROOT / "solution_agent" / "harness" / "mcp_server.py").read_text(encoding="utf-8")
    assert "research_md_snapshot" not in server
    assert "research_md_sync_plan" not in server


def _decision(case_id: str) -> dict:
    return {
        "entity_id": case_id,
        "execution_status": "plan_succeeded",
        "scientific_status": "unchanged",
        "conflicts": [],
        "authorizations_required": [],
        "initial_alignment_sections": [],
        "reader_tasks_pending": ["status"],
        "next_action": "run_reader_manager",
        "evidence_ids": [f"evidence-{case_id}"],
        "section_outcomes": [["status", "reader_update_proposal", "owner changed"]],
    }


def _ab_observation(case_id: str, decision: dict, input_tokens: int) -> ReaderManagerABObservation:
    return ReaderManagerABObservation(
        case_id=case_id,
        decision=decision,
        evidence_verified=True,
        usage=ProviderUsage(provider="codex", model="gpt-test", input_tokens=input_tokens, output_tokens=20),
    )


def test_provider_token_gate_requires_lower_usage_and_exact_decision_equivalence() -> None:
    case_ids = ["a", "b", "c"]
    baseline = [_ab_observation(case_id, _decision(case_id), 1000) for case_id in case_ids]
    candidate = [_ab_observation(case_id, _decision(case_id), 250) for case_id in case_ids]
    passed = evaluate_reader_manager_savings(baseline, candidate)
    assert passed.claim_allowed is True
    assert passed.measurements["saved_effective_tokens"] == 2250

    changed = _decision("b")
    changed["next_action"] = "none"
    candidate[1] = _ab_observation("b", changed, 100)
    failed = evaluate_reader_manager_savings(baseline, candidate)
    assert failed.claim_allowed is False
    assert "outcome_mismatch:b" in failed.violations

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from solution_agent.tools.patch_tools import (
    build_patch_from_intent,
    check_changed_files_allowed,
    check_patch_applies,
    inspect_patch_files,
    validate_patch_intent,
)


def _init_repo_with_note(tmp_path: Path, text: str = "# Note\n\n## Current behavior\n\nOld text.\n") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    target = repo / "docs" / "note.md"
    target.write_text(text, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    return repo, target


def _base_intent(**overrides):
    intent = {
        "schema_version": 1,
        "intent_id": "patch_intent_001",
        "tier": "P0",
        "target_file": "docs/note.md",
        "operation": "insert_after",
        "anchor": {"type": "literal", "text": "## Current behavior", "occurrence": "unique"},
        "new_text": "Inserted line.",
        "reason": "Clarify behavior.",
        "expected_validation": {"must_apply_cleanly": True, "must_change_only": ["docs/note.md"], "required_tests": []},
        "risk": {"source_code_behavior_change": False, "multi_file": False, "requires_codex": False},
    }
    intent.update(overrides)
    return intent


def test_inspect_patch_files_extracts_changed_files():
    patch = """diff --git a/solution_agent/remote_materializer/artifact_parser.py b/solution_agent/remote_materializer/artifact_parser.py
--- a/solution_agent/remote_materializer/artifact_parser.py
+++ b/solution_agent/remote_materializer/artifact_parser.py
@@ -1,2 +1,2 @@
-old
+new
"""

    assert inspect_patch_files(patch) == ["solution_agent/remote_materializer/artifact_parser.py"]


def test_changed_file_policy_rejects_out_of_scope_file():
    result = check_changed_files_allowed(
        ["models/loupe/dual_head_effort.py"],
        ["solution_agent/remote_materializer/artifact_parser.py"],
    )

    assert not result.ok
    assert "outside write_scope" in result.errors[0]


def test_changed_file_policy_does_not_treat_folder_scope_as_allowed_yet():
    result = check_changed_files_allowed(
        ["docs/note.md"],
        ["docs"],
    )

    assert not result.ok
    assert "outside write_scope" in result.errors[0]


def test_check_patch_applies_rejects_placeholder_hunk(repo_root: Path = Path(".")):
    patch = """diff --git a/solution_agent/remote_materializer/artifact_parser.py b/solution_agent/remote_materializer/artifact_parser.py
--- a/solution_agent/remote_materializer/artifact_parser.py
+++ b/solution_agent/remote_materializer/artifact_parser.py
@@ -XX,XX +XX,XX @@
-old
+new
"""

    result = check_patch_applies(repo_root, patch)

    assert not result.ok
    assert any("placeholder" in error for error in result.errors)


def test_build_patch_from_intent_generates_real_diff(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent()

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert result.ok, result.errors
    assert "Inserted line." in result.patch_text
    assert result.quality_report["git_apply_check_passed"] is True
    assert result.quality_report["changed_files"] == ["docs/note.md"]


def test_build_patch_from_intent_can_create_new_file(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(
        target_file="docs/new_note.md",
        operation="create_file",
        new_text="# New Note\n\nCreated safely.",
        expected_validation={"must_apply_cleanly": True, "must_change_only": ["docs/new_note.md"], "required_tests": []},
    )

    result = build_patch_from_intent(repo, intent, ["docs/new_note.md"])

    assert result.ok, result.errors
    assert "new file mode 100644" in result.patch_text
    assert "+Created safely." in result.patch_text
    assert result.quality_report["changed_files"] == ["docs/new_note.md"]


def test_build_patch_from_intent_rejects_missing_anchor(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(anchor={"type": "literal", "text": "## Missing", "occurrence": "unique"})

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert not result.ok
    assert result.errors == ["anchor_not_found"]


def test_build_patch_from_intent_rejects_non_unique_anchor(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path, "# Note\n\n## Same\nA\n## Same\nB\n")
    intent = _base_intent(anchor={"type": "literal", "text": "## Same", "occurrence": "unique"})

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert not result.ok
    assert result.errors == ["anchor_not_unique"]


def test_build_patch_from_intent_rejects_stale_file_hash(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(
        anchor={
            "type": "literal",
            "text": "## Current behavior",
            "occurrence": "unique",
            "file_sha256_before": hashlib.sha256(b"older content").hexdigest(),
        }
    )

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert not result.ok
    assert result.errors == ["stale_file_hash"]


def test_build_patch_from_intent_replaces_anchor_block(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(
        operation="replace_block",
        replacement_text="## Current behavior\n\nReplacement text.",
    )
    intent.pop("new_text")

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert result.ok, result.errors
    assert "Replacement text." in result.patch_text


def test_patch_intent_rejects_out_of_scope_target():
    intent = {
        "schema_version": 1,
        "intent_id": "patch_intent_001",
        "tier": "P0",
        "target_file": "solution_agent/runtime.py",
        "operation": "insert_after",
        "anchor": {"type": "literal", "text": "anchor", "occurrence": "unique"},
        "new_text": "text",
        "reason": "Nope.",
        "expected_validation": {"must_apply_cleanly": True, "must_change_only": ["solution_agent/runtime.py"]},
        "risk": {"source_code_behavior_change": False, "multi_file": False, "requires_codex": False},
    }

    errors = validate_patch_intent(intent, ["docs/note.md"])

    assert any("target_file_not_allowed" in error for error in errors)


def test_patch_intent_rejects_source_behavior_change():
    intent = {
        "schema_version": 1,
        "intent_id": "patch_intent_001",
        "tier": "P1",
        "target_file": "docs/note.md",
        "operation": "insert_after",
        "anchor": {"type": "literal", "text": "anchor", "occurrence": "unique"},
        "new_text": "text",
        "reason": "Nope.",
        "expected_validation": {"must_apply_cleanly": True, "must_change_only": ["docs/note.md"]},
        "risk": {"source_code_behavior_change": True, "multi_file": False, "requires_codex": False},
    }

    errors = validate_patch_intent(intent, ["docs/note.md"])

    assert "source_or_multifile_patch_requires_codex" in errors


def test_patch_intent_rejects_must_change_only_claiming_extra_file():
    intent = _base_intent(
        expected_validation={
            "must_apply_cleanly": True,
            "must_change_only": ["docs/note.md", "docs/other.md"],
            "required_tests": [],
        }
    )

    errors = validate_patch_intent(intent, ["docs/note.md"])

    assert "must_change_only includes non-target file: docs/other.md" in errors


def test_patch_intent_rejects_unsupported_p2_tier_with_codex_handoff(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(tier="P2")

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert not result.ok
    assert "unsupported patch tier: P2" in result.errors
    assert result.quality_report["recommended_action"] == "codex_handoff_required"
    assert result.quality_report["codex_handoff_required"] is True
    assert result.quality_report["primary_rule_id"] == "unsupported_patch_tier"
    assert "unsupported_patch_tier" in result.quality_report["rule_ids"]


def test_patch_intent_source_behavior_change_reports_codex_handoff(tmp_path: Path):
    repo, _target = _init_repo_with_note(tmp_path)
    intent = _base_intent(risk={"source_code_behavior_change": True, "multi_file": False, "requires_codex": True})

    result = build_patch_from_intent(repo, intent, ["docs/note.md"])

    assert not result.ok
    assert "source_or_multifile_patch_requires_codex" in result.errors
    assert result.quality_report["recommended_action"] == "codex_handoff_required"
    assert result.quality_report["codex_handoff_required"] is True
    assert result.quality_report["primary_rule_id"] == "source_or_multifile_patch_requires_codex"
    assert "source_or_multifile_patch_requires_codex" in result.quality_report["rule_ids"]


def test_patch_intent_rejects_unsafe_target_path():
    intent = _base_intent(target_file="../outside.md")

    errors = validate_patch_intent(intent, ["docs/note.md"])

    assert "target_file is unsafe or empty" in errors

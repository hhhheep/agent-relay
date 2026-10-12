from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PatchCheck:
    ok: bool
    changed_files: list[str]
    errors: list[str]


@dataclass(frozen=True)
class PatchIntentResult:
    ok: bool
    patch_text: str
    quality_report: dict[str, Any]
    errors: list[str]


ALLOWED_PATCH_INTENT_OPERATIONS = {"create_file", "insert_after", "insert_before", "replace_block"}
PATCH_INTENT_TIERS = {"P0", "P1"}


def generate_diff_from_worktree(repo_root: Path, paths: list[str] | None = None) -> str:
    """Return git diff text for real worktree changes."""
    command = ["git", "diff", "--"]
    if paths:
        command.extend(paths)
    result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def _is_git_worktree(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and _normalize_result(result.stdout).lower() == "true"


def _generate_unified_diff_from_text(path: str, original: str, edited: str) -> str:
    original_lines = original.splitlines(keepends=True)
    edited_lines = edited.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            edited_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        ),
    )
    return "".join(f"{line}\n" if not line.endswith("\n") else line for line in diff_lines)


def _normalize_result(value: Any) -> str:
    return str(value or "").strip()


def check_patch_applies(repo_root: Path, patch_text: str) -> PatchCheck:
    """Validate a patch with git apply --check and lightweight syntax checks."""
    changed_files = inspect_patch_files(patch_text)
    errors = _patch_text_errors(patch_text)
    git_error = _git_apply_check(repo_root, patch_text)
    if git_error:
        errors.append(git_error)
    return PatchCheck(ok=not errors, changed_files=changed_files, errors=errors)


def inspect_patch_files(patch_text: str) -> list[str]:
    """Extract changed files from a unified diff without trusting model prose."""
    files: set[str] = set()
    try:
        from unidiff import PatchSet  # type: ignore

        patch = PatchSet(patch_text.splitlines(keepends=True))
        for item in patch:
            for path in (item.source_file, item.target_file):
                normalized = _normalize_diff_path(path)
                if normalized:
                    files.add(normalized)
    except Exception:
        files.update(_fallback_changed_files(patch_text))
    return sorted(files)


def check_changed_files_allowed(changed_files: list[str], write_scope: list[str]) -> PatchCheck:
    errors: list[str] = []
    allowed = {_normalize_scope_path(path) for path in write_scope}
    for path in changed_files:
        if _normalize_scope_path(path) not in allowed:
            errors.append(f"patch touches file outside write_scope: {path}")
    return PatchCheck(ok=not errors, changed_files=sorted(changed_files), errors=errors)


def build_patch_from_intent(repo_root: Path, intent: dict[str, Any], write_scope: list[str]) -> PatchIntentResult:
    errors = validate_patch_intent(intent, write_scope)
    if errors:
        return _patch_intent_result(False, "", intent, [], errors)

    target_file = _normalize_scope_path(str(intent["target_file"]))
    target_path = repo_root / target_file
    operation = str(intent["operation"])
    new_text = str(intent.get("new_text") if intent.get("new_text") is not None else intent.get("replacement_text") or "")
    if operation == "create_file":
        if target_path.exists():
            return _patch_intent_result(False, "", intent, [], ["target_file_already_exists"])
        edited = new_text if new_text.endswith("\n") else f"{new_text}\n"
        patch_text = _generate_create_file_diff(target_file, edited)
        if _is_git_worktree(repo_root):
            check = check_patch_applies(repo_root, patch_text)
        else:
            patch_text_errors = _patch_text_errors(patch_text)
            check = PatchCheck(ok=not patch_text_errors, changed_files=inspect_patch_files(patch_text), errors=patch_text_errors)
        changed_check = check_changed_files_allowed(check.changed_files, write_scope)
        all_errors = [*check.errors, *changed_check.errors]
        ok = not all_errors
        report = {
            "status": "accepted" if ok else "rejected",
            "failure_type": None if ok else "patch_validation_failed",
            "changed_files": check.changed_files,
            "changed_line_count": _changed_line_count(patch_text),
            "git_apply_check_passed": check.ok,
            "validation_commands": ["git apply --check patch.diff"],
            "recommended_action": "human_review" if ok else "fix_patch_intent",
        }
        return PatchIntentResult(ok=ok, patch_text=patch_text, quality_report=report, errors=all_errors)

    try:
        original = target_path.read_text(encoding="utf-8")
    except Exception as exc:
        return _patch_intent_result(False, "", intent, [], [f"target_file read failed: {exc}"])

    anchor = intent.get("anchor") if isinstance(intent.get("anchor"), dict) else {}
    expected_hash = str(anchor.get("file_sha256_before") or "").strip()
    if expected_hash and hashlib.sha256(original.encode("utf-8")).hexdigest() != expected_hash:
        return _patch_intent_result(False, "", intent, [], ["stale_file_hash"])

    anchor_text = str(anchor.get("text") or "")
    matches = _anchor_offsets(original, anchor_text)
    if not matches:
        return _patch_intent_result(False, "", intent, [], ["anchor_not_found"])
    if len(matches) > 1 and str(anchor.get("occurrence") or "unique") == "unique":
        return _patch_intent_result(False, "", intent, [], ["anchor_not_unique"])

    edited = _apply_anchor_edit(original, operation, anchor_text, matches[0], new_text)
    if edited == original:
        return _patch_intent_result(False, "", intent, [], ["intent_made_no_change"])

    try:
        target_path.write_text(edited, encoding="utf-8")
        if _is_git_worktree(repo_root):
            patch_text = generate_diff_from_worktree(repo_root, [target_file])
            if not patch_text.strip():
                patch_text = _generate_unified_diff_from_text(target_file, original, edited)
        else:
            patch_text = _generate_unified_diff_from_text(target_file, original, edited)
    finally:
        target_path.write_text(original, encoding="utf-8")

    if _is_git_worktree(repo_root):
        check = check_patch_applies(repo_root, patch_text)
    else:
        patch_text_errors = _patch_text_errors(patch_text)
        check = PatchCheck(ok=not patch_text_errors, changed_files=inspect_patch_files(patch_text), errors=patch_text_errors)

    changed_check = check_changed_files_allowed(check.changed_files, write_scope)
    all_errors = [*check.errors, *changed_check.errors]
    ok = not all_errors
    report = {
        "status": "accepted" if ok else "rejected",
        "failure_type": None if ok else "patch_validation_failed",
        "changed_files": check.changed_files,
        "changed_line_count": _changed_line_count(patch_text),
        "git_apply_check_passed": check.ok,
        "validation_commands": ["git apply --check patch.diff"],
        "recommended_action": "human_review" if ok else "fix_patch_intent",
    }
    return PatchIntentResult(ok=ok, patch_text=patch_text, quality_report=report, errors=all_errors)


def validate_patch_intent(intent: dict[str, Any], write_scope: list[str]) -> list[str]:
    errors: list[str] = []
    required = ["schema_version", "intent_id", "tier", "target_file", "operation", "anchor", "reason", "expected_validation", "risk"]
    for key in required:
        if key not in intent:
            errors.append(f"patch_intent missing required key: {key}")
    tier = str(intent.get("tier") or "")
    if tier not in PATCH_INTENT_TIERS:
        errors.append(f"unsupported patch tier: {tier}")
    operation = str(intent.get("operation") or "")
    if operation not in ALLOWED_PATCH_INTENT_OPERATIONS:
        errors.append(f"unsupported patch operation: {operation}")
    target_file = _normalize_scope_path(str(intent.get("target_file") or ""))
    if not target_file:
        errors.append("target_file is unsafe or empty")
    elif target_file not in {_normalize_scope_path(path) for path in write_scope}:
        errors.append(f"target_file_not_allowed: {target_file}")
    anchor = intent.get("anchor")
    if operation != "create_file" and (not isinstance(anchor, dict) or not str(anchor.get("text") or "").strip()):
        errors.append("anchor must include non-empty text")
    if operation in {"create_file", "insert_after", "insert_before"} and not str(intent.get("new_text") or "").strip():
        errors.append(f"{operation} operation requires new_text")
    if operation == "replace_block" and not str(intent.get("replacement_text") or "").strip():
        errors.append("replace_block requires replacement_text")
    risk = intent.get("risk") if isinstance(intent.get("risk"), dict) else {}
    if risk.get("source_code_behavior_change") is True or risk.get("multi_file") is True or risk.get("requires_codex") is True:
        errors.append("source_or_multifile_patch_requires_codex")
    expected = intent.get("expected_validation") if isinstance(intent.get("expected_validation"), dict) else {}
    must_change_only = expected.get("must_change_only")
    if isinstance(must_change_only, list):
        for path in must_change_only:
            if _normalize_scope_path(str(path)) != target_file:
                errors.append(f"must_change_only includes non-target file: {path}")
    return errors


def _git_apply_check(repo_root: Path, patch_text: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, newline="\n") as handle:
        handle.write(patch_text)
        temp_path = handle.name
    try:
        result = subprocess.run(
            ["git", "apply", "--check", temp_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(temp_path)
    if result.returncode == 0:
        return ""
    return result.stderr.strip() or "git apply --check failed"


def _generate_create_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    if body:
        body = f"{body}\n"
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..0000000\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def _anchor_offsets(text: str, anchor: str) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            return offsets
        offsets.append(index)
        start = index + len(anchor)


def _apply_anchor_edit(original: str, operation: str, anchor: str, anchor_offset: int, new_text: str) -> str:
    insert_text = new_text if new_text.endswith("\n") else f"{new_text}\n"
    if operation == "insert_before":
        return original[:anchor_offset] + insert_text + original[anchor_offset:]
    anchor_end = anchor_offset + len(anchor)
    if operation == "insert_after":
        suffix = "" if original[anchor_end:anchor_end + 1] == "\n" else "\n"
        return original[:anchor_end] + suffix + insert_text + original[anchor_end:]
    replacement = str(new_text)
    return original[:anchor_offset] + replacement + original[anchor_end:]


def _changed_line_count(patch_text: str) -> int:
    count = 0
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def _patch_intent_result(
    ok: bool,
    patch_text: str,
    intent: dict[str, Any],
    changed_files: list[str],
    errors: list[str],
) -> PatchIntentResult:
    handoff_required = any(
        "requires_codex" in error
        or "source_or_multifile_patch_requires_codex" in error
        or "unsupported patch tier: P2" in error
        or "unsupported patch tier: P3" in error
        for error in errors
    )
    report = {
        "status": "accepted" if ok else "rejected",
        "failure_type": None if ok else (errors[0] if errors else "unknown"),
        "primary_rule_id": None if ok else (_patch_error_rule_id(errors[0]) if errors else "unknown"),
        "rule_ids": [_patch_error_rule_id(error) for error in errors],
        "changed_files": changed_files,
        "changed_line_count": _changed_line_count(patch_text),
        "git_apply_check_passed": False,
        "validation_commands": ["git apply --check patch.diff"],
        "recommended_action": "human_review" if ok else ("codex_handoff_required" if handoff_required else "fix_patch_intent"),
        "intent_id": intent.get("intent_id"),
        "codex_handoff_required": handoff_required,
    }
    return PatchIntentResult(ok=ok, patch_text=patch_text, quality_report=report, errors=errors)


def _patch_error_rule_id(error: str) -> str:
    text = str(error or "")
    if text.startswith("patch_intent missing required key:"):
        return "patch_intent_missing_required_key"
    if text.startswith("unsupported patch tier:"):
        return "unsupported_patch_tier"
    if text.startswith("unsupported patch operation:"):
        return "unsupported_patch_operation"
    if text == "target_file is unsafe or empty":
        return "unsafe_target_file"
    if text.startswith("target_file_not_allowed:"):
        return "target_file_not_allowed"
    if text == "anchor must include non-empty text":
        return "missing_anchor_text"
    if text == "insert operation requires new_text":
        return "missing_insert_text"
    if text == "replace_block requires replacement_text":
        return "missing_replacement_text"
    if text == "source_or_multifile_patch_requires_codex":
        return "source_or_multifile_patch_requires_codex"
    if text.startswith("must_change_only includes non-target file:"):
        return "must_change_only_non_target"
    if text == "stale_file_hash":
        return "stale_file_hash"
    if text == "anchor_not_found":
        return "anchor_not_found"
    if text == "anchor_not_unique":
        return "anchor_not_unique"
    if text == "intent_made_no_change":
        return "intent_made_no_change"
    if text == "target_file_already_exists":
        return "target_file_already_exists"
    if text.startswith("target_file read failed:"):
        return "target_file_read_failed"
    if "outside write_scope" in text:
        return "patch_touches_outside_write_scope"
    if "placeholder hunk" in text:
        return "patch_placeholder_hunk"
    if "markdown separator" in text:
        return "patch_markdown_separator"
    if "no unified diff hunk" in text:
        return "patch_no_unified_diff_hunk"
    if "no parseable changed files" in text:
        return "patch_no_parseable_changed_files"
    if "git apply" in text:
        return "git_apply_check_failed"
    return "patch_validation_failed"


def _patch_text_errors(patch_text: str) -> list[str]:
    errors: list[str] = []
    if "@@ -XX,XX +XX,XX @@" in patch_text or "-XX" in patch_text or "+XX" in patch_text:
        errors.append("patch contains placeholder hunk coordinates")
    if "\n---\n--- " in patch_text or patch_text.startswith("---\n--- "):
        errors.append("patch contains markdown separator lines")
    if not any(line.startswith("@@ ") for line in patch_text.splitlines()):
        errors.append("patch has no unified diff hunk")
    if not inspect_patch_files(patch_text):
        errors.append("patch has no parseable changed files")
    return errors


def _fallback_changed_files(patch_text: str) -> set[str]:
    files: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for part in parts[2:4]:
                    normalized = _normalize_diff_path(part)
                    if normalized:
                        files.add(normalized)
        elif line.startswith(("--- a/", "+++ b/")):
            normalized = _normalize_diff_path(line.split(maxsplit=1)[1])
            if normalized:
                files.add(normalized)
    return files


def _normalize_diff_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if text in {"", "/dev/null"}:
        return ""
    if text.startswith(("a/", "b/")):
        text = text[2:]
    if text.startswith("/") or ".." in Path(text).parts:
        return ""
    return text


def _normalize_scope_path(path: str) -> str:
    return _normalize_diff_path(path).lower()

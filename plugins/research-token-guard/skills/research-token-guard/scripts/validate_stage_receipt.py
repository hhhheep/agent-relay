#!/usr/bin/env python3
"""Validate a bounded remote-research stage receipt without external packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "research-stage-receipt/v1"
TOKEN_GUARD_SCHEMA = "research-token-guard/v1"
STATUSES = {"complete", "failed", "blocked", "needs_review"}
CHECK_STATUSES = {"pass", "fail", "pending", "not_applicable"}
DECISIONS = {"go", "stop", "defer"}
CHUNK_SIZE = 64 * 1024


def digest(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            size += len(chunk)
            sha.update(chunk)
    return size, sha.hexdigest()


def resolve_relative(root: Path, value: str, errors: list[str], label: str) -> Path | None:
    raw = value.split("#", 1)[0]
    candidate = Path(raw)
    if candidate.is_absolute():
        errors.append(f"{label} must be relative to root: {value}")
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        errors.append(f"{label} escapes root: {value}")
        return None
    return resolved


def require_text(data: dict[str, Any], key: str, errors: list[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be a non-empty string")
        return ""
    return value


def require_nonnegative_int(value: Any, label: str, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        errors.append(f"{label} must be a non-negative integer")
        return 0
    return value


def validate_evidence(
    values: Any,
    root: Path | None,
    check_paths: bool,
    errors: list[str],
    label: str,
) -> None:
    if not isinstance(values, list):
        errors.append(f"{label} must be a list")
        return
    for index, value in enumerate(values):
        item_label = f"{label}[{index}]"
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{item_label} must be a non-empty relative path")
            continue
        if root is not None:
            resolved = resolve_relative(root, value, errors, item_label)
            if check_paths and resolved is not None and not resolved.exists():
                errors.append(f"{item_label} does not exist: {value}")


def validate_token_guard_runs(
    section: dict[str, Any], root: Path, verify_runs: bool, errors: list[str]
) -> None:
    run_ids = section.get("run_ids", [])
    verified = section.get("verified_run_ids", [])
    if not isinstance(run_ids, list) or not all(isinstance(x, str) and x for x in run_ids):
        errors.append("token_guard.run_ids must contain non-empty strings")
        return
    if not isinstance(verified, list) or not all(isinstance(x, str) and x for x in verified):
        errors.append("token_guard.verified_run_ids must contain non-empty strings")
        return
    unknown = sorted(set(verified) - set(run_ids))
    if unknown:
        errors.append(f"verified_run_ids are not declared in run_ids: {unknown}")
    if not verify_runs:
        return
    for run_id in run_ids:
        receipt_path = root / ".token-guard" / "runs" / run_id / "receipt.json"
        if not receipt_path.is_file():
            errors.append(f"missing Token Guard receipt for run_id: {run_id}")
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid Token Guard receipt {run_id}: {exc}")
            continue
        if receipt.get("schema_version") != TOKEN_GUARD_SCHEMA:
            errors.append(f"unsupported Token Guard schema for run_id: {run_id}")
            continue
        valid = True
        for stream in ("stdout", "stderr"):
            metadata = receipt.get("streams", {}).get(stream, {})
            value = metadata.get("path")
            if not isinstance(value, str):
                errors.append(f"missing {stream} path for run_id: {run_id}")
                valid = False
                continue
            path = resolve_relative(root, value, errors, f"Token Guard {run_id} {stream}")
            if path is None or not path.is_file():
                errors.append(f"missing {stream} raw file for run_id: {run_id}")
                valid = False
                continue
            size, sha = digest(path)
            if size != metadata.get("bytes") or sha != metadata.get("sha256"):
                errors.append(f"hash or byte mismatch for {run_id} {stream}")
                valid = False
        if run_id in verified and not valid:
            errors.append(f"run declared verified but verification failed: {run_id}")


def validate(data: Any, args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["receipt root must be an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    require_text(data, "task_id", errors)
    require_text(data, "stage_id", errors)
    require_text(data, "objective", errors)
    status = data.get("status")
    if status not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")

    root = Path(args.root).resolve() if args.root else None
    checks = data.get("acceptance_checks")
    if not isinstance(checks, list) or not checks:
        errors.append("acceptance_checks must be a non-empty list")
        checks = []
    for index, check in enumerate(checks):
        label = f"acceptance_checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{label} must be an object")
            continue
        require_text(check, "name", errors)
        if check.get("status") not in CHECK_STATUSES:
            errors.append(f"{label}.status must be one of {sorted(CHECK_STATUSES)}")
        validate_evidence(check.get("evidence"), root, args.check_artifacts, errors, f"{label}.evidence")
    if status == "complete" and any(
        isinstance(check, dict) and check.get("status") == "pending" for check in checks
    ):
        errors.append("complete stage cannot contain pending acceptance checks")

    decision = data.get("decision")
    if not isinstance(decision, dict):
        errors.append("decision must be an object")
    else:
        require_text(decision, "gate", errors)
        require_text(decision, "rationale", errors)
        if decision.get("result") not in DECISIONS:
            errors.append(f"decision.result must be one of {sorted(DECISIONS)}")
        validate_evidence(
            decision.get("evidence"), root, args.check_artifacts, errors, "decision.evidence"
        )

    commands = data.get("commands")
    if not isinstance(commands, dict):
        errors.append("commands must be an object")
    else:
        total = require_nonnegative_int(commands.get("total"), "commands.total", errors)
        guarded = require_nonnegative_int(commands.get("guarded"), "commands.guarded", errors)
        unguarded = require_nonnegative_int(commands.get("unguarded"), "commands.unguarded", errors)
        reasons = commands.get("unguarded_reasons")
        if not isinstance(reasons, list) or not all(isinstance(x, str) and x.strip() for x in reasons):
            errors.append("commands.unguarded_reasons must contain non-empty strings")
            reasons = []
        if total != guarded + unguarded:
            errors.append("commands.total must equal guarded + unguarded")
        if unguarded > 0 and not reasons:
            errors.append("unguarded commands require at least one reason")

    token_guard = data.get("token_guard")
    if not isinstance(token_guard, dict):
        errors.append("token_guard must be an object")
    else:
        raw = require_nonnegative_int(token_guard.get("raw_bytes"), "token_guard.raw_bytes", errors)
        returned = require_nonnegative_int(
            token_guard.get("returned_bytes"), "token_guard.returned_bytes", errors
        )
        if returned > raw:
            errors.append("token_guard.returned_bytes cannot exceed raw_bytes")
        if args.verify_token_guard:
            if root is None:
                errors.append("--verify-token-guard requires --root")
            else:
                validate_token_guard_runs(token_guard, root, True, errors)
        elif root is not None:
            validate_token_guard_runs(token_guard, root, False, errors)

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
    else:
        for index, artifact in enumerate(artifacts):
            label = f"artifacts[{index}]"
            if not isinstance(artifact, dict):
                errors.append(f"{label} must be an object")
                continue
            path_value = artifact.get("path")
            if not isinstance(path_value, str) or not path_value:
                errors.append(f"{label}.path must be a non-empty relative path")
                continue
            if root is not None:
                path = resolve_relative(root, path_value, errors, f"{label}.path")
                if args.check_artifacts and path is not None:
                    if not path.is_file():
                        errors.append(f"{label}.path does not exist: {path_value}")
                    elif isinstance(artifact.get("sha256"), str):
                        _, sha = digest(path)
                        if sha != artifact["sha256"]:
                            errors.append(f"{label}.sha256 mismatch")

    handoff = data.get("handoff")
    if not isinstance(handoff, dict):
        errors.append("handoff must be an object")
    else:
        require_text(handoff, "next_action", errors)
        for key in ("required_inputs", "do_not_assume"):
            value = handoff.get(key)
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                errors.append(f"handoff.{key} must be a list of strings")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt")
    parser.add_argument("--root")
    parser.add_argument("--check-artifacts", action="store_true")
    parser.add_argument("--verify-token-guard", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, indent=2))
        return 2
    errors = validate(data, args)
    result = {
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "error_count": len(errors),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())

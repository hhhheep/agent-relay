#!/usr/bin/env python3
"""Durable stage ledger with verified content-addressed command reuse."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


LEDGER_SCHEMA = "research-stage-ledger/v1"
STAGE_SCHEMA = "research-stage-spec/v1"
FINGERPRINT_SCHEMA = "research-command-fingerprint/v1"
TOKEN_GUARD_SCHEMA = "research-token-guard/v1"
SECRET_FLAG_RE = re.compile(
    r"(?i)(api[-_]?key|token|password|passwd|secret|credential|authorization)"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,}|"
    r"(?:api[-_]?key|token|password|secret)\s*[=:]\s*\S+)"
)


class LedgerError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_now() -> float:
    return dt.datetime.now(dt.timezone.utc).timestamp()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def print_json(value: Any) -> bytes:
    raw = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")
    sys.stdout.buffer.write(raw)
    return raw


def resolve_within(root: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    root = root.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LedgerError(f"path escapes root: {value}") from exc
    return resolved


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot load JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"JSON root must be an object: {path}")
    return value


def validate_stage_spec(spec: dict[str, Any]) -> None:
    if spec.get("schema_version") != STAGE_SCHEMA:
        raise LedgerError(f"stage schema must be {STAGE_SCHEMA}")
    for field in ("task_id", "stage_id", "objective"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise LedgerError(f"stage {field} must be a non-empty string")
    checks = spec.get("acceptance_checks")
    if not isinstance(checks, list) or not checks or not all(
        isinstance(item, str) and item.strip() for item in checks
    ):
        raise LedgerError("acceptance_checks must contain non-empty strings")
    budgets = spec.get("budgets")
    required = (
        "max_command_executions",
        "max_model_facing_bytes",
        "max_retries_per_fingerprint",
        "max_wall_seconds",
    )
    if not isinstance(budgets, dict):
        raise LedgerError("budgets must be an object")
    for field in required:
        value = budgets.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LedgerError(f"budget {field} must be a positive integer")


def execution_spec(spec: dict[str, Any]) -> dict[str, Any]:
    execution = spec.get("execution")
    if not isinstance(execution, dict):
        raise LedgerError("stage execution must be an object for atomic execution")
    for field in ("token_guard", "command"):
        if field not in execution:
            raise LedgerError(f"stage execution is missing {field}")
    if not isinstance(execution["token_guard"], str) or not execution["token_guard"].strip():
        raise LedgerError("stage execution token_guard must be a non-empty string")
    command = execution["command"]
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise LedgerError("stage execution command must contain non-empty strings")
    inputs = execution.get("inputs", [])
    if not isinstance(inputs, list) or not all(
        isinstance(item, str) and item for item in inputs
    ):
        raise LedgerError("stage execution inputs must be strings")
    queries = execution.get("evidence_queries", [])
    if not isinstance(queries, list):
        raise LedgerError("stage execution evidence_queries must be a list")
    names: set[str] = set()
    for query in queries:
        if not isinstance(query, dict):
            raise LedgerError("each evidence query must be an object")
        name = query.get("name")
        literal = query.get("query")
        if not isinstance(name, str) or not name or name in names:
            raise LedgerError("evidence query names must be non-empty and unique")
        if not isinstance(literal, str) or not literal:
            raise LedgerError(f"evidence query {name} must have a non-empty literal query")
        if query.get("stream", "stdout") not in ("stdout", "stderr"):
            raise LedgerError(f"evidence query {name} has an invalid stream")
        if not isinstance(query.get("value_after_query", False), bool):
            raise LedgerError(f"evidence query {name} value_after_query must be boolean")
        max_bytes = query.get("max_bytes", 2048)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise LedgerError(f"evidence query {name} max_bytes must be a positive integer")
        names.add(name)
    return execution


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            parent_event_id TEXT,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            model_facing_bytes INTEGER NOT NULL CHECK(model_facing_bytes >= 0)
        );
        CREATE TABLE cache (
            fingerprint TEXT PRIMARY KEY,
            receipt_path TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            run_event_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


def meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        raise LedgerError(f"ledger metadata missing: {key}")
    return str(row["value"])


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def latest_event_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT event_id FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return str(row["event_id"]) if row is not None else None


def append_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
    *,
    model_facing_bytes: int = 0,
) -> str:
    parent = latest_event_id(conn)
    created_at = utc_now()
    payload_raw = canonical_bytes(payload)
    payload_sha = digest_bytes(payload_raw)
    envelope = {
        "schema_version": LEDGER_SCHEMA,
        "parent_event_id": parent,
        "event_type": event_type,
        "created_at": created_at,
        "payload_sha256": payload_sha,
        "model_facing_bytes": model_facing_bytes,
    }
    event_id = digest_bytes(canonical_bytes(envelope))
    conn.execute(
        "INSERT INTO events(event_id,parent_event_id,event_type,created_at,payload_json,"
        "payload_sha256,model_facing_bytes) VALUES(?,?,?,?,?,?,?)",
        (
            event_id,
            parent,
            event_type,
            created_at,
            payload_raw.decode("utf-8"),
            payload_sha,
            model_facing_bytes,
        ),
    )
    return event_id


def stage_spec(conn: sqlite3.Connection) -> dict[str, Any]:
    value = json.loads(meta_get(conn, "stage_spec"))
    if not isinstance(value, dict):
        raise LedgerError("stored stage specification is invalid")
    validate_stage_spec(value)
    return value


def event_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS count FROM events GROUP BY event_type"
    ).fetchall()
    return {str(row["event_type"]): int(row["count"]) for row in rows}


def budget_snapshot(conn: sqlite3.Connection, spec: dict[str, Any]) -> dict[str, Any]:
    counts = event_counts(conn)
    command_executions = counts.get("run_started", 0)
    row = conn.execute("SELECT COALESCE(SUM(model_facing_bytes),0) AS total FROM events").fetchone()
    model_bytes = int(row["total"])
    elapsed = max(0.0, epoch_now() - float(meta_get(conn, "created_epoch")))
    budgets = spec["budgets"]
    return {
        "command_executions": command_executions,
        "max_command_executions": budgets["max_command_executions"],
        "model_facing_bytes": model_bytes,
        "max_model_facing_bytes": budgets["max_model_facing_bytes"],
        "elapsed_seconds": round(elapsed, 3),
        "max_wall_seconds": budgets["max_wall_seconds"],
        "command_budget_remaining": budgets["max_command_executions"] - command_executions,
        "model_facing_bytes_remaining": budgets["max_model_facing_bytes"] - model_bytes,
        "wall_seconds_remaining": round(budgets["max_wall_seconds"] - elapsed, 3),
    }


def assert_can_run(conn: sqlite3.Connection, spec: dict[str, Any], fingerprint: str) -> None:
    state = meta_get(conn, "state")
    if state != "running":
        raise LedgerError(f"stage state does not permit execution: {state}")
    budget = budget_snapshot(conn, spec)
    if budget["command_budget_remaining"] <= 0:
        meta_set(conn, "state", "needs_review")
        conn.commit()
        raise LedgerError("command execution budget exhausted; stage moved to needs_review")
    if budget["model_facing_bytes_remaining"] <= 0:
        meta_set(conn, "state", "needs_review")
        conn.commit()
        raise LedgerError("model-facing byte budget exhausted; stage moved to needs_review")
    if budget["wall_seconds_remaining"] <= 0:
        meta_set(conn, "state", "needs_review")
        conn.commit()
        raise LedgerError("wall-time budget exhausted; stage moved to needs_review")
    failure_rows = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='run_completed'"
    ).fetchall()
    failures = 0
    for row in failure_rows:
        payload = json.loads(row["payload_json"])
        if payload.get("fingerprint") == fingerprint and not payload.get("success"):
            failures += 1
    if failures >= spec["budgets"]["max_retries_per_fingerprint"]:
        meta_set(conn, "state", "needs_review")
        conn.commit()
        raise LedgerError("retry budget exhausted for fingerprint; stage moved to needs_review")


def input_records(root: Path, inputs: list[str]) -> list[dict[str, Any]]:
    records = []
    for value in sorted(set(inputs)):
        path = resolve_within(root, value)
        if not path.is_file():
            raise LedgerError(f"declared input is not a file: {value}")
        size, digest = digest_file(path)
        records.append({"path": relative(root, path), "bytes": size, "sha256": digest})
    return records


def redact_argv(argv: list[str]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if arg.startswith("-") and SECRET_FLAG_RE.search(arg):
            if "=" in arg:
                result.append(arg.split("=", 1)[0] + "=<redacted>")
            else:
                result.append(arg)
                redact_next = True
            continue
        result.append(SECRET_VALUE_RE.sub("<redacted>", arg))
    return result


def command_fingerprint(
    spec: dict[str, Any],
    root: Path,
    cwd: Path,
    command: list[str],
    inputs: list[dict[str, Any]],
    token_guard: Path,
) -> tuple[str, dict[str, Any]]:
    _, tool_sha = digest_file(token_guard)
    payload = {
        "schema_version": FINGERPRINT_SCHEMA,
        "stage_contract": {
            "objective": spec["objective"],
            "acceptance_checks": spec["acceptance_checks"],
        },
        "cwd": relative(root, cwd),
        "command_sha256": digest_bytes(canonical_bytes(command)),
        "command_redacted": redact_argv(command),
        "inputs": inputs,
        "token_guard_sha256": tool_sha,
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "os": os.name,
        },
    }
    return digest_bytes(canonical_bytes(payload)), payload


def verify_token_receipt(root: Path, receipt_path: Path) -> tuple[bool, dict[str, Any]]:
    receipt = load_json(receipt_path)
    if receipt.get("schema_version") != TOKEN_GUARD_SCHEMA:
        return False, receipt
    for stream in ("stdout", "stderr"):
        metadata = receipt.get("streams", {}).get(stream)
        if not isinstance(metadata, dict):
            return False, receipt
        try:
            path = resolve_within(root, metadata["path"])
        except (KeyError, LedgerError):
            return False, receipt
        size, digest = digest_file(path)
        if size != metadata.get("bytes") or digest != metadata.get("sha256"):
            return False, receipt
    return True, receipt


def cached_receipt(
    conn: sqlite3.Connection, root: Path, fingerprint: str
) -> tuple[dict[str, Any], sqlite3.Row] | None:
    row = conn.execute("SELECT * FROM cache WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row is None:
        return None
    try:
        path = resolve_within(root, row["receipt_path"])
        _, receipt_sha = digest_file(path)
        valid, receipt = verify_token_receipt(root, path)
    except (LedgerError, OSError):
        return None
    if receipt_sha != row["receipt_sha256"] or not valid:
        return None
    return receipt, row


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise LedgerError(f"root is not a directory: {root}")
    spec_path = resolve_within(root, args.stage)
    spec = load_json(spec_path)
    validate_stage_spec(spec)
    db_path = resolve_within(root, args.db, must_exist=False)
    if db_path.exists():
        raise LedgerError(f"ledger already exists: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        create_schema(conn)
        meta_set(conn, "schema_version", LEDGER_SCHEMA)
        meta_set(conn, "stage_spec", canonical_bytes(spec).decode("utf-8"))
        meta_set(conn, "stage_spec_sha256", digest_bytes(canonical_bytes(spec)))
        meta_set(conn, "root", str(root))
        meta_set(conn, "state", "running")
        meta_set(conn, "created_epoch", str(epoch_now()))
        append_event(
            conn,
            "stage_started",
            {
                "task_id": spec["task_id"],
                "stage_id": spec["stage_id"],
                "stage_spec": relative(root, spec_path),
                "stage_spec_sha256": digest_bytes(canonical_bytes(spec)),
            },
        )
        conn.commit()
    finally:
        conn.close()
    print_json(
        {
            "schema_version": LEDGER_SCHEMA,
            "initialized": True,
            "db": relative(root, db_path),
            "state": "running",
        }
    )
    return 0


def load_ledger(root: Path, db_arg: str) -> tuple[Path, sqlite3.Connection]:
    db_path = resolve_within(root, db_arg)
    conn = open_db(db_path)
    if meta_get(conn, "schema_version") != LEDGER_SCHEMA:
        conn.close()
        raise LedgerError("unsupported ledger schema")
    if Path(meta_get(conn, "root")).resolve() != root.resolve():
        conn.close()
        raise LedgerError("ledger root does not match requested root")
    return db_path, conn


def output_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def record_model_view(
    conn: sqlite3.Connection,
    spec: dict[str, Any],
    kind: str,
    result: dict[str, Any],
    *,
    detail: dict[str, Any] | None = None,
) -> bytes:
    raw = output_bytes(result)
    budget = budget_snapshot(conn, spec)
    if (
        meta_get(conn, "state") == "running"
        and budget["model_facing_bytes"] + len(raw)
        > spec["budgets"]["max_model_facing_bytes"]
    ):
        meta_set(conn, "state", "needs_review")
        if "state" in result:
            result["state"] = "needs_review"
        result["budget_exceeded"] = "max_model_facing_bytes"
        raw = output_bytes(result)
    payload = {
        "kind": kind,
        "view_sha256": digest_bytes(raw),
        "returned_bytes": len(raw),
    }
    if detail:
        payload.update(detail)
    append_event(conn, "view_emitted", payload, model_facing_bytes=len(raw))
    conn.commit()
    return raw


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not args.command:
        raise LedgerError("missing command after --")
    db_path, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        cwd = resolve_within(root, args.cwd if args.cwd else root)
        if not cwd.is_dir():
            raise LedgerError("cwd is not a directory")
        token_guard = Path(args.token_guard).resolve(strict=True)
        inputs = input_records(root, args.input)
        fingerprint, fingerprint_payload = command_fingerprint(
            spec, root, cwd, args.command, inputs, token_guard
        )
        cache = cached_receipt(conn, root, fingerprint)
        if cache is not None:
            receipt, cache_row = cache
            result = {
                "schema_version": LEDGER_SCHEMA,
                "cache_hit": True,
                "fingerprint": fingerprint,
                "verified": True,
                "run_id": receipt["run_id"],
                "receipt": cache_row["receipt_path"],
                "raw_bytes": receipt["raw_bytes"],
                "instruction": "Reuse the verified receipt identity; retrieve exact evidence only if needed.",
            }
            raw = output_bytes(result)
            projected = budget_snapshot(conn, spec)["model_facing_bytes"] + len(raw)
            if projected > spec["budgets"]["max_model_facing_bytes"]:
                meta_set(conn, "state", "needs_review")
                result["budget_exceeded"] = "max_model_facing_bytes"
                raw = output_bytes(result)
            append_event(
                conn,
                "cache_hit",
                {
                    "fingerprint": fingerprint,
                    "run_event_id": cache_row["run_event_id"],
                    "receipt": cache_row["receipt_path"],
                },
                model_facing_bytes=len(raw),
            )
            conn.commit()
            sys.stdout.buffer.write(raw)
            return 0

        assert_can_run(conn, spec, fingerprint)
        append_event(
            conn,
            "run_started",
            {
                "fingerprint": fingerprint,
                "fingerprint_payload": fingerprint_payload,
            },
        )
        conn.commit()

        command = [
            sys.executable,
            str(token_guard),
            "run",
            "--root",
            str(root),
            "--cwd",
            str(cwd),
            "--artifacts",
            args.artifacts,
            "--inline-bytes",
            str(args.inline_bytes),
            "--failure-inline-bytes",
            str(args.failure_inline_bytes),
            "--timeout",
            str(args.timeout),
            "--",
            *args.command,
        ]
        guarded = subprocess.run(command, text=True, capture_output=True, check=False)
        try:
            receipt = json.loads(guarded.stdout)
        except json.JSONDecodeError as exc:
            meta_set(conn, "state", "needs_review")
            append_event(
                conn,
                "run_protocol_error",
                {"fingerprint": fingerprint, "detail": str(exc)},
            )
            conn.commit()
            raise LedgerError("token guard returned invalid JSON; stage moved to needs_review") from exc
        if not isinstance(receipt, dict) or "recovery" not in receipt:
            meta_set(conn, "state", "needs_review")
            conn.commit()
            raise LedgerError("token guard did not return a receipt; stage moved to needs_review")

        receipt_path = resolve_within(root, receipt["recovery"]["receipt"])
        valid, _ = verify_token_receipt(root, receipt_path)
        if not valid:
            meta_set(conn, "state", "needs_review")
        success = (
            valid
            and receipt.get("exit_code") == 0
            and not receipt.get("timed_out")
            and receipt.get("launch_error") is None
        )
        result = {
            "schema_version": LEDGER_SCHEMA,
            "cache_hit": False,
            "fingerprint": fingerprint,
            "receipt": receipt,
            "verified": valid,
        }
        raw = output_bytes(result)
        current_model_bytes = budget_snapshot(conn, spec)["model_facing_bytes"]
        if current_model_bytes + len(raw) > spec["budgets"]["max_model_facing_bytes"]:
            meta_set(conn, "state", "needs_review")
            result["budget_exceeded"] = "max_model_facing_bytes"
            raw = output_bytes(result)
        event_id = append_event(
            conn,
            "run_completed",
            {
                "fingerprint": fingerprint,
                "success": success,
                "verified": valid,
                "run_id": receipt.get("run_id"),
                "receipt": relative(root, receipt_path),
                "receipt_sha256": digest_file(receipt_path)[1],
                "raw_bytes": receipt.get("raw_bytes"),
                "returned_bytes": receipt.get("returned_bytes"),
                "exit_code": receipt.get("exit_code"),
                "timed_out": receipt.get("timed_out"),
            },
            model_facing_bytes=len(raw),
        )
        if success:
            conn.execute(
                "INSERT INTO cache(fingerprint,receipt_path,receipt_sha256,run_event_id,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
                "receipt_path=excluded.receipt_path,receipt_sha256=excluded.receipt_sha256,"
                "run_event_id=excluded.run_event_id,created_at=excluded.created_at",
                (
                    fingerprint,
                    relative(root, receipt_path),
                    digest_file(receipt_path)[1],
                    event_id,
                    utc_now(),
                ),
            )
        conn.commit()
        sys.stdout.buffer.write(raw)
        return 0 if success else 1
    finally:
        conn.close()


def invoke_ledger(*args: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"atomic ledger step returned invalid JSON (exit {completed.returncode})"
        ) from exc
    if not isinstance(value, dict):
        raise LedgerError("atomic ledger step returned a non-object JSON value")
    return completed.returncode, value


def cmd_execute(args: argparse.Namespace) -> int:
    """Run a declared stage and its exact evidence queries through one model tool call."""
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise LedgerError(f"root is not a directory: {root}")
    spec_path = resolve_within(root, args.stage)
    spec = load_json(spec_path)
    validate_stage_spec(spec)
    execution = execution_spec(spec)
    db_path = resolve_within(root, args.db, must_exist=False)

    if not db_path.exists():
        code, initialized = invoke_ledger(
            "init",
            "--root",
            str(root),
            "--stage",
            relative(root, spec_path),
            "--db",
            relative(root, db_path),
        )
        if code != 0:
            raise LedgerError(f"atomic initialization failed: {initialized.get('error', initialized)}")
    else:
        _, conn = load_ledger(root, relative(root, db_path))
        try:
            expected = digest_bytes(canonical_bytes(spec))
            if meta_get(conn, "stage_spec_sha256") != expected:
                raise LedgerError("existing ledger stage specification does not match")
        finally:
            conn.close()

    token_guard = resolve_within(root, execution["token_guard"])
    cwd = execution.get("cwd", ".")
    resolve_within(root, cwd)
    run_args = [
        "run",
        "--root",
        str(root),
        "--db",
        relative(root, db_path),
        "--token-guard",
        str(token_guard),
        "--cwd",
        cwd,
        "--artifacts",
        execution.get("artifacts", ".token-guard/ledger-runs"),
        "--inline-bytes",
        str(execution.get("inline_bytes", 2048)),
        "--failure-inline-bytes",
        str(execution.get("failure_inline_bytes", 4096)),
        "--timeout",
        str(execution.get("timeout", 0)),
    ]
    for input_path in execution.get("inputs", []):
        run_args.extend(["--input", input_path])
    run_args.extend(["--", *execution["command"]])
    run_code, run_result = invoke_ledger(*run_args)
    if run_code != 0:
        error = run_result.get("error")
        if error is None:
            receipt = run_result.get("receipt", {})
            error = {
                "verified": run_result.get("verified"),
                "exit_code": receipt.get("exit_code") if isinstance(receipt, dict) else None,
                "timed_out": receipt.get("timed_out") if isinstance(receipt, dict) else None,
                "launch_error": receipt.get("launch_error") if isinstance(receipt, dict) else None,
            }
        raise LedgerError(f"atomic guarded run failed: {error}")

    receipt_value = run_result.get("receipt")
    if isinstance(receipt_value, str):
        receipt_path = receipt_value
        run_id = run_result.get("run_id")
        raw_bytes = run_result.get("raw_bytes")
    elif isinstance(receipt_value, dict):
        recovery = receipt_value.get("recovery", {})
        receipt_path = recovery.get("receipt") if isinstance(recovery, dict) else None
        run_id = receipt_value.get("run_id")
        raw_bytes = receipt_value.get("raw_bytes")
    else:
        raise LedgerError("atomic guarded run did not return a receipt")
    if not isinstance(receipt_path, str) or not receipt_path:
        raise LedgerError("atomic guarded run receipt path is missing")

    evidence: dict[str, str] = {}
    evidence_meta: dict[str, dict[str, Any]] = {}
    for query in execution.get("evidence_queries", []):
        query_args = [
            "evidence",
            "--root",
            str(root),
            "--db",
            relative(root, db_path),
            "--token-guard",
            str(token_guard),
            "--receipt",
            receipt_path,
            "--stream",
            query.get("stream", "stdout"),
            "--query",
            query["query"],
            "--max-bytes",
            str(query.get("max_bytes", 2048)),
        ]
        query_code, query_result = invoke_ledger(*query_args)
        if query_code not in (0, 1):
            raise LedgerError(
                f"atomic evidence query {query['name']} failed: "
                f"{query_result.get('error', query_result)}"
            )
        excerpt = query_result.get("excerpt")
        if not isinstance(excerpt, dict):
            raise LedgerError(f"atomic evidence query {query['name']} returned no excerpt")
        content = str(excerpt.get("content", ""))
        if query.get("value_after_query", False):
            if excerpt.get("returned_match_count") != 1 or excerpt.get("truncated"):
                raise LedgerError(
                    f"atomic evidence query {query['name']} cannot extract one exact value"
                )
            line = content.rstrip("\r\n")
            line = re.sub(r"^\d+: ", "", line, count=1)
            _, marker, value = line.partition(query["query"])
            if not marker:
                raise LedgerError(
                    f"atomic evidence query {query['name']} marker is missing from its match"
                )
            content = value
        evidence[query["name"]] = content
        evidence_meta[query["name"]] = {
            "match_count": excerpt.get("match_count"),
            "returned_match_count": excerpt.get("returned_match_count"),
            "truncated": excerpt.get("truncated"),
            "stream_sha256": excerpt.get("stream_sha256"),
        }

    verify_code, verification = invoke_ledger(
        "verify",
        "--root",
        str(root),
        "--db",
        relative(root, db_path),
    )
    if verify_code != 0 or verification.get("valid") is not True:
        raise LedgerError(f"atomic ledger verification failed: {verification}")
    result = {
        "schema_version": LEDGER_SCHEMA,
        "kind": "atomic_stage_result",
        "task_id": spec["task_id"],
        "stage_id": spec["stage_id"],
        "cache_hit": bool(run_result.get("cache_hit")),
        "verified": True,
        "fingerprint": run_result.get("fingerprint"),
        "run_id": run_id,
        "receipt": receipt_path,
        "raw_bytes": raw_bytes,
        "evidence": evidence,
        "evidence_meta": evidence_meta,
    }
    print_json(result)
    return 0


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    errors: list[str] = []
    expected_parent: str | None = None
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    for row in rows:
        payload_raw = row["payload_json"].encode("utf-8")
        payload_sha = digest_bytes(payload_raw)
        if payload_sha != row["payload_sha256"]:
            errors.append(f"event {row['seq']} payload hash mismatch")
        if row["parent_event_id"] != expected_parent:
            errors.append(f"event {row['seq']} parent mismatch")
        envelope = {
            "schema_version": LEDGER_SCHEMA,
            "parent_event_id": row["parent_event_id"],
            "event_type": row["event_type"],
            "created_at": row["created_at"],
            "payload_sha256": row["payload_sha256"],
            "model_facing_bytes": row["model_facing_bytes"],
        }
        calculated = digest_bytes(canonical_bytes(envelope))
        if calculated != row["event_id"]:
            errors.append(f"event {row['seq']} event hash mismatch")
        expected_parent = row["event_id"]
    return not errors, errors


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, conn = load_ledger(root, args.db)
    try:
        valid, errors = verify_chain(conn)
        result = {
            "schema_version": LEDGER_SCHEMA,
            "valid": valid,
            "event_count": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "errors": errors,
        }
        print_json(result)
        return 0 if valid else 2
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db_path, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        valid, errors = verify_chain(conn)
        last = conn.execute(
            "SELECT seq,event_id,event_type,created_at FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        result = {
            "schema_version": LEDGER_SCHEMA,
            "db": relative(root, db_path),
            "task_id": spec["task_id"],
            "stage_id": spec["stage_id"],
            "objective": spec["objective"],
            "state": meta_get(conn, "state"),
            "ledger_valid": valid,
            "ledger_errors": errors,
            "budget": budget_snapshot(conn, spec),
            "event_counts": event_counts(conn),
            "last_event": dict(last) if last is not None else None,
        }
        raw = record_model_view(conn, spec, "status", result)
        sys.stdout.buffer.write(raw)
        return 0 if valid else 2
    finally:
        conn.close()


def cmd_evidence(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        receipt_path = resolve_within(root, args.receipt)
        valid, receipt = verify_token_receipt(root, receipt_path)
        if not valid:
            raise LedgerError("receipt or raw evidence failed verification")
        token_guard = Path(args.token_guard).resolve(strict=True)
        command = [
            sys.executable,
            str(token_guard),
            "excerpt",
            "--root",
            str(root),
            "--receipt",
            str(receipt_path),
            "--stream",
            args.stream,
            "--max-bytes",
            str(args.max_bytes),
        ]
        if args.query is not None:
            command.extend(["--query", args.query])
        if args.start_line is not None:
            command.extend(["--start-line", str(args.start_line)])
        if args.end_line is not None:
            command.extend(["--end-line", str(args.end_line)])
        excerpt = subprocess.run(command, text=True, capture_output=True, check=False)
        if excerpt.returncode not in (0, 1):
            raise LedgerError(f"evidence retrieval failed: {excerpt.stdout}")
        excerpt_data = json.loads(excerpt.stdout)
        result = {
            "schema_version": LEDGER_SCHEMA,
            "kind": "evidence",
            "verified": True,
            "run_id": receipt["run_id"],
            "receipt": relative(root, receipt_path),
            "excerpt": excerpt_data,
        }
        raw = record_model_view(
            conn,
            spec,
            "evidence",
            result,
            detail={
                "run_id": receipt["run_id"],
                "receipt": relative(root, receipt_path),
                "stream": args.stream,
                "query_sha256": digest_bytes(args.query.encode("utf-8"))
                if args.query is not None
                else None,
                "start_line": args.start_line,
                "end_line": args.end_line,
            },
        )
        sys.stdout.buffer.write(raw)
        return excerpt.returncode
    finally:
        conn.close()


def event_index(conn: sqlite3.Connection, since_seq: int, max_events: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seq,event_id,parent_event_id,event_type,created_at,payload_sha256,"
        "model_facing_bytes FROM events WHERE seq>? ORDER BY seq LIMIT ?",
        (since_seq, max_events),
    ).fetchall()
    return [dict(row) for row in rows]


def cmd_change(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        valid, errors = verify_chain(conn)
        events = event_index(conn, args.since_seq, args.max_events)
        latest_row = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()
        latest_seq = int(latest_row[0])
        result = {
            "schema_version": LEDGER_SCHEMA,
            "kind": "audit" if args.audit else "change",
            "ledger_valid": valid,
            "ledger_errors": errors,
            "since_seq": 0 if args.audit else args.since_seq,
            "latest_seq_before_view": latest_seq,
            "truncated": bool(events and events[-1]["seq"] < latest_seq),
            "events": events,
        }
        kind = "audit" if args.audit else "change"
        raw = record_model_view(
            conn,
            spec,
            kind,
            result,
            detail={"since_seq": result["since_seq"], "event_count": len(events)},
        )
        sys.stdout.buffer.write(raw)
        return 0 if valid else 2
    finally:
        conn.close()


def evidence_records(root: Path, values: list[str]) -> list[dict[str, Any]]:
    records = []
    for value in values:
        path = resolve_within(root, value)
        if not path.is_file():
            raise LedgerError(f"evidence is not a file: {value}")
        size, digest = digest_file(path)
        records.append({"path": relative(root, path), "bytes": size, "sha256": digest})
    return records


def cmd_close(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        state = meta_get(conn, "state")
        if state not in ("running", "needs_review"):
            raise LedgerError(f"stage is already terminal: {state}")
        valid, errors = verify_chain(conn)
        if not valid:
            raise LedgerError("cannot close an invalid ledger: " + "; ".join(errors))
        evidence = evidence_records(root, args.evidence)
        terminal = {"go": "accepted", "stop": "rejected", "defer": "deferred"}[args.result]
        result = {
            "schema_version": LEDGER_SCHEMA,
            "kind": "stage_close",
            "task_id": spec["task_id"],
            "stage_id": spec["stage_id"],
            "objective": spec["objective"],
            "decision": {
                "result": args.result,
                "rationale": args.rationale,
                "evidence": evidence,
            },
            "terminal_state": terminal,
            "budget": budget_snapshot(conn, spec),
        }
        raw = output_bytes(result)
        append_event(
            conn,
            "stage_closed",
            result,
            model_facing_bytes=len(raw),
        )
        meta_set(conn, "state", terminal)
        conn.commit()
        sys.stdout.buffer.write(raw)
        return 0
    finally:
        conn.close()


def cmd_handoff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, conn = load_ledger(root, args.db)
    try:
        spec = stage_spec(conn)
        valid, errors = verify_chain(conn)
        row = conn.execute(
            "SELECT seq,event_id,payload_json FROM events WHERE event_type='stage_closed' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise LedgerError("stage has not been closed")
        close_payload = json.loads(row["payload_json"])
        result = {
            "schema_version": LEDGER_SCHEMA,
            "kind": "handoff",
            "ledger_valid": valid,
            "ledger_errors": errors,
            "task_id": spec["task_id"],
            "stage_id": spec["stage_id"],
            "objective": spec["objective"],
            "acceptance_checks": spec["acceptance_checks"],
            "state": meta_get(conn, "state"),
            "decision": close_payload["decision"],
            "stage_close_event": row["event_id"],
            "budget": budget_snapshot(conn, spec),
        }
        raw = record_model_view(conn, spec, "handoff", result)
        sys.stdout.buffer.write(raw)
        return 0 if valid else 2
    finally:
        conn.close()


def normalize_command(command: list[str]) -> list[str]:
    return command[1:] if command and command[0] == "--" else command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    init = sub.add_parser("init")
    init.add_argument("--root", required=True)
    init.add_argument("--stage", required=True)
    init.add_argument("--db", required=True)
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--db", required=True)
    run.add_argument("--token-guard", required=True)
    run.add_argument("--cwd")
    run.add_argument("--input", action="append", default=[])
    run.add_argument("--artifacts", default=".token-guard/ledger-runs")
    run.add_argument("--inline-bytes", type=int, default=2048)
    run.add_argument("--failure-inline-bytes", type=int, default=4096)
    run.add_argument("--timeout", type=float, default=0)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    execute = sub.add_parser("execute")
    execute.add_argument("--root", required=True)
    execute.add_argument("--stage", required=True)
    execute.add_argument("--db", required=True)
    execute.set_defaults(func=cmd_execute)

    verify = sub.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--db", required=True)
    verify.set_defaults(func=cmd_verify)
    status = sub.add_parser("status")
    status.add_argument("--root", required=True)
    status.add_argument("--db", required=True)
    status.set_defaults(func=cmd_status)

    evidence = sub.add_parser("evidence")
    evidence.add_argument("--root", required=True)
    evidence.add_argument("--db", required=True)
    evidence.add_argument("--token-guard", required=True)
    evidence.add_argument("--receipt", required=True)
    evidence.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    evidence.add_argument("--query")
    evidence.add_argument("--start-line", type=int)
    evidence.add_argument("--end-line", type=int)
    evidence.add_argument("--max-bytes", type=int, default=8192)
    evidence.set_defaults(func=cmd_evidence)

    change = sub.add_parser("change")
    change.add_argument("--root", required=True)
    change.add_argument("--db", required=True)
    change.add_argument("--since-seq", type=int, default=0)
    change.add_argument("--max-events", type=int, default=50)
    change.add_argument("--audit", action="store_true")
    change.set_defaults(func=cmd_change)

    close = sub.add_parser("close")
    close.add_argument("--root", required=True)
    close.add_argument("--db", required=True)
    close.add_argument("--result", choices=("go", "stop", "defer"), required=True)
    close.add_argument("--rationale", required=True)
    close.add_argument("--evidence", action="append", default=[])
    close.set_defaults(func=cmd_close)

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--root", required=True)
    handoff.add_argument("--db", required=True)
    handoff.set_defaults(func=cmd_handoff)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "command"):
        args.command = normalize_command(args.command)
    if getattr(args, "inline_bytes", 1) <= 0 or getattr(args, "failure_inline_bytes", 1) <= 0:
        parser.error("inline byte budgets must be positive")
    if getattr(args, "timeout", 0) < 0:
        parser.error("timeout must be non-negative")
    if getattr(args, "max_bytes", 1) <= 0:
        parser.error("max bytes must be positive")
    if getattr(args, "since_seq", 0) < 0:
        parser.error("since seq must be non-negative")
    if getattr(args, "max_events", 1) <= 0:
        parser.error("max events must be positive")
    try:
        return int(args.func(args))
    except (LedgerError, sqlite3.Error, OSError) as exc:
        print_json({"schema_version": LEDGER_SCHEMA, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

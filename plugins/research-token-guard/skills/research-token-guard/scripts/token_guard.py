#!/usr/bin/env python3
"""Deterministic, recoverable command-output receipts for Codex research tasks."""

from __future__ import annotations

import argparse
import codecs
import collections
import datetime as dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Iterable


SCHEMA_VERSION = "research-token-guard/v1"
DEFAULT_ARTIFACTS = ".token-guard/runs"
DEFAULT_INLINE_BYTES = 12_000
DEFAULT_FAILURE_INLINE_BYTES = 24_000
DEFAULT_EXCERPT_BYTES = 8_192
MAX_QUERY_CHARS = 256
MAX_SIGNAL_LINES = 96
MAX_SIGNAL_LINE_CHARS = 2_000
CHUNK_SIZE = 64 * 1024

SIGNAL_RE = re.compile(
    r"(?i)(traceback|exception|\berror\b|\bfail(?:ed|ure)?\b|warning|warn:|"
    r"\bauc\b|accuracy|precision|recall|f1(?:[-_ ]?score)?|eer|loss|metric|"
    r"baseline|bootstrap|confidence|p[-_ ]?value|status|summary|result|"
    r"\bnan\b|\binf\b|\boom\b|out of memory|killed|timeout|assert)"
)
SECRET_FLAG_RE = re.compile(
    r"(?i)(api[-_]?key|token|password|passwd|secret|credential|authorization)"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,}|"
    r"(?:api[-_]?key|token|password|secret)\s*[=:]\s*\S+)"
)


class GuardError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def json_dump(data: Any, path: Path | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if path is not None:
        path.write_text(text + "\n", encoding="utf-8")
    return text


def print_json(data: Any) -> None:
    """Emit JSON safely even when the host console is not UTF-8."""
    text = json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    sys.stdout.write(text + "\n")


def resolve_within(root: Path, candidate: Path, *, must_exist: bool = False) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=must_exist)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise GuardError(f"path escapes root: {candidate}") from exc
    return resolved


def relative_display(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if arg.startswith("-") and SECRET_FLAG_RE.search(arg):
            if "=" in arg:
                redacted.append(arg.split("=", 1)[0] + "=<redacted>")
            else:
                redacted.append(arg)
                redact_next = True
            continue
        redacted.append(SECRET_VALUE_RE.sub("<redacted>", arg))
    return redacted


class StreamCapture:
    def __init__(self, source: BinaryIO, destination: Path) -> None:
        self.source = source
        self.destination = destination
        self.byte_count = 0
        self.sha256 = hashlib.sha256()
        self.error: str | None = None

    def run(self) -> None:
        try:
            with self.destination.open("wb") as target:
                while True:
                    chunk = self.source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
                    self.sha256.update(chunk)
                    self.byte_count += len(chunk)
        except Exception as exc:  # capture failures must be reported, not hidden
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def digest(self) -> str:
        return self.sha256.hexdigest()


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def count_lines(path: Path) -> int:
    count = 0
    last = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:] if chunk else last
    if path.stat().st_size and last != b"\n":
        count += 1
    return count


def is_valid_utf8(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
        return True
    except UnicodeDecodeError:
        return False


def decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def bounded_head(path: Path, budget: int) -> str:
    with path.open("rb") as handle:
        return decode_bytes(handle.read(max(0, budget)))


def bounded_tail(path: Path, budget: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - max(0, budget)))
        data = handle.read(max(0, budget))
    if size > budget:
        newline = data.find(b"\n")
        if newline >= 0:
            data = data[newline + 1 :]
    return decode_bytes(data)


def signal_lines(
    path: Path, max_lines: int = MAX_SIGNAL_LINES, max_bytes: int = DEFAULT_EXCERPT_BYTES
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    used_bytes = 0
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            text = decode_bytes(raw_line).rstrip("\r\n")
            if not SIGNAL_RE.search(text):
                continue
            clipped = text[:MAX_SIGNAL_LINE_CHARS]
            key = clipped.strip()
            if key in seen:
                for item in found:
                    if item["text"] == key:
                        item["repeat_count"] += 1
                        break
                continue
            encoded_size = len(key.encode("utf-8"))
            if used_bytes + encoded_size > max_bytes:
                break
            seen[key] = line_no
            found.append({"line": line_no, "text": key, "repeat_count": 1})
            used_bytes += encoded_size
            if len(found) >= max_lines:
                break
    return found


def stream_metadata(root: Path, path: Path, capture: StreamCapture) -> dict[str, Any]:
    return {
        "path": relative_display(root, path),
        "bytes": capture.byte_count,
        "lines": count_lines(path),
        "utf8_valid": is_valid_utf8(path),
        "sha256": capture.digest,
        "capture_error": capture.error,
    }


def full_inline(stdout_path: Path, stderr_path: Path) -> dict[str, str]:
    return {
        "stdout": decode_bytes(stdout_path.read_bytes()),
        "stderr": decode_bytes(stderr_path.read_bytes()),
    }


def compact_inline(stdout_path: Path, stderr_path: Path, budget: int) -> dict[str, Any]:
    stderr_budget = budget // 2
    stdout_budget = budget - stderr_budget
    result: dict[str, Any] = {}
    for name, path, stream_budget in (
        ("stdout", stdout_path, stdout_budget),
        ("stderr", stderr_path, stderr_budget),
    ):
        head_budget = stream_budget // 5
        tail_budget = stream_budget // 4
        signal_budget = max(0, stream_budget - head_budget - tail_budget)
        result[name] = {
            "head": bounded_head(path, head_budget),
            "tail": bounded_tail(path, tail_budget),
            "signals": signal_lines(path, max_bytes=signal_budget),
        }
    return result


def make_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def command_hash(argv: list[str]) -> str:
    payload = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        raise GuardError("missing command after --")
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise GuardError(f"root is not a directory: {root}")
    cwd = resolve_within(root, Path(args.cwd) if args.cwd else root, must_exist=True)
    if not cwd.is_dir():
        raise GuardError(f"cwd is not a directory: {cwd}")
    artifacts_arg = Path(args.artifacts)
    artifacts_base = artifacts_arg if artifacts_arg.is_absolute() else root / artifacts_arg
    artifacts_base = resolve_within(root, artifacts_base)
    run_id = make_run_id()
    run_dir = artifacts_base / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    receipt_path = run_dir / "receipt.json"

    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    stdout_capture: StreamCapture | None = None
    stderr_capture: StreamCapture | None = None

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(args.command, **popen_kwargs)
        assert proc.stdout is not None and proc.stderr is not None
        stdout_capture = StreamCapture(proc.stdout, stdout_path)
        stderr_capture = StreamCapture(proc.stderr, stderr_path)
        threads = [
            threading.Thread(target=stdout_capture.run, name="stdout-capture", daemon=True),
            threading.Thread(target=stderr_capture.run, name="stderr-capture", daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            exit_code = proc.wait(timeout=args.timeout if args.timeout > 0 else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(proc)
            exit_code = proc.returncode
        for thread in threads:
            thread.join()
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        stdout_path.touch(exist_ok=True)
        stderr_path.write_text(launch_error + "\n", encoding="utf-8")
        stdout_capture = StreamCapture(open(os.devnull, "rb"), stdout_path)
        stderr_capture = StreamCapture(open(os.devnull, "rb"), stderr_path)
        stdout_capture.byte_count = stdout_path.stat().st_size
        stderr_data = stderr_path.read_bytes()
        stderr_capture.byte_count = len(stderr_data)
        stderr_capture.sha256.update(stderr_data)

    assert stdout_capture is not None and stderr_capture is not None
    duration_s = round(time.monotonic() - started, 3)
    combined_bytes = stdout_path.stat().st_size + stderr_path.stat().st_size
    failed = launch_error is not None or timed_out or exit_code not in (0, None)
    inline_budget = args.failure_inline_bytes if failed else args.inline_bytes
    lossy = combined_bytes > inline_budget
    if lossy:
        inline_output: dict[str, Any] = compact_inline(stdout_path, stderr_path, inline_budget)
        returned_bytes = len(json.dumps(inline_output, ensure_ascii=False).encode("utf-8"))
    else:
        inline_output = full_inline(stdout_path, stderr_path)
        returned_bytes = combined_bytes

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": duration_s,
        "root": str(root),
        "cwd": relative_display(root, cwd),
        "command": redact_argv(args.command),
        "command_sha256": command_hash(args.command),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "lossy": lossy,
        "compression_applied": lossy,
        "raw_bytes": combined_bytes,
        "returned_bytes": returned_bytes,
        "omitted_bytes_lower_bound": max(0, combined_bytes - returned_bytes),
        "inline_budget_bytes": inline_budget,
        "streams": {
            "stdout": stream_metadata(root, stdout_path, stdout_capture),
            "stderr": stream_metadata(root, stderr_path, stderr_capture),
        },
        "inline_output": inline_output,
        "recovery": {
            "receipt": relative_display(root, receipt_path),
            "hint": "Use excerpt with a literal query or exact line range; run verify before evidence-critical conclusions.",
        },
    }
    json_dump(receipt, receipt_path)
    print_json(receipt)
    return 0 if not failed else 1


def load_receipt(root: Path, receipt_arg: str) -> tuple[Path, dict[str, Any]]:
    candidate = Path(receipt_arg)
    if not candidate.is_absolute():
        candidate = root / candidate
    path = resolve_within(root, candidate, must_exist=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"invalid receipt: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("unsupported receipt schema")
    return path, data


def stream_path_from_receipt(root: Path, receipt: dict[str, Any], stream: str) -> Path:
    try:
        raw = receipt["streams"][stream]["path"]
    except (KeyError, TypeError) as exc:
        raise GuardError(f"receipt has no {stream} stream") from exc
    return resolve_within(root, root / raw, must_exist=True)


def take_utf8_budget(lines: Iterable[str], max_bytes: int) -> tuple[list[str], bool]:
    selected: list[str] = []
    used = 0
    truncated = False
    for text in lines:
        encoded = text.encode("utf-8")
        if used + len(encoded) > max_bytes:
            truncated = True
            break
        selected.append(text)
        used += len(encoded)
    return selected, truncated


def cmd_excerpt(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _, receipt = load_receipt(root, args.receipt)
    path = stream_path_from_receipt(root, receipt, args.stream)
    if args.query and (args.start_line or args.end_line):
        raise GuardError("choose query or line range, not both")
    if not args.query and not args.start_line and not args.end_line:
        raise GuardError("provide --query or a line range")
    if args.query and len(args.query) > MAX_QUERY_CHARS:
        raise GuardError(f"query exceeds {MAX_QUERY_CHARS} characters")
    start = args.start_line or 1
    end = args.end_line or start
    if start < 1 or end < start:
        raise GuardError("invalid line range")

    query = args.query.casefold() if args.query else None
    selected: list[str] = []
    used_bytes = 0
    truncated = False
    match_count = 0
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            text = decode_bytes(raw_line).rstrip("\r\n")
            include = query in text.casefold() if query is not None else start <= line_no <= end
            if not include:
                continue
            match_count += 1
            rendered = f"{line_no}: {text}\n"
            rendered_size = len(rendered.encode("utf-8"))
            if used_bytes + rendered_size <= args.max_bytes:
                selected.append(rendered)
                used_bytes += rendered_size
            else:
                truncated = True
    result = {
        "schema_version": SCHEMA_VERSION,
        "receipt_run_id": receipt["run_id"],
        "stream": args.stream,
        "stream_sha256": receipt["streams"][args.stream]["sha256"],
        "query": args.query,
        "start_line": args.start_line,
        "end_line": args.end_line,
        "match_count": match_count,
        "returned_match_count": len(selected),
        "truncated": truncated,
        "max_bytes": args.max_bytes,
        "content": "".join(selected),
    }
    print_json(result)
    return 0


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    receipt_path, receipt = load_receipt(root, args.receipt)
    checks: dict[str, Any] = {}
    valid = True
    for stream in ("stdout", "stderr"):
        path = stream_path_from_receipt(root, receipt, stream)
        size, digest = file_digest(path)
        expected = receipt["streams"][stream]
        stream_valid = size == expected["bytes"] and digest == expected["sha256"]
        checks[stream] = {
            "valid": stream_valid,
            "bytes": size,
            "expected_bytes": expected["bytes"],
            "sha256": digest,
            "expected_sha256": expected["sha256"],
        }
        valid = valid and stream_valid
    result = {
        "schema_version": SCHEMA_VERSION,
        "receipt": relative_display(root, receipt_path),
        "run_id": receipt["run_id"],
        "valid": valid,
        "checks": checks,
    }
    print_json(result)
    return 0 if valid else 2


def cmd_stats(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifacts_arg = Path(args.artifacts)
    artifacts = artifacts_arg if artifacts_arg.is_absolute() else root / artifacts_arg
    artifacts = resolve_within(root, artifacts)
    receipts = sorted(artifacts.glob("*/receipt.json")) if artifacts.exists() else []
    total_raw = 0
    total_returned = 0
    lossy_runs = 0
    failures = 0
    invalid = 0
    for path in receipts:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("schema_version") != SCHEMA_VERSION:
                invalid += 1
                continue
            total_raw += int(item.get("raw_bytes", 0))
            total_returned += int(item.get("returned_bytes", 0))
            lossy_runs += int(bool(item.get("lossy")))
            failures += int(
                bool(item.get("timed_out"))
                or item.get("launch_error") is not None
                or item.get("exit_code") not in (0, None)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            invalid += 1
    saved = max(0, total_raw - total_returned)
    result = {
        "schema_version": SCHEMA_VERSION,
        "runs": len(receipts) - invalid,
        "invalid_receipts": invalid,
        "lossy_runs": lossy_runs,
        "failed_runs": failures,
        "raw_bytes": total_raw,
        "returned_bytes": total_returned,
        "saved_bytes": saved,
        "reduction_percent": round(saved * 100 / total_raw, 2) if total_raw else 0.0,
        "estimated_saved_tokens": saved // 4,
        "estimate_warning": "Character/byte estimates are not provider billing or tokenizer counts.",
    }
    print_json(result)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    version = sys.version_info
    ok = version >= (3, 9)
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "rtk_optional_path": shutil.which("rtk"),
        "external_model_used": False,
        "global_hook_required": False,
    }
    print_json(result)
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture exact command output and return a bounded, recoverable receipt."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run = sub.add_parser("run", help="run a command without a shell and save exact output")
    run.add_argument("--root", required=True, help="workspace root and artifact security boundary")
    run.add_argument("--cwd", help="command working directory; must remain under root")
    run.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    run.add_argument("--inline-bytes", type=int, default=DEFAULT_INLINE_BYTES)
    run.add_argument("--failure-inline-bytes", type=int, default=DEFAULT_FAILURE_INLINE_BYTES)
    run.add_argument("--timeout", type=float, default=0, help="seconds; 0 disables timeout")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    excerpt = sub.add_parser("excerpt", help="recover exact matching or ranged lines")
    excerpt.add_argument("--root", required=True)
    excerpt.add_argument("--receipt", required=True)
    excerpt.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    excerpt.add_argument("--query", help="case-insensitive literal query")
    excerpt.add_argument("--start-line", type=int)
    excerpt.add_argument("--end-line", type=int)
    excerpt.add_argument("--max-bytes", type=int, default=DEFAULT_EXCERPT_BYTES)
    excerpt.set_defaults(func=cmd_excerpt)

    verify = sub.add_parser("verify", help="verify raw files against receipt hashes")
    verify.add_argument("--root", required=True)
    verify.add_argument("--receipt", required=True)
    verify.set_defaults(func=cmd_verify)

    stats = sub.add_parser("stats", help="aggregate local receipt byte savings")
    stats.add_argument("--root", required=True)
    stats.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    stats.set_defaults(func=cmd_stats)

    doctor = sub.add_parser("doctor", help="check the dependency-free runtime")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def normalize_remainder(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "command"):
        args.command = normalize_remainder(args.command)
    for field in ("inline_bytes", "failure_inline_bytes", "max_bytes"):
        value = getattr(args, field, None)
        if value is not None and value <= 0:
            parser.error(f"--{field.replace('_', '-')} must be positive")
    if getattr(args, "timeout", 0) < 0:
        parser.error("--timeout must be non-negative")
    try:
        return int(args.func(args))
    except GuardError as exc:
        print_json({"schema_version": SCHEMA_VERSION, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from solution_agent.harness import verify_token_guard_receipt


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_verify_token_guard_receipt_checks_both_raw_streams(tmp_path: Path) -> None:
    run_dir = tmp_path / ".token-guard" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    stdout = b"metric=0.91\n"
    stderr = b""
    (run_dir / "stdout.log").write_bytes(stdout)
    (run_dir / "stderr.log").write_bytes(stderr)
    receipt = {
        "run_id": "r1",
        "command_sha256": "abc",
        "exit_code": 0,
        "timed_out": False,
        "streams": {
            "stdout": {
                "path": ".token-guard/runs/r1/stdout.log",
                "bytes": len(stdout),
                "sha256": _hash(stdout),
            },
            "stderr": {
                "path": ".token-guard/runs/r1/stderr.log",
                "bytes": len(stderr),
                "sha256": _hash(stderr),
            },
        },
    }
    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    verified = verify_token_guard_receipt(receipt_path, root=tmp_path)

    assert verified.verified is True
    assert {stream.name for stream in verified.streams} == {"stdout", "stderr"}


def test_verify_token_guard_receipt_rejects_modified_raw_stream(tmp_path: Path) -> None:
    run_dir = tmp_path / ".token-guard" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "stdout.log").write_text("changed", encoding="utf-8")
    (run_dir / "stderr.log").write_bytes(b"")
    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "r1",
                "command_sha256": "abc",
                "exit_code": 0,
                "timed_out": False,
                "streams": {
                    "stdout": {
                        "path": ".token-guard/runs/r1/stdout.log",
                        "bytes": 3,
                        "sha256": _hash(b"old"),
                    },
                    "stderr": {
                        "path": ".token-guard/runs/r1/stderr.log",
                        "bytes": 0,
                        "sha256": _hash(b""),
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stream verification failed: stdout"):
        verify_token_guard_receipt(receipt_path, root=tmp_path)

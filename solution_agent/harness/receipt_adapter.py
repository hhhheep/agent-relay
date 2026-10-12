from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import VerifiedReceipt, VerifiedStream


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def verify_token_guard_receipt(receipt_path: Path, *, root: Path) -> VerifiedReceipt:
    """Verify the raw stream hashes referenced by a Token Guard receipt."""

    root = root.resolve()
    receipt_path = receipt_path.resolve()
    if not _under_root(receipt_path, root):
        raise ValueError("receipt path is outside root")
    payload: dict[str, Any] = json.loads(receipt_path.read_text(encoding="utf-8"))
    streams: list[VerifiedStream] = []
    for name in ("stdout", "stderr"):
        stream = payload.get("streams", {}).get(name)
        if not isinstance(stream, dict):
            raise ValueError(f"receipt is missing stream metadata: {name}")
        raw_path = Path(str(stream.get("path") or ""))
        path = raw_path if raw_path.is_absolute() else root / raw_path
        path = path.resolve()
        if not _under_root(path, root):
            raise ValueError(f"stream path is outside root: {name}")
        if not path.is_file():
            raise ValueError(f"stream path does not exist: {name}")
        expected_hash = str(stream.get("sha256") or "")
        actual_hash = _sha256(path)
        expected_bytes = int(stream.get("bytes", -1))
        actual_bytes = path.stat().st_size
        verified = expected_hash == actual_hash and expected_bytes == actual_bytes
        if not verified:
            raise ValueError(f"stream verification failed: {name}")
        streams.append(
            VerifiedStream(
                name=name,
                path=str(path.relative_to(root)).replace("\\", "/"),
                bytes=actual_bytes,
                sha256=actual_hash,
                verified=True,
            )
        )

    return VerifiedReceipt(
        receipt_path=str(receipt_path.relative_to(root)).replace("\\", "/"),
        run_id=str(payload.get("run_id") or ""),
        command_sha256=str(payload.get("command_sha256") or ""),
        exit_code=payload.get("exit_code"),
        timed_out=bool(payload.get("timed_out", False)),
        streams=streams,
        verified=True,
    )

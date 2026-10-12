#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe release path: {value}")
    return path


def inspect_release(archive_path: Path, expected_sha256: str, target_root: Path) -> dict:
    actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256.lower():
        raise ValueError("bootstrap release SHA-256 mismatch")
    target_root = target_root.resolve()
    target_state = "absent"
    if target_root.exists():
        target_state = "empty" if target_root.is_dir() and not any(target_root.iterdir()) else "nonempty"
    if target_state == "nonempty":
        raise FileExistsError("target root exists and is non-empty; bootstrap installer never overwrites")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
        if manifest.get("schema_version") not in {"solution-agent-bootstrap/v1", "solution-agent-harness/0.6"}:
            raise ValueError("unsupported bootstrap release manifest")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise ValueError("bootstrap release contains no files")
        verified = []
        for entry in entries:
            relative = _safe_path(str(entry.get("path") or ""))
            payload = archive.read("release/" + relative.as_posix())
            if len(payload) != entry.get("bytes") or _sha256_bytes(payload) != entry.get("sha256"):
                raise ValueError(f"bootstrap release file mismatch: {relative}")
            verified.append(relative.as_posix())
    return {
        "archive": str(archive_path.resolve()),
        "archive_sha256": actual_sha256,
        "target_root": str(target_root),
        "target_state": target_state,
        "file_count": len(verified),
        "safe": True,
        "conflicts": [],
        "files": verified,
    }


def install_release(archive_path: Path, expected_sha256: str, target_root: Path, *, apply: bool = False) -> dict:
    result = inspect_release(archive_path, expected_sha256, target_root)
    result["mode"] = "apply" if apply else "dry_run"
    result["applied"] = False
    if not apply:
        return result
    target_root = Path(result["target_root"])
    target_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target_root.name}.bootstrap-", dir=target_root.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for relative_text in result["files"]:
                relative = _safe_path(relative_text)
                destination = temporary.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = archive.read("release/" + relative.as_posix())
                with destination.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
        if target_root.exists():
            target_root.rmdir()
        os.replace(temporary, target_root)
        result["applied"] = True
        result["install_receipt"] = str(target_root / "bootstrap-install-receipt.json")
        receipt = {key: value for key, value in result.items() if key != "files"}
        (target_root / "bootstrap-install-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = install_release(args.archive, args.expected_sha256, args.target_root, apply=args.apply)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

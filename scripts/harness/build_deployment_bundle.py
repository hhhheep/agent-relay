#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.release_identity import build_runtime_identity, canonical_json_sha256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe overlay path: {value}")
    return path.as_posix()


def build_bundle(overlay_root: Path, baseline_root: Path, file_list: Path, output: Path) -> dict:
    overlay_root = overlay_root.resolve()
    baseline_root = baseline_root.resolve()
    paths = [
        _safe_relative(line.strip())
        for line in file_list.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("overlay file list contains duplicates")
    entries: list[dict[str, str | int | None]] = []
    for relative in paths:
        source = overlay_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"overlay file missing: {relative}")
        baseline = baseline_root / relative
        entries.append(
            {
                "path": relative,
                "base_sha256": _sha256(baseline) if baseline.is_file() else None,
                "overlay_sha256": _sha256(source),
                "bytes": source.stat().st_size,
            }
        )
    release: dict | None = None
    try:
        identity = build_runtime_identity(overlay_root)
        identity_payload = asdict(identity)
        release = {
            "release_id": identity.release_id,
            "source_root_sha256": identity.source_root_sha256,
            "mcp_schema_sha256": identity.mcp_schema_sha256,
            "release_manifest_sha256": canonical_json_sha256(identity_payload),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        # Unit fixtures may exercise the generic overlay builder without a
        # Harness source tree. Production Harness bundles always have identity.
        release = None
    payload_hash = canonical_json_sha256({entry["path"]: entry["overlay_sha256"] for entry in entries})
    release_id = (release or {}).get("release_id") or f"harness-0.6-{payload_hash[:16]}"
    manifest = {
        "schema_version": "2.0",
        "protocol_version": "0.6",
        "release_id": release_id,
        "payload_sha256": payload_hash,
        "inventory_kind": "complete_managed_release",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "delete_files": True,
            "overwrite_changed_target": False,
            "contains_credentials": False,
            "unknown_files_are_protected": True,
        },
        "release": release,
        "files": entries,
        "obsolete": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for entry in entries:
            archive.write(overlay_root / str(entry["path"]), "files/" + str(entry["path"]), compress_type=zipfile.ZIP_DEFLATED)
    result = {
        "bundle": str(output.resolve()),
        "bundle_sha256": _sha256(output),
        "file_count": len(entries),
        "manifest": manifest,
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overlay-root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--file-list", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_bundle(args.overlay_root, args.baseline_root, args.file_list, args.output)
    print(json.dumps({key: result[key] for key in ("bundle", "bundle_sha256", "file_count")}, sort_keys=True))


if __name__ == "__main__":
    main()

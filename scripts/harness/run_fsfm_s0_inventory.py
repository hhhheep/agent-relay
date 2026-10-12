#!/usr/bin/env python3
"""Bounded, metadata-only inventory for the FAN FSFM S0 gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


DATASET_LABELS = ("ffpp", "dfdc", "celeb_df_v2")
MAX_DEPTH = 2
MAX_ENTRIES_PER_DIRECTORY = 128
MAX_DIRECTORIES = 384
SOURCE_FILES = (
    "fsfm-3c/pretrain/main_pretrain.py",
    "fsfm-3c/models_fsfm.py",
    "datasets/pretrain/preprocess/dataset_preprocess.py",
    "datasets/finetune/preprocess/dataset_preprocess.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(path: Path, relative: str) -> dict[str, Any]:
    info = path.lstat()
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "directory"
    elif path.is_file():
        kind = "file"
    else:
        kind = "other"
    return {"relative": relative, "kind": kind, "size_bytes": info.st_size if kind == "file" else None}


def inventory(root: Path, *, depth: int = MAX_DEPTH) -> dict[str, Any]:
    if depth < 0 or depth > MAX_DEPTH:
        raise ValueError("depth is outside the fixed bound")
    result: dict[str, Any] = {
        "root": str(root),
        "exists": root.is_dir() and not root.is_symlink(),
        "entries": [],
        "truncated": False,
        "errors": [],
    }
    if not result["exists"]:
        return result
    pending = [(root, 0)]
    directory_count = 0
    while pending:
        current, level = pending.pop(0)
        directory_count += 1
        if directory_count > MAX_DIRECTORIES:
            result["truncated"] = True
            break
        try:
            with os.scandir(current) as iterator:
                children = []
                for item in iterator:
                    if len(children) >= MAX_ENTRIES_PER_DIRECTORY:
                        result["truncated"] = True
                        break
                    children.append(Path(item.path))
        except OSError as exc:
            result["errors"].append({"relative": str(current.relative_to(root)), "error": type(exc).__name__})
            continue
        for child in sorted(children, key=lambda item: item.name.casefold()):
            relative = child.relative_to(root).as_posix()
            try:
                row = _entry(child, relative)
            except OSError as exc:
                result["errors"].append({"relative": relative, "error": type(exc).__name__})
                continue
            result["entries"].append(row)
            if row["kind"] == "directory" and level < depth:
                pending.append((child, level + 1))
    result["directory_count"] = directory_count
    result["entry_count"] = len(result["entries"])
    return result


def source_inventory(repo_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"root": str(repo_root), "exists": False, "git_head": None, "files": {}}
    head = repo_root / ".git" / "HEAD"
    if head.is_file() and head.stat().st_size <= 1024:
        value = head.read_text(encoding="utf-8", errors="replace").strip()
        if value.startswith("ref: "):
            ref = repo_root / ".git" / value[5:]
            if ref.is_file() and ref.stat().st_size <= 1024:
                result["git_head"] = ref.read_text(encoding="utf-8", errors="replace").strip()
        elif len(value) == 40:
            result["git_head"] = value
    for relative in SOURCE_FILES:
        path = repo_root / relative
        present = path.is_file() and not path.is_symlink()
        result["files"][relative] = {
            "exists": present,
            "sha256": _sha256(path) if present else None,
        }
    result["exists"] = repo_root.is_dir() and all(item["exists"] for item in result["files"].values())
    return result


def run(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    roots = {label: Path(getattr(args, f"{label}_root")) for label in DATASET_LABELS}
    datasets = {label: inventory(root) for label, root in roots.items()}
    sources = source_inventory(Path(args.repo_root))
    receipt = {
        "schema_version": 1,
        "stage": "FSFM_S0_INVENTORY",
        "scope": "metadata_only",
        "limits": {
            "max_depth": MAX_DEPTH,
            "max_entries_per_directory": MAX_ENTRIES_PER_DIRECTORY,
            "max_directories_per_root": MAX_DIRECTORIES,
        },
        "datasets": datasets,
        "upstream": sources,
        "image_pixels_read": False,
        "raw_data_modified": False,
    }
    missing_count = sum(not item["exists"] for item in datasets.values())
    metrics = {
        "execution_status": "completed",
        "contract_status": "pass" if missing_count == 0 and not any(item["truncated"] or item["errors"] for item in datasets.values()) else "defer",
        "science_status": "not_run",
        "dataset_roots_observed": len(datasets) - missing_count,
        "dataset_roots_missing": missing_count,
        "upstream_checkout_present": sources["exists"],
        "image_pixels_read": False,
        "write_enabled": False,
    }
    return receipt, metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffpp_root", required=True)
    parser.add_argument("--dfdc_root", required=True)
    parser.add_argument("--celeb_df_v2_root", required=True)
    parser.add_argument("--repo_root", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    receipt, metrics = run(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "fsfm_s0_inventory.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "fsfm_s0_inventory_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": metrics, "receipt": str(output_dir / "fsfm_s0_inventory.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

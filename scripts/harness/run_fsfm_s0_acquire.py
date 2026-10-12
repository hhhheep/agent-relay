#!/usr/bin/env python3
"""Acquire the pinned FSFM source and, when requested, verified model assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


GIT_URL = "https://github.com/wolo-wolo/FSFM-CVPR25.git"
EXPECTED_COMMIT = "986aaab6d505911f73042ee91cc49c8621354c05"
HF_ROOT = "https://huggingface.co/Wolowolo/fsfm-3c/resolve/main/pretrained_models/VF2_ViT-B"
ASSETS = (
    ("checkpoint-400.pth", "8ff8429f8a516d0285f1df5f6bb68432fee820dbf1d5753d247fd30a651c04cc"),
    ("checkpoint-te-400.pth", "014ba8f89595ca1264fd6200a36f3821af19ab73bcd1a49493518bd4058a9fb0"),
    ("pretrain_ds_mean_std.txt", None),
)
SOURCE_FILES = (
    "fsfm-3c/pretrain/main_pretrain.py",
    "fsfm-3c/models_fsfm.py",
    "datasets/pretrain/preprocess/dataset_preprocess.py",
    "datasets/finetune/preprocess/dataset_preprocess.py",
    "fsfm-3c/finuetune/cross_dataset_DfD/engine_finetune.py",
)
MAX_ASSET_BYTES = 2_200_000_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command(argv: list[str], timeout: int) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return result.returncode, (result.stderr or result.stdout)[-1000:]


def source(checkout: Path) -> dict:
    if checkout.is_symlink():
        raise RuntimeError("checkout target is a symlink")
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        partial = checkout.with_name(checkout.name + ".partial")
        if partial.exists() or partial.is_symlink():
            raise RuntimeError("source partial target already exists; manual reconciliation required")
        code, detail = command(["git", "clone", "--depth", "1", "--branch", "main", GIT_URL, str(partial)], 180)
        if code:
            raise RuntimeError(f"git clone failed ({code}): {detail}")
        code, actual = command(["git", "-C", str(partial), "rev-parse", "HEAD"], 30)
        if code or actual.strip() != EXPECTED_COMMIT:
            raise RuntimeError(f"upstream commit changed; expected {EXPECTED_COMMIT}, observed {actual.strip()}")
        if checkout.exists():
            raise RuntimeError("checkout target appeared during acquisition")
        partial.rename(checkout)
    code, actual = command(["git", "-C", str(checkout), "rev-parse", "HEAD"], 30)
    if code or actual.strip() != EXPECTED_COMMIT:
        raise RuntimeError("existing checkout is not the pinned commit")
    files = {}
    for relative in SOURCE_FILES:
        path = checkout / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"expected source file missing: {relative}")
        files[relative] = sha256(path)
    return {"path": str(checkout), "commit": EXPECTED_COMMIT, "source_sha256": files}


def validate_normalization(path: Path) -> None:
    if path.stat().st_size > 4096:
        raise RuntimeError("normalization file exceeds expected small-text bound")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(set(row) != {"mean", "std"} for row in rows):
        raise RuntimeError("normalization format is unexpected")
    if any(len(row["mean"]) != 3 or len(row["std"]) != 3 for row in rows):
        raise RuntimeError("normalization channel count is unexpected")


def asset(weights: Path, name: str, expected_sha256: str | None) -> dict:
    weights.mkdir(parents=True, exist_ok=True)
    if weights.is_symlink():
        raise RuntimeError("weights target is a symlink")
    target = weights / name
    if target.is_symlink():
        raise RuntimeError(f"asset target is a symlink: {name}")
    if not target.is_file():
        partial = weights / (name + ".part")
        if partial.exists() or partial.is_symlink():
            raise RuntimeError(f"partial asset already exists: {name}; manual reconciliation required")
        url = HF_ROOT + "/" + name
        code, detail = command([
            "curl", "--location", "--fail", "--silent", "--show-error",
            "--connect-timeout", "15", "--max-time", "1500", "--retry", "2",
            "--max-filesize", str(MAX_ASSET_BYTES), "--output", str(partial), url,
        ], 1550)
        if code:
            raise RuntimeError(f"download failed for {name} ({code}): {detail}")
        if not partial.is_file() or partial.stat().st_size > MAX_ASSET_BYTES:
            raise RuntimeError(f"asset size invalid: {name}")
        actual = sha256(partial)
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError(f"SHA-256 mismatch for {name}: {actual}")
        if name.endswith(".txt"):
            validate_normalization(partial)
        if target.exists():
            raise RuntimeError(f"asset target appeared during acquisition: {name}")
        partial.rename(target)
    actual = sha256(target)
    if expected_sha256 and actual != expected_sha256:
        raise RuntimeError(f"existing SHA-256 mismatch for {name}: {actual}")
    if name.endswith(".txt"):
        validate_normalization(target)
    return {"path": str(target), "bytes": target.stat().st_size, "sha256": actual}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout_dir", required=True)
    parser.add_argument("--weights_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument(
        "--source_only",
        action="store_true",
        help="Acquire and verify only the GitHub checkout; do not contact Hugging Face.",
    )
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    receipt = {"schema_version": 1, "stage": "FSFM_S0_ACQUIRE", "source": None,
               "assets": {}, "error": None, "gpu_used": False, "checkpoint_unpickled": False}
    try:
        receipt["source"] = source(Path(args.checkout_dir))
        if not args.source_only:
            for name, expected in ASSETS:
                receipt["assets"][name] = asset(Path(args.weights_dir), name, expected)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        receipt["error"] = str(exc)[:1000]
    expected_asset_count = 0 if args.source_only else len(ASSETS)
    complete = (
        receipt["source"] is not None
        and len(receipt["assets"]) == expected_asset_count
        and receipt["error"] is None
    )
    metrics = {"execution_status": "completed", "contract_status": "pass" if complete else "defer",
               "science_status": "not_run", "source_checkout_present": receipt["source"] is not None,
               "source_only": args.source_only, "asset_count_expected": expected_asset_count,
               "asset_count_verified": len(receipt["assets"]), "elapsed_seconds": round(time.monotonic() - started, 3),
               "gpu_used": False, "checkpoint_unpickled": False}
    (output / "fsfm_s0_acquire.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (output / "fsfm_s0_acquire_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": metrics, "error": receipt["error"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

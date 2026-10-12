#!/usr/bin/env python3
"""Run one bounded FSFM S0-boundary audit without loading model/data tensors."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import time
from pathlib import Path


EXPECTED_COMMIT = "986aaab6d505911f73042ee91cc49c8621354c05"
DETECTOR_SHA256 = "74defa99ab77225ed5ba2db34c9e19f2b4707f17c49b06c8396ee638db2e7575"
DETECTOR_XET_HASH = "4d2bf05312e65fe9aee230afb9b61489a1b2166688fd91b459063258a4f6add8"
OFFICIAL_URLS = {
    "paper_html": "https://openaccess.thecvf.com/content/CVPR2025/html/Wang_FSFM_A_Generalizable_Face_Security_Foundation_Model_via_Self-Supervised_Facial_CVPR_2025_paper.html",
    "checkpoint_pointer": "https://huggingface.co/Wolowolo/fsfm-3c/raw/main/finetuned_models/FF%2B%2B_c23_32frames/checkpoint-min_val_loss.pth",
    "release_readme": "https://huggingface.co/Wolowolo/fsfm-3c/raw/main/finetuned_models/FF%2B%2B_c23_32frames/README.md",
    "test_readme": "https://huggingface.co/Wolowolo/fsfm-3c/raw/main/finetuned_models/FF%2B%2B_c23_32frames/experiments_test/README.md",
    "test_results": "https://huggingface.co/Wolowolo/fsfm-3c/raw/main/finetuned_models/FF%2B%2B_c23_32frames/experiments_test/test_results.txt",
    "normalization": "https://huggingface.co/Wolowolo/fsfm-3c/raw/main/finetuned_models/FF%2B%2B_c23_32frames/pretrain_ds_mean_std.txt",
}
OFFICIAL_PAYLOAD_SHA256 = {
    "paper_html": "8b004a1fa91fae61cb3e5fac0a50cf9e3869ec00a6086d1c8e9c84600d20ad87",
    "checkpoint_pointer": "25122f5507dde9c948a3041fb14cfe0880599d3214376a9dd70ce8dafe2ef530",
    "release_readme": "f6cda066adfc70618ef627b2395426a5f66c1dc2ab5d3e24525f264d0d56ac52",
    "test_readme": "471ddbf37d12052ee03145870a2e39a58f8d33c5a5270d997d123706a9cc0839",
    "test_results": "d281e39141a6ccbf06508bf40ce0bf6c289dc118143362c41f3b5e7bf1abed36",
    "normalization": "3a644bdeebe9396ddd59fc22b0fc1b4cc31b42a658e4eef8c794426d1824daa5",
}
METADATA_SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "fsfm_s0_boundary_metadata.json"
SOURCE_FILES = (
    "README.md",
    "datasets/finetune/preprocess/dataset_preprocess.py",
    "datasets/finetune/preprocess/tools/util.py",
    "fsfm-3c/util/datasets.py",
    "fsfm-3c/finuetune/cross_dataset_DfD/engine_finetune.py",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(text: str, needles: tuple[str, ...], label: str) -> None:
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise RuntimeError(f"{label} missing expected anchors: {missing}")


def verify_checkout(repo: Path) -> dict:
    if repo.is_symlink() or not repo.is_dir():
        raise RuntimeError("repo_root must be a real directory, not a symlink")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode or commit != EXPECTED_COMMIT:
        raise RuntimeError(f"pinned checkout mismatch: expected {EXPECTED_COMMIT}, observed {commit}")
    hashes = {}
    texts = {}
    for relative in SOURCE_FILES:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"required official source missing or symlinked: {relative}")
        hashes[relative] = sha256_file(path)
        texts[relative] = path.read_text(encoding="utf-8", errors="replace")
    require(texts["datasets/finetune/preprocess/tools/util.py"], ("face_detector", "SimilarityTransform", "warpAffine"), "face preprocessing")
    require(texts["fsfm-3c/util/datasets.py"], ("Resize", "Normalize", "apply_simple_augment"), "evaluation transform")
    engine = texts["fsfm-3c/finuetune/cross_dataset_DfD/engine_finetune.py"]
    require(engine, ("softmax",), "score semantics")
    if "[:, 1]" not in engine and "[:,1]" not in engine:
        raise RuntimeError("score semantics missing class-1 slice anchor")
    return {"path": str(repo), "commit": commit, "source_sha256": hashes}


def verify_official_metadata() -> tuple[dict, dict[str, str]]:
    if METADATA_SNAPSHOT.is_symlink() or not METADATA_SNAPSHOT.is_file():
        raise RuntimeError("pinned official metadata snapshot is missing or symlinked")
    raw = METADATA_SNAPSHOT.read_bytes()
    if len(raw) > 65536:
        raise RuntimeError("pinned official metadata snapshot exceeds 64 KiB")
    snapshot = json.loads(raw)
    if snapshot.get("schema_version") != 1 or not snapshot.get("captured_at"):
        raise RuntimeError("pinned official metadata snapshot schema is invalid")
    sources = snapshot.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(OFFICIAL_URLS):
        raise RuntimeError("pinned official metadata source set is invalid")
    texts: dict[str, str] = {}
    receipt: dict[str, dict] = {}
    for name, url in OFFICIAL_URLS.items():
        item = sources.get(name)
        if not isinstance(item, dict):
            raise RuntimeError(f"pinned official metadata entry is invalid: {name}")
        if item.get("url") != url or item.get("payload_sha256") != OFFICIAL_PAYLOAD_SHA256[name]:
            raise RuntimeError(f"pinned official metadata provenance mismatch: {name}")
        if not isinstance(item.get("payload_bytes"), int) or item["payload_bytes"] <= 0:
            raise RuntimeError(f"pinned official metadata byte count is invalid: {name}")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"pinned official metadata content is invalid: {name}")
        texts[name] = content
        receipt[name] = {
            "url": url,
            "bytes": item["payload_bytes"],
            "sha256": item["payload_sha256"],
            "verification_mode": "pinned_bounded_snapshot",
            "captured_at": snapshot["captured_at"],
        }
    require(texts["paper_html"], ("FSFM", "CVPR 2025", "10 publicly available datasets"), "CVF paper page")
    require(texts["checkpoint_pointer"], ("version https://git-lfs.github.com/spec/v1", f"oid sha256:{DETECTOR_SHA256}"), "detector LFS pointer")
    require(texts["release_readme"], ("FF++", "c23", "32"), "detector release README")
    require(texts["test_results"], ("91.44", "83.47", "89.71", "86.96", "apply_simple_augment=False"), "published detector results")
    require(texts["normalization"], ("0.5482207536697388", "0.2789176106452942"), "normalization sidecar")
    receipt["snapshot"] = {
        "path": str(METADATA_SNAPSHOT),
        "sha256": sha256_bytes(raw),
        "network_required": False,
    }
    return receipt, texts


def claim_rows() -> list[dict[str, str]]:
    return [
        {
            "claim": "The FF++ C23 fine-tuned FSFM detector was cross-dataset evaluated.",
            "evidence": "Official paper/release report video AUC 91.44/83.47/89.71/86.96 on CDFv2/DFDC/DFDCP/WDF; frame AUC 85.05/80.20/85.50/85.26.",
            "untested_boundary": "No evidence here for a target-derived threshold, target 1% FPR, deployment calibration, local 50-part DFDC, or financial-domain robustness.",
        },
        {
            "claim": "FSFM transfers beyond deepfake detection.",
            "evidence": "The CVPR paper reports experiments across DfD, face anti-spoofing, and diffusion-face forensics on ten public datasets.",
            "untested_boundary": "This is not evidence of universal task/domain coverage or of the local FAN target cohort.",
        },
        {
            "claim": "An author-released FF++ C23 detector checkpoint is obtainable.",
            "evidence": f"Official Hugging Face Git-LFS pointer names checkpoint-min_val_loss.pth and declares SHA-256 {DETECTOR_SHA256}.",
            "untested_boundary": "The stage verifies publisher metadata only; it does not download, locally hash, unpickle, or execute the 1.03 GB checkpoint.",
        },
        {
            "claim": "The released detector has a specific evaluation preprocessing path.",
            "evidence": "Pinned code uses DLIB detection, 1.3 face scale, landmark similarity alignment/warp and released test log has apply_simple_augment=False with 224 input and released mean/std.",
            "untested_boundary": "A largest-face/no-affine bbox pipeline is a protocol divergence and cannot be called paper reproduction.",
        },
        {
            "claim": "Published detector scores directly define a fake-positive deployment score.",
            "evidence": "Pinned engine takes softmax(logits)[:,1]; ImageFolder lexical order makes fake=0 and real=1, so published code's positive score is p(real).",
            "untested_boundary": "D1 must explicitly define s_fake=softmax(logits)[:,0]=1-p(real); no target labels may set its threshold.",
        },
        {
            "claim": "Local CDFv2/DFDC results would be numerically comparable to the paper by dataset name alone.",
            "evidence": "Official preprocessing selects Celeb-real plus Celeb-synthesis for CDFv2 and a bounded DFDC test layout; cohort membership is part of the protocol.",
            "untested_boundary": "Full CDFv2 including YouTube-real and a local 50-part DFDC cohort require separate manifest hashes and non-reproduction labels.",
        },
    ]


def d1_spec() -> dict:
    return {
        "status": "proposed_not_executed",
        "checkpoint": {
            "name": "checkpoint-min_val_loss.pth",
            "publisher_sha256": DETECTOR_SHA256,
            "require_local_sha256_match_before_load": True,
            "pretrained_online_or_target_checkpoints_are_not_detector_substitutes": True,
            "safe_load_policy": "Treat as untrusted pickle; inspect/load only in the later approved evaluation environment.",
        },
        "author_parity_preprocessing": {
            "frame_sampling": "32 uniform indices over each video, with exact duplicate-index and decode-failure accounting",
            "detector": "DLIB frontal detector, upsample=1; pin package/model versions and preserve source face-selection behavior",
            "face_geometry": "scale=1.3; 81-landmark five-point SimilarityTransform; cv2.warpAffine to 112x112; preserve re-detection gate",
            "tensor_transform": "PIL RGB; bicubic direct Resize(224,224); ToTensor; apply_simple_augment=False",
            "normalization": {
                "mean": [0.5482207536697388, 0.42340534925460815, 0.3654651641845703],
                "std": [0.2789176106452942, 0.2438540756702423, 0.23493893444538116],
            },
        },
        "score_contract": {
            "class_index": {"fake": 0, "real": 1},
            "frame_fake_score": "softmax(logits, dim=1)[:,0]",
            "video_fake_score": "arithmetic mean of valid frame_fake_score values",
            "threshold": "freeze from FF++ C23 validation real videos only at empirical 99th percentile; never tune on target",
            "paper_metric_reproduction": "report frame/video ROC-AUC separately and label the author score orientation",
        },
        "cohort_contract": {
            "paper_reference": "CDFv2 Celeb-real + Celeb-synthesis (YouTube-real excluded); author DFDC test layout; DFDCP; WDF",
            "local_evaluation": "use only S0-data-approved manifests, record path/label/video identities and SHA-256, and label any local 50-part DFDC or full CDFv2 result non-comparable",
            "weighting": "video-equal primary; frame-level secondary; no target-label cohort pruning",
        },
        "required_outputs": [
            "checkpoint local SHA-256 receipt",
            "environment and preprocessing-version lock",
            "source validation threshold receipt",
            "target manifest SHA-256 and coverage/failure ledger",
            "per-cohort frame/video AUC plus fixed-source-threshold TPR/FPR",
        ],
        "gate": "DEFER until S0-data verifies eligible local cohorts and the detector checkpoint is acquired and locally hash-verified through an approved bounded route",
    }


def write_outputs(output: Path, receipt: dict, metadata_text: dict[str, str]) -> None:
    rows = claim_rows()
    with (output / "claim_evidence_boundary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("claim", "evidence", "untested_boundary"))
        writer.writeheader()
        writer.writerows(rows)
    checkpoint = {
        "publisher": "Wolowolo/fsfm-3c",
        "path": "finetuned_models/FF++_c23_32frames/checkpoint-min_val_loss.pth",
        "availability": "public Git-LFS/Xet object metadata verified; body not downloaded",
        "declared_sha256": DETECTOR_SHA256,
        "xet_hash_from_publisher_file_page": DETECTOR_XET_HASH,
        "hash_authority": "SHA-256 is the oid in the official Git-LFS pointer, not a locally recomputed file digest",
        "pointer_excerpt_verified": f"oid sha256:{DETECTOR_SHA256}" in metadata_text["checkpoint_pointer"],
        "checkpoint_body_downloaded": False,
        "checkpoint_unpickled": False,
    }
    (output / "checkpoint_provenance.json").write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
    (output / "d1_direct_eval_spec.proposed.json").write_text(json.dumps(d1_spec(), indent=2) + "\n", encoding="utf-8")
    (output / "s0_data_handoff.md").write_text(
        "# S0-data handoff\n\n"
        "Run as a separate bounded task; do not start D1. Inventory only eligible local CDFv2 and DFDC cohorts, "
        "emit immutable manifests plus SHA-256, keep Celeb-real/YouTube-real separate, identify the exact local DFDC parts, "
        "and report coverage/label provenance without reading model scores. Return GO only when cohort identity and labels are auditable.\n",
        encoding="utf-8",
    )
    (output / "action_requests.json").write_text(
        json.dumps({"actions": [{"type": "next_stage", "stage": "S0-data", "condition": "install and run this allowlisted boundary route first"}]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "worker_notes.md").write_text(
        "# Worker notes\n\nCPU-only official-material audit. No datasets, target results, checkpoint body, model execution, GPU, training, or ResearchFlow writes.\n",
        encoding="utf-8",
    )
    (output / "artifact_summary.md").write_text(
        "# Artifact summary\n\nThe stage verified the pinned source commit and bounded official publisher metadata, then emitted the claim boundary, checkpoint provenance, proposed D1 contract, and separate S0-data handoff.\n",
        encoding="utf-8",
    )
    (output / "fsfm_s0_boundary_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo_root", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    receipt = {
        "schema_version": 1,
        "stage": "S0-boundary",
        "source": None,
        "official_metadata": None,
        "constraints": {
            "gpu_used": False,
            "checkpoint_body_downloaded": False,
            "checkpoint_unpickled": False,
            "target_results_read": False,
            "researchflow_write_enabled": False,
        },
        "error": None,
    }
    texts: dict[str, str] = {}
    try:
        receipt["source"] = verify_checkout(Path(args.repo_root))
        receipt["official_metadata"], texts = verify_official_metadata()
        write_outputs(output, receipt, texts)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        receipt["error"] = str(exc)[:1200]
        (output / "fsfm_s0_boundary_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    passed = receipt["source"] is not None and receipt["official_metadata"] is not None and receipt["error"] is None
    metrics = {
        "execution_status": "completed",
        "contract_status": "pass" if passed else "defer",
        "science_status": "defer_pending_s0_data" if passed else "not_reached",
        "source_commit_verified": receipt["source"] is not None,
        "official_metadata_verified": receipt["official_metadata"] is not None,
        "checkpoint_body_downloaded": False,
        "checkpoint_unpickled": False,
        "gpu_used": False,
        "target_results_read": False,
        "researchflow_write_enabled": False,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    (output / "fsfm_s0_boundary_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": metrics, "error": receipt["error"]}, ensure_ascii=False))
    raise SystemExit(0 if passed else 2)


if __name__ == "__main__":
    main()

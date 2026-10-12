#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path


CASES = (
    {
        "case_id": "success_go",
        "execution_status": "succeeded",
        "scientific_status": "go",
        "metric_value": 0.91,
        "threshold": 0.8,
        "artifact_status": "verified",
        "exit_code": 0,
    },
    {
        "case_id": "execution_failed",
        "execution_status": "failed",
        "scientific_status": "defer",
        "metric_value": None,
        "threshold": 0.8,
        "artifact_status": "missing_due_to_exit_137",
        "exit_code": 137,
    },
    {
        "case_id": "success_stop",
        "execution_status": "succeeded",
        "scientific_status": "stop",
        "metric_value": 0.51,
        "threshold": 0.8,
        "artifact_status": "verified",
        "exit_code": 0,
    },
)


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        case_dir = root / str(case["case_id"])
        case_dir.mkdir(parents=True, exist_ok=True)
        raw_path = case_dir / "raw.log"
        with raw_path.open("w", encoding="utf-8", newline="\n") as handle:
            for index in range(3500):
                handle.write(
                    f"progress step={index:04d} worker=fixture status=running "
                    f"loss={(3500-index)/3500:.6f} repeated_diagnostic=healthy\n"
                )
            handle.write("FINAL_EVIDENCE=" + json.dumps(case, sort_keys=True) + "\n")
        raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        compact = {
            "schema_version": "harness-token-ab/v1",
            **case,
            "raw_log": {"path": "raw.log", "sha256": raw_hash, "bytes": raw_path.stat().st_size},
            "claim_boundary": "execution failure is defer, never a negative scientific finding",
        }
        compact_path = case_dir / "compact_evidence.json"
        compact_path.write_text(json.dumps(compact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        results.append({"case_id": case["case_id"], "raw_sha256": raw_hash, "raw_bytes": raw_path.stat().st_size})
    manifest = {"schema_version": "harness-token-ab/v1", "cases": results}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    root = Path(__file__).resolve().parents[2] / "benchmarks" / "harness_token_ab" / "generated"
    print(json.dumps(build(root), sort_keys=True))


if __name__ == "__main__":
    main()

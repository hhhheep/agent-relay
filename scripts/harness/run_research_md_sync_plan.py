#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from solution_agent.harness.md_sync import MarkdownSnapshot, SyncBase, build_sync_artifacts, load_mapping


def _load_json_model(path: Path, model):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _new_output_dir(path: Path) -> Path:
    if path.exists():
        raise FileExistsError(f"output_dir already exists; create-only policy: {path}")
    path.mkdir(parents=True)
    return path


def run(
    mapping_path: Path,
    entity_id: str,
    local_snapshot_path: Path,
    remote_snapshot_path: Path,
    output_dir: Path,
    max_reader_tokens: int,
    base_path: Path | None = None,
) -> dict:
    mapping, mapping_sha256 = load_mapping(mapping_path)
    local = _load_json_model(local_snapshot_path, MarkdownSnapshot)
    remote = _load_json_model(remote_snapshot_path, MarkdownSnapshot)
    base = _load_json_model(base_path, SyncBase) if base_path else None
    plan, reader, decision, base_candidate, metrics = build_sync_artifacts(
        mapping,
        mapping_sha256,
        entity_id,
        local,
        remote,
        base=base,
        max_reader_tokens=max_reader_tokens,
    )
    output = _new_output_dir(output_dir)
    artifacts = {
        "sync_plan.json": plan.model_dump(mode="json"),
        "reader_input_packet.json": reader,
        "codex_decision_packet.json": decision,
        "base_candidate.json": base_candidate.model_dump(mode="json"),
        "md_sync_metrics.json": metrics,
    }
    for name, payload in artifacts.items():
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_dir": str(output.resolve()),
        "decision_fingerprint": decision["decision_fingerprint"],
        "metrics": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping_path", type=Path, required=True)
    parser.add_argument("--entity_id", required=True)
    parser.add_argument("--local_snapshot_path", type=Path, required=True)
    parser.add_argument("--remote_snapshot_path", type=Path, required=True)
    parser.add_argument("--base_path", type=Path)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--max_reader_tokens", type=int, default=4096)
    args = parser.parse_args()
    print(json.dumps(run(**vars(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()

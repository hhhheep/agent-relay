#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from solution_agent.harness.md_sync import load_mapping, parse_markdown_snapshot, resolve_under_root


def _new_output_dir(path: Path) -> Path:
    if path.exists():
        raise FileExistsError(f"output_dir already exists; create-only policy: {path}")
    path.mkdir(parents=True)
    return path


def run(
    mapping_path: Path,
    entity_id: str,
    endpoint: str,
    workspace_root: Path,
    output_dir: Path,
    max_input_bytes: int,
) -> dict:
    mapping, mapping_sha256 = load_mapping(mapping_path)
    entity = mapping.entity(entity_id)
    if endpoint not in {"local", "remote"}:
        raise ValueError("endpoint must be local or remote")
    endpoint_config = entity.local if endpoint == "local" else entity.remote
    source = resolve_under_root(workspace_root, endpoint_config.path)
    if not source.is_file():
        raise FileNotFoundError(source)
    output = _new_output_dir(output_dir)
    snapshot = parse_markdown_snapshot(
        source,
        entity_id=entity_id,
        endpoint=endpoint,
        source_path=endpoint_config.path,
        max_input_bytes=max_input_bytes,
    )
    snapshot_path = output / "md_snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    metrics = {
        "execution_status": "succeeded",
        "endpoint": endpoint,
        "source_bytes": snapshot.source_bytes,
        "block_count": len(snapshot.blocks),
        "link_count": len(snapshot.links),
        "mapping_sha256": mapping_sha256,
        "source_sha256": snapshot.source_sha256,
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "write_enabled": False,
    }
    (output / "md_snapshot_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"snapshot_path": str(snapshot_path.resolve()), "metrics": metrics}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping_path", type=Path, required=True)
    parser.add_argument("--entity_id", required=True)
    parser.add_argument("--endpoint", choices=("local", "remote"), required=True)
    parser.add_argument("--workspace_root", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--max_input_bytes", type=int, default=524_288)
    args = parser.parse_args()
    print(json.dumps(run(**vars(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()

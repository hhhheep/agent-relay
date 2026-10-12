#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.release_identity import write_release_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one deterministic Harness release manifest.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-id")
    args = parser.parse_args()
    result = write_release_manifest(args.root.resolve(), args.output.resolve(), profile_id=args.profile_id)
    print(json.dumps({key: result[key] for key in ("release_id", "source_root_sha256", "mcp_schema_sha256")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

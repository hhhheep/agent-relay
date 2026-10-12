#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.profile_deploy import snapshot_profile_conflicts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrieve a hash-verified snapshot of only the files that conflict with one declared profile deployment."
    )
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = snapshot_profile_conflicts(
        profiles_path=args.profiles,
        profile_name=args.profile,
        bundle=args.bundle,
        expected_sha256=args.expected_sha256,
        output=args.output,
        deployment_id=args.deployment_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

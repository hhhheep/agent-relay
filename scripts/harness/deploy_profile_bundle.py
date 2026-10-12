#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.profile_deploy import deploy_profile_bundle
from solution_agent.execution_port.io_utils import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy one verified overlay through a declared remote profile lifecycle.")
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--deployment-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, help="Save full evidence and print only a bounded summary")
    args = parser.parse_args()
    result = deploy_profile_bundle(
        profiles_path=args.profiles,
        profile_name=args.profile,
        bundle=args.bundle,
        installer=args.installer,
        expected_sha256=args.expected_sha256,
        deployment_id=args.deployment_id,
        apply=args.apply,
    )
    if args.receipt:
        atomic_write_json(args.receipt, result)
        remote = result.get("remote", {})
        print(json.dumps({"ok": result["ok"], "receipt": str(args.receipt),
                          "deployment_id": result["deployment_id"], "error": remote.get("error"),
                          "conflicts": len((remote.get("dry_run") or {}).get("conflicts", [])),
                          "plan_sha256": (remote.get("dry_run") or {}).get("plan_sha256")}, indent=2))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.remote_profile import load_remote_deploy_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    profile = load_remote_deploy_profile(args.profiles, args.profile)
    print(json.dumps({"valid": True, "profile": profile.public_dict()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

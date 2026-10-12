#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import fields
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.release_identity import RuntimeIdentity, build_runtime_identity, compare_identities


def _identity(path: Path) -> RuntimeIdentity:
    value = json.loads(path.read_text(encoding="utf-8"))
    if "python_module_hashes" not in value and "module_hashes" in value:
        value["python_module_hashes"] = value.pop("module_hashes")
    value["capabilities"] = tuple(value.get("capabilities") or ())
    names = {item.name for item in fields(RuntimeIdentity)}
    missing = sorted(names - value.keys())
    if missing:
        raise ValueError(f"observed identity is incomplete ({path}): missing {', '.join(missing)}")
    return RuntimeIdentity(**{key: item for key, item in value.items() if key in names})


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a source root against one observed Harness runtime identity.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--observed", type=Path, required=True, help="JSON RuntimeIdentity captured from the runtime")
    args = parser.parse_args()
    expected = build_runtime_identity(args.root.resolve())
    observed = _identity(args.observed.resolve())
    mismatches = compare_identities(expected, observed)
    result = {"ok": not mismatches, "expected": expected.compact(), "observed": observed.compact(), "mismatches": mismatches}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())

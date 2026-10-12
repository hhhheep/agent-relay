from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import run_materialized_execution_task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-path", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--materializer-root", required=True, type=Path)
    parser.add_argument("--policy", default=None, type=Path)
    parser.add_argument("--env-file", default=None, type=Path)
    args = parser.parse_args()

    result = run_materialized_execution_task(
        task_path=args.task_path,
        out_dir=args.out_dir,
        workspace_root=args.workspace_root,
        materializer_root=args.materializer_root,
        policy_path=args.policy,
        env_file=args.env_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    status = str(result.get("status") or "")
    sys.exit(0 if status == "success" else 1)


if __name__ == "__main__":
    main()

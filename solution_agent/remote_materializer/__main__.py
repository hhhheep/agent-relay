from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import run_materializer
from .model_clients import StaticMaterializerClient, StaticReaderClient, load_env_file, load_policy_clients


PACKAGE_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize task artifacts without running training.")
    parser.add_argument("--task", required=True, type=Path, help="Path to task.md")
    parser.add_argument("--output-root", default=Path("runs"), type=Path, help="Directory that will contain runs/<task_id>/")
    parser.add_argument("--reader-output", type=Path, help="Raw reader work_order JSON output for dry-run mode")
    parser.add_argument(
        "--materializer-output",
        action="append",
        default=[],
        type=Path,
        help="Raw materializer output. Pass multiple times to simulate retries.",
    )
    parser.add_argument("--live", action="store_true", help="Call provider APIs using provider policy and env file")
    parser.add_argument(
        "--provider-policy",
        default=PACKAGE_DIR / "config" / "provider_policy.bailian.yaml",
        type=Path,
        help="Provider policy YAML for live mode",
    )
    parser.add_argument("--env-file", type=Path, help="Optional .env file containing API keys")
    parser.add_argument("--print-json", action="store_true", help="Print result_package as JSON")
    args = parser.parse_args(argv)

    if args.live:
        if args.env_file:
            load_env_file(args.env_file)
        clients = load_policy_clients(args.provider_policy)
        reader_client = clients.reader
        materializer_client = clients.materializer
        backup_materializer_client = clients.backup_materializer
        reader_verifier_client = clients.reader_verifier
    else:
        if not args.reader_output:
            parser.error("--reader-output is required unless --live is set")
        reader_raw = args.reader_output.read_text(encoding="utf-8")
        materializer_outputs = [path.read_text(encoding="utf-8") for path in args.materializer_output]
        if not materializer_outputs:
            materializer_outputs = [""]
        reader_client = StaticReaderClient(reader_raw)
        materializer_client = StaticMaterializerClient(materializer_outputs)
        backup_materializer_client = None
        reader_verifier_client = None
    package = run_materializer(
        args.task,
        args.output_root,
        reader_client,
        materializer_client,
        backup_materializer_client=backup_materializer_client,
        reader_verifier_client=reader_verifier_client,
    )
    if args.print_json:
        print(json.dumps(package, ensure_ascii=False, indent=2))
    return 0 if package.get("status") in {"materialized", "ambiguous"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

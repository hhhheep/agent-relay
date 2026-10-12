#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
from solution_agent.harness.release_identity import build_runtime_identity, release_files


TOP_LEVEL_FILES = {
    ".gitignore",
    ".solutionignore",
    "README.md",
    "cli.py",
    "pytest.ini",
    "requirements.txt",
    "requirements-harness.txt",
    "requirements-optional.txt",
    "requirements-extraction-windows.txt",
    "requirements-worker-linux.txt",
}
TREE_ROOTS = {
    "solution_agent",
    "scripts/harness",
    "plugins/solution-agent-harness",
}
EXPLICIT_FILES = {
    "config/harness_remote_deploy_profiles.example.json",
    "config/research_md_sync.example.yaml",
    "scripts/release/install_dependencies.py",
    "docs/engineering/phase-one/INSTALL.md",
    "docs/HARNESS_V2_DEPLOYMENT.md",
    "docs/HARNESS_V2_BOOTSTRAP_INSTALL.md",
    "docs/HARNESS_V2_ACCEPTANCE.md",
    "docs/HARNESS_V2_COMPLETION_AUDIT.md",
    "docs/HARNESS_V2_MANAGEMENT_COMPATIBILITY.md",
    "docs/HARNESS_V2_MD_SYNC_AND_WRITING_ROUTER_DESIGN.md",
    "docs/HARNESS_V2_MD_SYNC_PHASE0_ACCEPTANCE.md",
    "docs/HARNESS_V2_REMOTE_DEPLOYMENT_REQUIREMENTS.md",
    "docs/HARNESS_V2_TWO_ENDED_TOKEN_CONTRACT.md",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".token-guard",
    "executions",
    "outputs",
    "results",
    "reports",
    "state",
    "tasks",
    "workspaces",
    "worktrees",
}
FORBIDDEN_NAMES = {".env", "credentials.json", "secrets.json"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve())
    pure = PurePosixPath(relative.as_posix())
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe release path: {path}")
    return pure.as_posix()


def _eligible(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return False
    if path.name in FORBIDDEN_NAMES or ".env" in path.name.lower() or path.suffix.lower() in {".pyc", ".pyo"}:
        return False
    return True


def collect_release_files(root: Path) -> list[str]:
    root = root.resolve()
    selected: set[str] = set()
    for name in TOP_LEVEL_FILES | EXPLICIT_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"required release file missing: {name}")
        selected.add(name)
    for tree in TREE_ROOTS:
        base = root / tree
        if not base.is_dir():
            raise FileNotFoundError(f"required release tree missing: {tree}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = _safe_relative(path, root)
            if _eligible(relative):
                selected.add(relative)
    for path in release_files(root):
        relative = _safe_relative(path, root)
        if not _eligible(relative):
            raise ValueError(f"runtime identity includes forbidden release path: {relative}")
        selected.add(relative)
    return sorted(selected)


def _scan_sensitive(relative: str, payload: bytes) -> None:
    lower = relative.lower()
    if any(marker in lower for marker in ("/.env", "credential", "secret")):
        raise ValueError(f"sensitive-looking path cannot enter release: {relative}")
    if len(payload) > 2_000_000 or b"\x00" in payload:
        return
    text = payload.decode("utf-8", errors="ignore")
    suspicious = (
        re.compile(r"(?m)^\s*(?:DASHSCOPE_API_KEY|OPENAI_API_KEY)\s*=\s*['\"]?sk-[A-Za-z0-9]"),
        re.compile(r"(?m)^-----BEGIN (?:OPENSSH )?PRIVATE KEY-----$"),
    )
    if any(pattern.search(text) for pattern in suspicious):
        raise ValueError(f"credential material detected in release file: {relative}")


def build_release(root: Path, output: Path, *, migration_review: dict | None = None) -> dict:
    root = root.resolve()
    files = collect_release_files(root)
    entries = []
    payloads: dict[str, bytes] = {}
    for relative in files:
        payload = (root / relative).read_bytes()
        _scan_sensitive(relative, payload)
        payloads[relative] = payload
        entries.append({"path": relative, "bytes": len(payload), "sha256": _sha256_bytes(payload)})
    if migration_review is not None:
        reviewed = migration_review.get("files", {})
        if not isinstance(reviewed, dict) or set(reviewed) - set(payloads):
            raise ValueError("migration review contains unpublished files")
        for entry in entries:
            review = reviewed.get(entry["path"])
            if review is None:
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", str(review.get("remote_sha256", ""))) or not review.get("reason"):
                raise ValueError(f"migration review requires exact remote hash and reason: {entry['path']}")
            if review.get("source_sha256") != entry["sha256"]:
                raise ValueError(f"migration review source changed: {entry['path']}")
            entry["base_sha256"] = review["remote_sha256"]
    identity = build_runtime_identity(root)
    manifest = {
        "schema_version": "solution-agent-harness/0.6",
        "protocol_version": "0.6",
        "release_id": identity.release_id,
        "source_root_sha256": identity.source_root_sha256,
        "mcp_schema_sha256": identity.mcp_schema_sha256,
        "payload_sha256": _sha256_bytes(json.dumps(entries, sort_keys=True).encode()),
        "migration_review": migration_review,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "initial_install_only": False,
            "contains_credentials": False,
            "contains_runtime_logs": False,
            "contains_research_data": False,
            "force_overwrite": False,
        },
        "files": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for relative in sorted(payloads):
            archive.writestr("release/" + relative, payloads[relative])
    result = {
        "archive": str(output.resolve()),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "files": len(files),
        "bytes": output.stat().st_size,
        "manifest": manifest,
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    installer_source = root / "scripts" / "harness" / "install_overlay.py"
    installer_output = output.parent / "install_bootstrap_release.py"
    shutil.copy2(installer_source, installer_output)
    handoff = output.parent / "REMOTE_INSTALL_HANDOFF.md"
    handoff.write_text(
        "# Example 91 Solution Agent Harness bootstrap handoff\n\n"
        f"- Release: `{output.name}`\n"
        f"- Release SHA-256: `{result['sha256']}`\n"
        f"- Release files: `{result['files']}`\n"
        f"- Release bytes: `{result['bytes']}`\n"
        f"- Manifest: `{output.name}.manifest.json`\n"
        f"- Installer: `{installer_output.name}`\n"
        "- Target: `/home/agent-user/.local/opt/solution-agent`\n"
        "- Project: `/srv/example-workspaces/fan-workspace`\n"
        "- Run root: `/srv/example-workspaces/harness-runs`\n\n"
        "Transfer the release, manifest, and installer as declared artifacts into a new deployment "
        "directory below the run root. Verify the remote release SHA-256 equals the value above. "
        "Run the installer without `--apply`, stop on any conflict or unsafe path, then "
        "repeat with `--apply --plan-sha256 <dry-run plan hash>` only after a zero-conflict dry-run. Follow "
        "`docs/HARNESS_V2_BOOTSTRAP_INSTALL.md` inside the installed release for runtime, plugin, "
        "and fresh-task smoke gates. Never read or copy the provider key.\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--migration-review", type=Path)
    args = parser.parse_args()
    review = json.loads(args.migration_review.read_text(encoding="utf-8")) if args.migration_review else None
    result = build_release(args.root, args.output, migration_review=review)
    print(json.dumps({key: result[key] for key in ("archive", "sha256", "files", "bytes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

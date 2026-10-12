from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal


RELEASE_FILE_LIST = "deploy/overlay-files.txt"
_SELF_REFERENTIAL_RELEASE_FILES = {"state/integrated_focus_runtime/release_manifest.json"}

CANONICAL_TOOL_NAMES = (
    "stage_execute",
    "stage_status",
    "stage_collect",
    "terminal_open",
    "terminal_exec",
    "terminal_send",
    "terminal_read",
    "terminal_close",
    "artifact_inspect",
    "artifact_store",
    "artifact_apply",
)

def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def git_source_state(root: Path) -> tuple[str | None, bool]:
    """Return the containing Git commit and whether this release tree is uncommitted.

    An unversioned tree is deliberately treated as dirty: it cannot make a clean-source claim.
    """

    try:
        commit = subprocess.run(
            ["git", "-C", str(root.resolve()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if commit.returncode != 0:
            return None, True
        status = subprocess.run(
            ["git", "-C", str(root.resolve()), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
        )
        return commit.stdout.strip() or None, status.returncode != 0 or bool(status.stdout.strip())
    except OSError:
        return None, True


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _release_relative(value: str) -> str:
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe release allowlist path: {value}")
    return relative.as_posix()


def release_files(root: Path) -> list[Path]:
    root = root.resolve()
    file_list = root / RELEASE_FILE_LIST
    if not file_list.is_file() or file_list.is_symlink():
        raise ValueError(f"release allowlist is missing or symlinked: {file_list}")
    declared = [
        _release_relative(line.strip())
        for line in file_list.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(declared) != len(set(declared)):
        raise ValueError("release allowlist contains duplicate paths")
    selected: dict[str, Path] = {RELEASE_FILE_LIST: file_list}
    for relative in declared:
        if relative in _SELF_REFERENTIAL_RELEASE_FILES:
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file():
            raise ValueError(f"release allowlist file is missing: {relative}")
        if path.is_symlink():
            raise ValueError(f"release source cannot contain symlinked files: {relative}")
        selected[relative] = path
    return [selected[key] for key in sorted(selected)]


def source_tree_sha256(root: Path) -> tuple[str, dict[str, str]]:
    root = root.resolve()
    hashes: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in release_files(root):
        relative = _relative(path, root)
        file_hash = sha256_file(path)
        hashes[relative] = file_hash
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), hashes


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def plugin_version(root: Path) -> str:
    manifest = _json_object(root / "plugins" / "solution-agent-harness" / ".codex-plugin" / "plugin.json")
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise ValueError("plugin manifest has no version")
    return version


def mcp_tool_contract(root: Path) -> dict[str, Any]:
    manifest = _json_object(root / "plugins" / "solution-agent-harness" / ".mcp.json")
    servers = manifest.get("mcpServers")
    if not isinstance(servers, dict) or len(servers) != 1:
        raise ValueError("plugin must declare exactly one MCP server")
    name, server = next(iter(servers.items()))
    if not isinstance(server, dict) or not isinstance(server.get("tools"), dict):
        raise ValueError("plugin MCP server has no tools object")
    tools = server["tools"]
    if tuple(tools) != CANONICAL_TOOL_NAMES:
        raise ValueError(f"MCP tools do not match canonical ordered set: {tuple(tools)}")
    contract = {
        "server": str(name),
        "tools": tools,
        "tool_timeout_sec": server.get("tool_timeout_sec"),
    }
    if root.resolve() == Path(__file__).resolve().parents[2]:
        try:
            from solution_agent.harness.mcp_server import mcp
        except ModuleNotFoundError:
            # Source-only builders may run before optional MCP dependencies are installed.
            # The declared plugin contract remains authoritative in that case.
            return contract

        runtime_tools: dict[str, Any] = {}
        registered = mcp._tool_manager._tools
        if tuple(registered) != CANONICAL_TOOL_NAMES:
            raise ValueError(f"runtime MCP tools do not match canonical ordered set: {tuple(registered)}")
        for tool_name in CANONICAL_TOOL_NAMES:
            tool = registered[tool_name]
            runtime_tools[tool_name] = {
                # MCP dependency versions differ in docstring indentation only.
                # Normalize presentation whitespace, not schema fields or content.
                "description": inspect.cleandoc(tool.description or ""),
                "input_schema": tool.parameters,
                "output_schema": tool.fn_metadata.output_schema,
                "annotations": tool.annotations.model_dump(mode="json") if tool.annotations is not None else None,
            }
        contract["runtime_tools"] = runtime_tools
    return contract


def detect_capabilities(root: Path) -> tuple[str, ...]:
    probes = {
        "context_gateway": root / "solution_agent" / "context_gateway.py",
        "job_supervisor": root / "solution_agent" / "harness" / "terminal_commands.py",
        "profile_terminal": root / "solution_agent" / "harness" / "remote_terminal.py",
        "terminal_hub": root / "solution_agent" / "harness" / "remote_terminal.py",
        "wait_sidecar": root / "solution_agent" / "harness" / "wait_monitor.py",
        "wait_state_machine": root / "solution_agent" / "harness" / "wait_runtime.py",
    }
    return tuple(sorted(name for name, path in probes.items() if path.is_file()))


@dataclass(frozen=True)
class RuntimeIdentity:
    schema_version: int
    release_id: str
    source_root_sha256: str
    plugin_version: str
    plugin_manifest_sha256: str
    mcp_schema_sha256: str
    python_module_hashes: dict[str, str]
    profile_id: str | None
    capabilities: tuple[str, ...]
    host_resume_mode: Literal["supported", "manual", "unavailable"]

    def compact(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "source_root_sha256": self.source_root_sha256,
            "plugin_version": self.plugin_version,
            "mcp_schema_sha256": self.mcp_schema_sha256,
            "profile_id": self.profile_id,
            "capabilities": list(self.capabilities),
            "host_resume_mode": self.host_resume_mode,
        }


def build_runtime_identity(
    root: Path,
    *,
    profile_id: str | None = None,
    host_resume_mode: Literal["supported", "manual", "unavailable"] = "manual",
) -> RuntimeIdentity:
    root = root.resolve()
    tree_hash, file_hashes = source_tree_sha256(root)
    plugin_manifest = root / "plugins" / "solution-agent-harness" / ".codex-plugin" / "plugin.json"
    tool_contract = mcp_tool_contract(root)
    release_id = f"harness-0.6-{tree_hash[:16]}"
    return RuntimeIdentity(
        schema_version=1,
        release_id=release_id,
        source_root_sha256=tree_hash,
        plugin_version=plugin_version(root),
        plugin_manifest_sha256=sha256_file(plugin_manifest),
        mcp_schema_sha256=canonical_json_sha256(tool_contract),
        python_module_hashes={path: value for path, value in file_hashes.items() if path.endswith(".py")},
        profile_id=profile_id,
        capabilities=detect_capabilities(root),
        host_resume_mode=host_resume_mode,
    )


def write_release_manifest(root: Path, output: Path, *, profile_id: str | None = None) -> dict[str, Any]:
    identity = build_runtime_identity(root, profile_id=profile_id)
    payload = asdict(identity)
    payload["capabilities"] = list(identity.capabilities)
    payload["source_commit"], payload["source_dirty"] = git_source_state(root)
    payload["skill_sha256"] = sha256_file(
        root.resolve() / "plugins" / "solution-agent-harness" / "skills" / "solution-agent-harness" / "SKILL.md"
    )
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    return payload


def compare_identities(expected: RuntimeIdentity, observed: RuntimeIdentity) -> list[str]:
    mismatches: list[str] = []
    for field in (
        "release_id",
        "source_root_sha256",
        "plugin_version",
        "plugin_manifest_sha256",
        "mcp_schema_sha256",
        "host_resume_mode",
    ):
        if getattr(expected, field) != getattr(observed, field):
            mismatches.append(field)
    if expected.python_module_hashes != observed.python_module_hashes:
        mismatches.append("python_module_hashes")
    if expected.capabilities != observed.capabilities:
        mismatches.append("capabilities")
    return mismatches


def build_source_inventory(
    candidate_roots: Iterable[Path],
    *,
    selected_authoritative_root: Path | None = None,
    selection_reason: str = "caller-declared authoritative source root",
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for candidate in candidate_roots:
        root = candidate.resolve()
        entry: dict[str, Any] = {"path": str(root), "exists": root.is_dir()}
        if root.is_dir():
            try:
                identity = build_runtime_identity(root)
                commit, dirty = git_source_state(root)
                entry.update(
                    ok=True,
                    commit=commit,
                    tree_sha256=identity.source_root_sha256,
                    dirty=dirty,
                    release_id=identity.release_id,
                    plugin_version=identity.plugin_version,
                    mcp_schema_sha256=identity.mcp_schema_sha256,
                    capabilities=list(identity.capabilities),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        candidates.append(entry)
    selected = selected_authoritative_root.resolve() if selected_authoritative_root is not None else None
    if selected is None:
        selected = next((Path(item["path"]) for item in candidates if item.get("ok")), None)
    return {
        "schema_version": 1,
        "candidate_roots": candidates,
        "selected_authoritative_root": str(selected) if selected is not None else None,
        "selection_reason": selection_reason,
    }

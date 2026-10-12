from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


def _find_solution_root() -> Path:
    configured = os.environ.get("SOLUTION_AGENT_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "solution_agent" / "harness" / "mcp_server.py").is_file():
            return parent
    raise RuntimeError("Set SOLUTION_AGENT_ROOT to the solution-agent checkout")


root = _find_solution_root()
sys.path.insert(0, str(root))
os.environ["SOLUTION_AGENT_ROOT"] = str(root)
existing_pythonpath = os.environ.get("PYTHONPATH", "")
pythonpath_parts = [item for item in existing_pythonpath.split(os.pathsep) if item]
if str(root) not in pythonpath_parts:
    os.environ["PYTHONPATH"] = os.pathsep.join([str(root), *pythonpath_parts])


def _reexec_in_local_venv(solution_root: Path) -> None:
    """Use the checkout venv without encoding a host-specific path in the manifest."""

    if os.environ.get("SOLUTION_AGENT_VENV_REEXECED") == "1":
        return
    candidates = [
        solution_root / ".venv" / "Scripts" / "python.exe",
        solution_root / ".venv" / "bin" / "python",
    ]
    interpreter = next((candidate for candidate in candidates if candidate.is_file()), None)
    if interpreter is None or Path(sys.executable).resolve() == interpreter.resolve():
        return
    environment = dict(os.environ)
    environment["SOLUTION_AGENT_VENV_REEXECED"] = "1"
    completed = subprocess.run(
        [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]],
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


_reexec_in_local_venv(root)

from solution_agent.harness.mcp_server import main, mcp  # noqa: E402


def _tool_manifest() -> dict:
    import asyncio

    from mcp import Client

    from solution_agent.harness.manifests import build_tool_manifest

    async def collect() -> dict:
        async with Client(mcp) as client:
            result = await client.list_tools()
        return build_tool_manifest(result.tools).model_dump(mode="json")

    return asyncio.run(collect())


if __name__ == "__main__":
    if "--check" in sys.argv:
        import importlib.metadata
        import json
        from dataclasses import asdict

        from solution_agent.harness.release_identity import build_runtime_identity
        from solution_agent.harness.remote_terminal import load_terminal_profile, terminal_runtime_check

        try:
            profile_id = load_terminal_profile(root).profile_id
        except (OSError, RuntimeError, ValueError, yaml.YAMLError):
            profile_id = os.environ.get("SOLUTION_AGENT_BINDING_ID") or os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None
        runtime_identity = build_runtime_identity(root, profile_id=profile_id, host_resume_mode="manual")

        print(
            json.dumps(
                {
                    "ok": True,
                    "mcp_version": importlib.metadata.version("mcp"),
                    "root": str(root),
                    "runtime_identity": asdict(runtime_identity),
                    "terminal": terminal_runtime_check(root),
                    "tool_manifest": _tool_manifest(),
                }
            )
        )
    else:
        main()

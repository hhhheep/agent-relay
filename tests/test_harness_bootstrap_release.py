from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = _load("bootstrap_builder", "scripts/harness/build_bootstrap_release.py")
installer = _load("bootstrap_installer", "scripts/harness/install_bootstrap_release.py")


def test_bootstrap_release_build_dry_run_and_initial_install(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    built = builder.build_release(ROOT, archive)
    paths = [entry["path"] for entry in built["manifest"]["files"]]
    assert built["files"] > 100
    assert "solution_agent/harness/mcp_server.py" in paths
    assert "deploy/overlay-files.txt" in paths
    assert "install_engine.py" not in paths
    assert not any(path.startswith("reports/") for path in paths)
    from solution_agent.harness.release_identity import build_runtime_identity, source_tree_sha256
    assert built["manifest"]["release_id"] == build_runtime_identity(ROOT).release_id
    assert "plugins/solution-agent-harness/.mcp.json" in paths
    assert "plugins/solution-agent-harness/skills/solution-agent-harness/references/feedback.md" in paths
    assert "config/harness_remote_deploy_profiles.example.json" in paths
    assert "config/research_md_sync.example.yaml" in paths
    assert "solution_agent/harness/md_sync.py" in paths
    assert "scripts/harness/run_research_md_sync_plan.py" in paths
    assert "docs/HARNESS_V2_MD_SYNC_AND_WRITING_ROUTER_DESIGN.md" in paths
    assert "docs/HARNESS_V2_MD_SYNC_PHASE0_ACCEPTANCE.md" in paths
    assert "docs/HARNESS_V2_BOOTSTRAP_INSTALL.md" in paths
    assert "docs/HARNESS_V2_ACCEPTANCE.md" in paths
    assert "docs/HARNESS_V2_COMPLETION_AUDIT.md" in paths
    assert not any(".env" in path or "__pycache__" in path for path in paths)

    target = tmp_path / "installed" / "solution-agent"
    dry = installer.install_release(archive, built["sha256"], target)
    assert dry["safe"] is True
    assert dry["target_state"] == "absent"
    assert dry["applied"] is False
    applied = installer.install_release(archive, built["sha256"], target, apply=True)
    assert applied["applied"] is True
    assert (target / "solution_agent" / "harness" / "mcp_server.py").is_file()
    assert (target / "bootstrap-install-receipt.json").is_file()
    assert source_tree_sha256(target)[0] == built["manifest"]["source_root_sha256"]
    harness_skill = target / "plugins" / "solution-agent-harness" / "skills" / "solution-agent-harness" / "SKILL.md"
    assert "Only after an actionable Harness defect" in harness_skill.read_text(encoding="utf-8")
    assert not (target / "plugins" / "solution-agent-harness" / "skills" / "solution-agent-feedback" / "SKILL.md").exists()


def test_bootstrap_release_refuses_hash_mismatch_and_nonempty_target(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    built = builder.build_release(ROOT, archive)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.install_release(archive, "0" * 64, tmp_path / "new-root")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(FileExistsError, match="never overwrites"):
        installer.install_release(archive, built["sha256"], occupied)
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_delivered_fresh_installer_uses_the_documented_cli(tmp_path):
    import json
    import subprocess
    import sys
    archive = tmp_path / "release.zip"
    built = builder.build_release(ROOT, archive)
    delivered = tmp_path / "install_bootstrap_release.py"
    target = tmp_path / "fresh-target"
    completed = subprocess.run([sys.executable, str(delivered), "--archive", str(archive),
        "--expected-sha256", built["sha256"], "--target-root", str(target)],
        capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["safe"] and not result["applied"]
    assert not target.exists()
    managed_help = subprocess.run([sys.executable, str(tmp_path / "install_overlay.py"), "--help"],
        capture_output=True, text=True, check=True)
    assert "--plan-sha256" in managed_help.stdout

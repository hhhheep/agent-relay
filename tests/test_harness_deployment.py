from __future__ import annotations

import importlib.util
import json
import hashlib
import zipfile
import contextlib
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = _load("harness_bundle_builder", "scripts/harness/build_deployment_bundle.py")
installer = _load("harness_overlay_installer", "scripts/harness/install_overlay.py")


def test_bundle_dry_run_apply_receipt_and_conflict(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    overlay = tmp_path / "overlay"
    target = tmp_path / "target"
    for root in (baseline, overlay, target):
        (root / "pkg").mkdir(parents=True)
    (baseline / "pkg" / "existing.txt").write_text("old\n", encoding="utf-8")
    (target / "pkg" / "existing.txt").write_text("old\n", encoding="utf-8")
    (overlay / "pkg" / "existing.txt").write_text("new\n", encoding="utf-8")
    (overlay / "pkg" / "added.txt").write_text("added\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text("pkg/existing.txt\npkg/added.txt\n", encoding="utf-8")
    bundle = tmp_path / "bundle.zip"
    builder.build_bundle(overlay, baseline, file_list, bundle)

    dry_run = installer.install_bundle(bundle, target)
    assert dry_run["conflicts"] == []
    assert dry_run["applied"] == []
    applied = installer.install_bundle(bundle, target, apply=True)
    assert (target / "pkg" / "existing.txt").read_text(encoding="utf-8") == "new\n"
    assert (target / "pkg" / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert not (target / ".solution-agent-harness-backups").exists()
    ledger = Path(applied["ledger"])
    assert ledger.is_file()
    receipt = json.loads(ledger.read_text(encoding="utf-8"))
    assert receipt["bundle_sha256"] == applied["bundle_sha256"]
    assert receipt["applied"] == ["pkg/existing.txt", "pkg/added.txt"]
    assert receipt["verified"] == ["pkg/existing.txt", "pkg/added.txt"]

    replay = installer.install_bundle(bundle, target, apply=True)
    assert replay["applied"] == []
    assert replay["verified"] == ["pkg/existing.txt", "pkg/added.txt"]
    assert replay["ledger"] == applied["ledger"]

    (target / "pkg" / "existing.txt").write_text("user change\n", encoding="utf-8")
    conflict = installer.install_bundle(bundle, target)
    assert conflict["conflicts"][0]["path"] == "pkg/existing.txt"


def _bundle(path, payload=b"new", *, obsolete=None):
    manifest = {"schema_version": "solution-agent-harness/0.6", "files": [
        {"path": "pkg/file.txt", "sha256": hashlib.sha256(payload).hexdigest()}],
        "obsolete": obsolete or []}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr("release/pkg/file.txt", payload)
    return path


def test_managed_upgrade_uses_recorded_hash_not_current_contents(tmp_path):
    target = tmp_path / "target"
    first = _bundle(tmp_path / "first.zip", b"first")
    installer.install_bundle(first, target, apply=True)
    second = _bundle(tmp_path / "second.zip", b"second")
    assert installer.install_bundle(second, target)["conflicts"] == []
    (target / "pkg/file.txt").write_bytes(b"unreviewed change")
    assert len(installer.install_bundle(second, target)["conflicts"]) == 1


def test_obsolete_conflict_prevents_all_writes(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_bytes(b"changed")
    bundle = _bundle(tmp_path / "bundle.zip", obsolete=[{
        "path": "old.txt", "owner": "solution-agent-harness", "expected_sha256": "0" * 64}])
    result = installer.install_bundle(bundle, target, apply=True)
    assert len(result["conflicts"]) == 1
    assert not (target / "pkg/file.txt").exists()
    assert (target / "old.txt").read_bytes() == b"changed"


def test_plan_rechecked_inside_lock(tmp_path, monkeypatch):
    target = tmp_path / "target"
    bundle = _bundle(tmp_path / "bundle.zip")
    @contextlib.contextmanager
    def changed_root(root):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg/file.txt").write_bytes(b"intervening change")
        yield
    monkeypatch.setattr(installer, "_deployment_lock", changed_root)
    with pytest.raises(RuntimeError, match="changed before lock"):
        installer.install_bundle(bundle, target, apply=True)
    assert (target / "pkg/file.txt").read_bytes() == b"intervening change"


@pytest.mark.parametrize("name", ["../file", "C:/file", "pkg\\file", "pkg/../file", "./file"])
def test_cross_platform_unsafe_paths_rejected(name):
    with pytest.raises(ValueError, match="unsafe manifest path"):
        installer._relative(name)


def test_reviewed_plan_mismatch_never_writes(tmp_path):
    target = tmp_path / "target"
    bundle = _bundle(tmp_path / "bundle.zip")
    with pytest.raises(RuntimeError, match="reviewed plan"):
        installer.install_bundle(bundle, target, apply=True, expected_plan_sha256="0" * 64)
    assert not target.exists()

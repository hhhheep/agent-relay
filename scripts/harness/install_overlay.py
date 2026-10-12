#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import contextlib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in value or ":" in value or path.as_posix() != value:
        raise ValueError(f"unsafe manifest path: {value}")
    return path


def _plan_hash(manifest: dict[str, Any], plan: list[dict[str, Any]], target_root: Path) -> str:
    payload = {"bundle_manifest": manifest, "target_root": str(target_root), "plan": plan}
    return _sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _managed_inventory(target_root: Path) -> dict[str, set[str]]:
    """Read only prior Harness receipts; unknown files never become managed by inference."""
    inventory: dict[str, set[str]] = {}
    root = target_root / ".solution-agent-harness-deployments"
    if not root.is_dir():
        return inventory
    for ledger in root.glob("*/install-ledger.json"):
        try:
            value = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        verified = set(value.get("verified", []))
        for item in value.get("plan", []):
            name, digest = item.get("path"), item.get("overlay_sha256")
            if name in verified and isinstance(digest, str) and len(digest) == 64:
                inventory.setdefault(name, set()).add(digest)
    return inventory


def _target(root: Path, name: str) -> Path:
    relative = _relative(name)
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError(f"symlink or junction target: {name}")
    if root not in candidate.resolve().parents:
        raise ValueError(f"target escapes root: {name}")
    if candidate.exists() and not candidate.is_file():
        raise ValueError(f"target is not a regular file: {name}")
    return candidate


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _deployment_lock(target_root: Path):
    lock_dir = target_root / ".solution-agent-harness-deployments"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / "deployment.lock"
    handle = lock.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write("0")
            handle.flush()
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("deployment lock is held") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("deployment lock is held") from exc
        yield
    finally:
        if os.name == "nt":
            try: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception: pass
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def inspect_bundle(bundle: Path, target_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if target_root.is_symlink() or getattr(target_root, "is_junction", lambda: False)():
        raise ValueError("target root may not be a symlink or junction")
    target_root = target_root.resolve()
    prior_inventory = _managed_inventory(target_root)
    with zipfile.ZipFile(bundle) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("duplicate archive members")
        manifest_name = "manifest.json" if "manifest.json" in archive.namelist() else "release-manifest.json"
        manifest = json.loads(archive.read(manifest_name))
        release_layout = manifest_name == "release-manifest.json"
        if manifest.get("schema_version") not in {"1.0", "2.0"} or not isinstance(manifest.get("files"), list):
            if manifest.get("schema_version") != "solution-agent-harness/0.6":
                raise ValueError("unsupported bundle manifest")
        plan: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in manifest["files"]:
            relative = _relative(str(entry["path"]))
            if relative.as_posix() in seen:
                raise ValueError(f"duplicate manifest path: {relative}")
            seen.add(relative.as_posix())
            member = ("release/" if release_layout else "files/") + relative.as_posix()
            payload = archive.read(member)
            expected_payload_hash = entry.get("overlay_sha256", entry.get("sha256"))
            if _sha256_bytes(payload) != expected_payload_hash:
                raise ValueError(f"bundle hash mismatch: {relative}")
            target = _target(target_root, relative.as_posix())
            current_hash = _sha256(target) if target.is_file() else None
            base_hash = entry.get("base_sha256")
            if base_hash is None and current_hash in prior_inventory.get(relative.as_posix(), set()):
                base_hash = current_hash
            overlay_hash = entry.get("overlay_sha256", entry.get("sha256"))
            if current_hash == overlay_hash:
                action, safe = "already_current", True
            elif base_hash is None and current_hash is None:
                action, safe = "create", True
            elif base_hash is not None and current_hash == base_hash:
                action, safe = "update", True
            else:
                action, safe = "conflict", False
            plan.append(
                {
                    "path": relative.as_posix(),
                    "action": action,
                    "safe": safe,
                    "current_sha256": current_hash,
                    "base_sha256": base_hash,
                    "overlay_sha256": overlay_hash or expected_payload_hash,
                }
            )
        for obsolete in manifest.get("obsolete", []):
            name = _relative(str(obsolete.get("path", ""))).as_posix()
            if name in seen:
                raise ValueError(f"duplicate manifest path: {name}")
            seen.add(name)
            target = _target(target_root, name)
            current = _sha256(target) if target.is_file() else None
            expected = obsolete.get("expected_sha256")
            safe = obsolete.get("owner") == "solution-agent-harness" and bool(expected) and current in (None, expected)
            plan.append({"path": name, "action": "remove" if current else "already_absent",
                         "safe": safe, "current_sha256": current, "base_sha256": expected,
                         "overlay_sha256": None})
    return manifest, plan


def install_bundle(bundle: Path, target_root: Path, *, apply: bool = False, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    manifest, plan = inspect_bundle(bundle, target_root)
    target_root = target_root.resolve()
    conflicts = [item for item in plan if not item["safe"]]
    bundle_sha256 = _sha256(bundle)
    result: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "target_root": str(target_root),
        "bundle_sha256": bundle_sha256,
        "conflicts": conflicts,
        "plan": plan,
        "applied": [],
        "verified": [],
        "ledger": None,
        "plan_sha256": _plan_hash(manifest, plan, target_root),
        "journal": None,
    }
    if expected_plan_sha256 is not None and expected_plan_sha256 != result["plan_sha256"]:
        raise RuntimeError("deployment plan hash does not match the reviewed plan")
    if conflicts or not apply:
        return result

    with _deployment_lock(target_root), zipfile.ZipFile(bundle) as archive:
        locked_manifest, locked_plan = inspect_bundle(bundle, target_root)
        if _sha256(bundle) != bundle_sha256 or _plan_hash(locked_manifest, locked_plan, target_root) != result["plan_sha256"]:
            raise RuntimeError("deployment plan changed before lock acquisition; run dry-run again")
        release_layout = "release-manifest.json" in archive.namelist()
        journal_dir = target_root / ".solution-agent-harness-deployments" / bundle_sha256
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal = journal_dir / "journal.json"
        result["journal"] = str(journal)
        ledger = journal_dir / "install-ledger.json"
        result["ledger"] = str(ledger)
        _write_json(journal, {"state": "applying", "plan_sha256": result["plan_sha256"], "started_at": time.time()})
        for item in plan:
            target = _target(target_root, item["path"])
            observed = _sha256(target) if target.is_file() else None
            if observed != item["current_sha256"]:
                raise RuntimeError(f"target changed during apply: {item['path']}")
            if item["action"] in {"already_current", "already_absent", "remove"}:
                if item["action"] == "already_current":
                    result["verified"].append(item["path"])
                elif item["action"] == "remove":
                    target.unlink()
                    result.setdefault("removed", []).append(item["path"])
                _write_json(ledger, {**result, "state": "applying"})
                continue
            relative = _relative(item["path"])
            target = target_root.joinpath(*relative.parts)
            payload = archive.read(("release/" if release_layout else "files/") + relative.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
            if _sha256(target) != item["overlay_sha256"]:
                raise RuntimeError(f"published file hash mismatch: {relative}")
            result["applied"].append(relative.as_posix())
            result["verified"].append(relative.as_posix())
            _write_json(ledger, {**result, "state": "applying"})
        _write_json(ledger, {**result, "state": "complete", "manifest_policy": manifest.get("policy")})
        _write_json(journal, {"state": "complete", "plan_sha256": result["plan_sha256"], "ledger": str(ledger)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    result = install_bundle(args.bundle, args.target_root, apply=args.apply, expected_plan_sha256=args.plan_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["conflicts"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

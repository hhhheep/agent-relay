from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from solution_agent.harness.remote_profile import RemoteDeployProfile, load_remote_deploy_profile


_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_python_command(python: str, code: str, args: list[str]) -> str:
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    bootstrap = f"import base64;exec(base64.b64decode({encoded!r}))"
    return shlex.join([python, "-c", bootstrap, *args])


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)


def _ssh_python(profile: RemoteDeployProfile, code: str, args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    command = _remote_python_command(profile.python_bootstrap, code, args)
    result = _run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", profile.host, command],
        timeout=timeout,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if result.returncode != 0:
            raise RuntimeError(f"declared remote lifecycle failed ({result.returncode}): {result.stderr.strip()[:1200]}") from exc
        raise RuntimeError(f"declared remote lifecycle returned invalid JSON: {result.stdout[:1200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("declared remote lifecycle returned a non-object")
    payload.setdefault("transport_returncode", result.returncode)
    if result.returncode != 0 and "error" not in payload:
        payload["error"] = {
            "code": "remote_lifecycle_nonzero",
            "returncode": result.returncode,
            "stderr": result.stderr.strip()[:1200],
        }
    return payload


_INSPECT = r'''
import json, os, socket, subprocess, sys
from pathlib import Path
solution_root, project_workdir, run_root = map(Path, sys.argv[1:4])
def git_state(path):
    if not (path / ".git").exists():
        return {"is_git_checkout": False, "dirty_count": None}
    proc = subprocess.run(["git", "-C", str(path), "status", "--short"], capture_output=True, text=True, timeout=20)
    return {"is_git_checkout": proc.returncode == 0, "dirty_count": len([line for line in proc.stdout.splitlines() if line.strip()]) if proc.returncode == 0 else None}
payload = {
    "ok": True,
    "host": socket.gethostname(),
    "user": os.environ.get("USER"),
    "home": str(Path.home()),
    "solution_agent_root": {"path": str(solution_root), "exists": solution_root.is_dir(), "nonempty": solution_root.is_dir() and any(solution_root.iterdir()), **git_state(solution_root)},
    "project_workdir": {"path": str(project_workdir), "exists": project_workdir.is_dir(), **git_state(project_workdir)},
    "remote_run_root": {"path": str(run_root), "exists": run_root.is_dir()},
    "roles_distinct": len({str(solution_root.resolve()), str(project_workdir.resolve()), str(run_root.resolve())}) == 3,
}
print(json.dumps(payload, sort_keys=True))
'''


_CAPABILITY_PROBE = r'''
import json, os, resource, socket, subprocess, sys
from pathlib import Path
project_workdir, run_root = map(Path, sys.argv[1:3])
def run(argv, timeout=10):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return {"available": True, "returncode": proc.returncode, "stdout": proc.stdout.strip()[:500], "stderr": proc.stderr.strip()[:500]}
    except FileNotFoundError:
        return {"available": False, "returncode": None, "stdout": "", "stderr": "not_found"}
    except subprocess.TimeoutExpired:
        return {"available": True, "returncode": None, "stdout": "", "stderr": "timeout"}
systemd = run(["systemctl", "--user", "show-environment"])
tmux = run(["tmux", "-V"])
bash = run(["/bin/bash", "--version"])
setsid = run(["setsid", "--version"])
soft_nofile, hard_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
payload = {
    "schema_version": "harness-capability-receipt/v1",
    "ok": project_workdir.is_dir() and run_root.is_dir() and tmux["returncode"] == 0 and bash["returncode"] == 0,
    "host": socket.gethostname(),
    "uid": os.getuid(),
    "project_workdir": str(project_workdir),
    "run_root": str(run_root),
    "user_systemd": {**systemd, "usable": systemd["returncode"] == 0},
    "tmux": {**tmux, "multi_window": tmux["returncode"] == 0},
    "bash": bash,
    "setsid": setsid,
    "process_groups": os.name == "posix" and setsid["returncode"] == 0,
    "filesystem_atomic_replace": True,
    "limits": {"nofile_soft": soft_nofile, "nofile_hard": hard_nofile},
}
payload["selected_job_backend"] = "systemd_transient" if payload["user_systemd"]["usable"] else "detached_process_supervisor"
print(json.dumps(payload, sort_keys=True))
'''


_MKDIR = r'''
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
path.mkdir(parents=True, exist_ok=True)
print(json.dumps({"ok": True, "deployment_dir": str(path)}))
'''


_DEPLOY = r'''
import hashlib, json, subprocess, sys, zipfile
from pathlib import Path
bundle, expected, installer, target, bootstrap_python, apply_text = sys.argv[1:7]
apply = apply_text == "true"
def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
def run(args, timeout=900):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"args": [str(x) for x in args], "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
observed = sha256(bundle)
payload = {"ok": False, "bundle_sha256": observed, "expected_sha256": expected, "dry_run": None, "apply": None, "venv": None, "requirements": None, "server_check": None}
if observed != expected:
    payload["error"] = {"code": "bundle_hash_mismatch"}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(3)
with zipfile.ZipFile(bundle) as archive:
    prefix = "release/" if "release-manifest.json" in archive.namelist() else "files/"
    trusted_installer = archive.read(prefix + "scripts/harness/install_overlay.py")
if Path(installer).read_bytes() != trusted_installer:
    payload["error"] = {"code": "installer_bundle_mismatch"}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(3)
dry = run([bootstrap_python, installer, "--bundle", bundle, "--target-root", target])
payload["dry_run_command"] = {"returncode": dry["returncode"], "stderr": dry["stderr"][-1200:]}
try:
    payload["dry_run"] = json.loads(dry["stdout"])
except json.JSONDecodeError:
    payload["error"] = {"code": "dry_run_invalid_json", "stdout": dry["stdout"][:1200]}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(4)
plan = payload["dry_run"].get("plan", [])
conflicts = payload["dry_run"].get("conflicts", [])
unsafe = [item.get("path") for item in plan if item.get("safe") is not True]
payload["unsafe"] = unsafe
if dry["returncode"] != 0 or conflicts or unsafe:
    payload["error"] = {"code": "dry_run_gate_failed", "conflict_count": len(conflicts), "unsafe_count": len(unsafe)}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(5)
if not apply:
    payload["ok"] = True
    payload["mode"] = "dry_run"
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(0)
applied = run([bootstrap_python, installer, "--bundle", bundle, "--target-root", target, "--apply", "--plan-sha256", payload["dry_run"]["plan_sha256"]])
payload["apply_command"] = {"returncode": applied["returncode"], "stderr": applied["stderr"][-1200:]}
try:
    payload["apply"] = json.loads(applied["stdout"])
except json.JSONDecodeError:
    payload["error"] = {"code": "apply_invalid_json", "stdout": applied["stdout"][:1200]}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(6)
if applied["returncode"] != 0:
    payload["error"] = {"code": "apply_failed"}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(7)
venv_python = str(Path(target) / ".venv" / "bin" / "python")
if not Path(venv_python).is_file():
    payload["venv"] = run([bootstrap_python, "-m", "venv", str(Path(target) / ".venv")])
    if payload["venv"]["returncode"] != 0:
        payload["error"] = {"code": "venv_create_failed"}
        print(json.dumps(payload, sort_keys=True)); raise SystemExit(8)
else:
    payload["venv"] = {"returncode": 0, "reused": True}
payload["requirements"] = run([venv_python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(Path(target) / "requirements-harness.txt")])
if payload["requirements"]["returncode"] != 0:
    payload["error"] = {"code": "requirements_failed", "stderr": payload["requirements"]["stderr"][-1200:]}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(9)
payload["server_check"] = run([venv_python, str(Path(target) / "plugins" / "solution-agent-harness" / "scripts" / "server.py"), "--check"])
if payload["server_check"]["returncode"] != 0:
    payload["error"] = {"code": "server_check_failed", "stderr": payload["server_check"]["stderr"][-1200:]}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(10)
payload["ok"] = True
payload["mode"] = "apply"
print(json.dumps(payload, sort_keys=True))
'''


_SNAPSHOT_CONFLICTS = r'''
import hashlib, json, os, sys, zipfile
from pathlib import Path, PurePosixPath
bundle, expected, target_text, output_text = sys.argv[1:5]
target = Path(target_text).resolve()
output = Path(output_text)
def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()
def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
observed = sha256(bundle)
payload = {"ok": False, "bundle_sha256": observed, "expected_sha256": expected, "snapshot": str(output)}
if observed != expected:
    payload["error"] = {"code": "bundle_hash_mismatch"}
    print(json.dumps(payload, sort_keys=True)); raise SystemExit(3)
with zipfile.ZipFile(bundle) as archive:
    manifest = json.loads(archive.read("manifest.json"))
    entries = manifest.get("files")
    if manifest.get("schema_version") != "1.0" or not isinstance(entries, list):
        payload["error"] = {"code": "unsupported_bundle_manifest"}
        print(json.dumps(payload, sort_keys=True)); raise SystemExit(4)
    conflicts = []
    current_files = []
    for entry in entries:
        relative = PurePosixPath(str(entry.get("path", "")))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            payload["error"] = {"code": "unsafe_manifest_path", "path": str(relative)}
            print(json.dumps(payload, sort_keys=True)); raise SystemExit(5)
        candidate = target.joinpath(*relative.parts)
        if candidate.is_symlink():
            payload["error"] = {"code": "symlink_target_rejected", "path": relative.as_posix()}
            print(json.dumps(payload, sort_keys=True)); raise SystemExit(6)
        resolved = candidate.resolve()
        if os.path.commonpath([str(target), str(resolved)]) != str(target):
            payload["error"] = {"code": "target_escape", "path": relative.as_posix()}
            print(json.dumps(payload, sort_keys=True)); raise SystemExit(7)
        current_hash = sha256(candidate) if candidate.is_file() else None
        base_hash = entry.get("base_sha256")
        overlay_hash = entry.get("overlay_sha256")
        safe = current_hash == overlay_hash or (base_hash is None and current_hash is None) or (base_hash is not None and current_hash == base_hash)
        if not safe:
            item = {
                "path": relative.as_posix(),
                "current_sha256": current_hash,
                "base_sha256": base_hash,
                "overlay_sha256": overlay_hash,
                "present": candidate.is_file(),
            }
            conflicts.append(item)
            if candidate.is_file():
                current_files.append((relative.as_posix(), candidate))
snapshot_manifest = {
    "schema_version": "1.0",
    "kind": "solution-agent-conflict-snapshot",
    "bundle_sha256": observed,
    "target_root": str(target),
    "conflicts": conflicts,
}
output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name("." + output.name + ".tmp")
with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("manifest.json", json.dumps(snapshot_manifest, sort_keys=True, indent=2) + "\n")
    for relative, candidate in current_files:
        archive.write(candidate, "files/" + relative)
temporary.replace(output)
payload.update({"ok": True, "conflict_count": len(conflicts), "snapshot_sha256": sha256(output), "manifest": snapshot_manifest})
print(json.dumps(payload, sort_keys=True))
'''


def inspect_remote(profile: RemoteDeployProfile) -> dict[str, Any]:
    return _ssh_python(
        profile,
        _INSPECT,
        [profile.solution_agent_root, profile.project_workdir, profile.remote_run_root],
    )


def probe_remote_capabilities(profile: RemoteDeployProfile) -> dict[str, Any]:
    """Probe the declared host once and return a bounded deployment receipt."""

    return _ssh_python(
        profile,
        _CAPABILITY_PROBE,
        [profile.project_workdir, profile.remote_run_root],
    )


def _verify_conflict_snapshot(path: Path, expected_sha256: str) -> dict[str, Any]:
    observed = _sha256(path)
    if observed != expected_sha256:
        raise RuntimeError(f"conflict snapshot SHA-256 mismatch: expected {expected_sha256}, observed {observed}")
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != "1.0" or manifest.get("kind") != "solution-agent-conflict-snapshot":
            raise RuntimeError("unsupported conflict snapshot manifest")
        conflicts = manifest.get("conflicts")
        if not isinstance(conflicts, list):
            raise RuntimeError("conflict snapshot has no conflict list")
        names = set(archive.namelist())
        for item in conflicts:
            relative = PurePosixPath(str(item.get("path", "")))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise RuntimeError(f"unsafe conflict snapshot path: {relative}")
            member = "files/" + relative.as_posix()
            if item.get("present") is True:
                if member not in names:
                    raise RuntimeError(f"conflict snapshot is missing {relative}")
                current_hash = hashlib.sha256(archive.read(member)).hexdigest()
                if current_hash != item.get("current_sha256"):
                    raise RuntimeError(f"conflict snapshot file hash mismatch: {relative}")
            elif member in names:
                raise RuntimeError(f"conflict snapshot unexpectedly contains absent file: {relative}")
    return manifest


def snapshot_profile_conflicts(
    *,
    profiles_path: Path,
    profile_name: str,
    bundle: Path,
    expected_sha256: str,
    output: Path,
    deployment_id: str,
) -> dict[str, Any]:
    profile = load_remote_deploy_profile(profiles_path, profile_name)
    bundle = bundle.resolve()
    output = output.resolve()
    expected = expected_sha256.strip().lower()
    if not bundle.is_file():
        raise FileNotFoundError("bundle must be an existing file")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
    observed = _sha256(bundle)
    if observed != expected:
        raise ValueError(f"local bundle SHA-256 mismatch: expected {expected}, observed {observed}")
    if not _DEPLOYMENT_ID.fullmatch(deployment_id):
        raise ValueError("deployment_id contains unsupported characters")

    identity = inspect_remote(profile)
    if not identity.get("roles_distinct"):
        raise RuntimeError("remote solution checkout, project workdir, and run root roles are not distinct")
    remote_dir = str(PurePosixPath(profile.remote_run_root) / "deployments" / deployment_id)
    _ssh_python(profile, _MKDIR, [remote_dir])
    remote_bundle = str(PurePosixPath(remote_dir) / bundle.name)
    remote_snapshot = str(PurePosixPath(remote_dir) / "conflict-snapshot.zip")
    scp = shutil.which("scp")
    if not scp:
        raise RuntimeError("scp is unavailable for the declared profile lifecycle")
    transfer = _run([scp, "-p", str(bundle), f"{profile.host}:{remote_dir}/"], timeout=300)
    if transfer.returncode != 0:
        raise RuntimeError(f"declared artifact transfer failed ({transfer.returncode}): {transfer.stderr.strip()[:1200]}")
    remote = _ssh_python(
        profile,
        _SNAPSHOT_CONFLICTS,
        [remote_bundle, expected, profile.solution_agent_root, remote_snapshot],
        timeout=300,
    )
    if not remote.get("ok"):
        return {"ok": False, "profile": profile.name, "deployment_id": deployment_id, "identity": identity, "remote": remote}
    output.parent.mkdir(parents=True, exist_ok=True)
    receive = _run([scp, "-p", f"{profile.host}:{remote_snapshot}", str(output)], timeout=300)
    if receive.returncode != 0:
        raise RuntimeError(f"declared snapshot retrieval failed ({receive.returncode}): {receive.stderr.strip()[:1200]}")
    manifest = _verify_conflict_snapshot(output, str(remote["snapshot_sha256"]))
    if manifest.get("bundle_sha256") != expected:
        raise RuntimeError("conflict snapshot references the wrong deployment bundle")
    return {
        "ok": True,
        "profile": profile.name,
        "host_alias": profile.host,
        "deployment_id": deployment_id,
        "identity": identity,
        "remote_snapshot": remote_snapshot,
        "local_snapshot": str(output),
        "snapshot_sha256": remote["snapshot_sha256"],
        "conflict_count": len(manifest["conflicts"]),
        "conflicts": manifest["conflicts"],
    }


def deploy_profile_bundle(
    *,
    profiles_path: Path,
    profile_name: str,
    bundle: Path,
    installer: Path,
    expected_sha256: str,
    deployment_id: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    profile = load_remote_deploy_profile(profiles_path, profile_name)
    bundle = bundle.resolve()
    installer = installer.resolve()
    expected = expected_sha256.strip().lower()
    if not bundle.is_file() or not installer.is_file():
        raise FileNotFoundError("bundle and installer must be existing files")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
    observed = _sha256(bundle)
    if observed != expected:
        raise ValueError(f"local bundle SHA-256 mismatch: expected {expected}, observed {observed}")
    if deployment_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        deployment_id = f"{stamp}-{observed[:12]}"
    if not _DEPLOYMENT_ID.fullmatch(deployment_id):
        raise ValueError("deployment_id contains unsupported characters")

    identity = inspect_remote(profile)
    if not identity.get("roles_distinct"):
        raise RuntimeError("remote solution checkout, project workdir, and run root roles are not distinct")
    solution = identity.get("solution_agent_root") if isinstance(identity.get("solution_agent_root"), dict) else {}
    if not solution.get("exists") or not solution.get("nonempty"):
        raise RuntimeError("remote solution-agent checkout is absent or empty; use the bootstrap installer instead")

    remote_dir = str(PurePosixPath(profile.remote_run_root) / "deployments" / deployment_id)
    _ssh_python(profile, _MKDIR, [remote_dir])
    remote_bundle = str(PurePosixPath(remote_dir) / bundle.name)
    remote_installer = str(PurePosixPath(remote_dir) / installer.name)
    scp = shutil.which("scp")
    if not scp:
        raise RuntimeError("scp is unavailable for the declared profile lifecycle")
    transfer = _run([scp, "-p", str(bundle), str(installer), f"{profile.host}:{remote_dir}/"], timeout=300)
    if transfer.returncode != 0:
        raise RuntimeError(f"declared artifact transfer failed ({transfer.returncode}): {transfer.stderr.strip()[:1200]}")

    remote = _ssh_python(
        profile,
        _DEPLOY,
        [remote_bundle, expected, remote_installer, profile.solution_agent_root, profile.python_bootstrap, "true" if apply else "false"],
        timeout=1200,
    )
    return {
        "ok": bool(remote.get("ok")),
        "profile": profile.name,
        "host_alias": profile.host,
        "deployment_id": deployment_id,
        "declared_artifacts": [remote_bundle, remote_installer],
        "identity": identity,
        "remote": remote,
    }

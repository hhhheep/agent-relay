"""Read-only, profile-bound evidence capture before a managed runtime upgrade."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.harness.build_bootstrap_release import collect_release_files
from solution_agent.execution_port.io_utils import atomic_write_json
from solution_agent.harness.profile_deploy import _ssh_python
from solution_agent.harness.remote_profile import load_remote_deploy_profile

AUDIT = r'''
import base64, hashlib, json, os, sys, zlib
from pathlib import Path
root = Path(sys.argv[1]).resolve()
expected = json.loads(zlib.decompress(base64.b64decode(sys.argv[2])))
files = {}
for name, desired in expected.items():
    path = root / name
    if root not in path.resolve().parents or path.is_symlink():
        files[name] = {"unsafe": True}; continue
    if not path.exists():
        files[name] = {"sha256": None}; continue
    if not path.is_file() or path.stat().st_size > 2000000:
        files[name] = {"unsafe": True}; continue
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    item = {"sha256": actual}
    if actual != desired:
        item["content_base64"] = base64.b64encode(data).decode()
    files[name] = item
owned = {}
for file in (root / ".solution-agent-harness-deployments").glob("*/install-ledger.json"):
    try: ledger = json.loads(file.read_text())
    except (OSError, ValueError): continue
    verified = set(ledger.get("verified", []))
    for entry in ledger.get("plan", []):
        name, value = entry.get("path"), entry.get("overlay_sha256")
        if name in verified and isinstance(value, str):
            owned.setdefault(name, []).append({"sha256": value, "ledger": str(file)})
processes = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try:
        if proc.stat().st_uid != os.getuid(): continue
        command = (proc / 'cmdline').read_bytes()
    except OSError: continue
    if b'run_fsfm_d1_verified.py' in command or b'solution_agent.harness.mcp_server' in command or b'terminal_commands.py\x00run' in command:
        processes.append({"pid": int(proc.name), "kind": "fsfm_d1" if b'run_fsfm_d1_verified.py' in command else "harness"})
print(json.dumps({"root": str(root), "files": files, "ledger_hashes": owned, "processes": processes}))
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in collect_release_files(ROOT)}
    profile = load_remote_deploy_profile(ROOT / "config/harness_remote_deploy_profiles.yaml", args.profile)
    evidence = _ssh_python(profile, AUDIT, [profile.solution_agent_root,
        base64.b64encode(zlib.compress(json.dumps(expected).encode())).decode()])
    evidence["profile"] = profile.name
    evidence["local_expected_hashes"] = expected
    atomic_write_json(args.output, evidence)
    changed = [name for name, item in evidence["files"].items()
               if item.get("sha256") is not None and item.get("sha256") != expected[name]]
    print(json.dumps({"evidence": str(args.output.resolve()), "changed": changed,
                      "processes": evidence["processes"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()

"""Project-local Windows pre-push checks; no hooks, installs or auto-regeneration."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PATH_TESTS = (
    'tests/test_md_sync.py',
    'tests/test_publication_registry.py',
    'tests/test_harness_mcp.py',
)


def main():
    if len(sys.argv) != 1:
        print('Usage: .venv/Scripts/python.exe scripts/release/pre_push.py', file=sys.stderr)
        return 2
    if os.name != 'nt':
        print('NOT AVAILABLE: Windows-native path checks require Windows; run on a Windows checkout.', file=sys.stderr)
        return 2
    if sys.version_info[:2] != (3, 13) or sys.prefix == sys.base_prefix:
        print('Use the isolated Python 3.13 environment prepared by install_dependencies.py.', file=sys.stderr)
        return 2
    checks = (
        ('Publication privacy', ['scripts/extraction/check-publication.py']),
        ('Reviewed engineering provenance', ['scripts/release/provenance.py']),
        ('Windows path, snapshot and MCP regressions', ['-m', 'pytest', *PATH_TESTS, '-q', '-ra']),
    )
    for label, args in checks:
        print(f'CHECK: {label}', flush=True)
        result = subprocess.run([sys.executable, *args], cwd=ROOT)
        if result.returncode:
            print(f'BLOCKED: {label}; do not push. Fix and rerun the gate.', file=sys.stderr)
            return result.returncode if result.returncode > 0 else 1
    print('PASS: pre-push minimum checks. Run additional affected suites; this is not full release acceptance.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

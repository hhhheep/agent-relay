"""Explicit, repo-local dependency setup; never registers or starts services."""
from __future__ import annotations
import argparse, os, platform, shutil, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def run(args,cwd=ROOT): subprocess.run([str(a) for a in args],cwd=cwd,check=True)
def executable(name):
    candidate=shutil.which(name+'.cmd') if os.name=='nt' else None
    return candidate or shutil.which(name) or name
def main():
    p=argparse.ArgumentParser();p.add_argument('--role',choices=['windows-client','linux-worker'],required=True)
    p.add_argument('--venv',type=Path,default=ROOT/'.venv');p.add_argument('--node-only',action='store_true')
    p.add_argument('--python-only',action='store_true');p.add_argument('--with-xats',action='store_true');args=p.parse_args()
    if args.node_only and args.python_only: p.error('Choose at most one dependency subset')
    expected='Windows' if args.role=='windows-client' else 'Linux'
    if platform.system()!=expected: p.error(f'{args.role} requires {expected}; cross-platform native installation is not supported')
    if not args.node_only:
        if sys.version_info[:2]!=(3,13): p.error('Candidate dependency set requires Python 3.13')
        venv=args.venv.resolve();python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not python.is_file(): run([sys.executable,'-m','venv',venv])
        if not (venv/'pyvenv.cfg').is_file(): p.error('Destination must be an isolated venv')
        if 'include-system-site-packages = true' in (venv/'pyvenv.cfg').read_text(): p.error('System-site-packages must be disabled')
        requirements='requirements-extraction-windows.txt' if args.role=='windows-client' else 'requirements-worker-linux.txt'
        run([python,'-m','pip','--isolated','install','--require-virtualenv','--index-url','https://pypi.org/simple','-r',ROOT/requirements])
    if args.role=='windows-client' and not args.python_only:
        version=subprocess.run([executable('node'),'--version'],capture_output=True,text=True,check=True).stdout.strip()
        if not version.startswith('v24.'): p.error('Node 24 is required for this candidate')
        for relative in ['packages/report-provider','components/lifecycle']:
            run([executable('npm'),'ci','--ignore-scripts','--no-audit','--no-fund'],ROOT/relative)
        run([executable('pnpm'),'install','--frozen-lockfile','--ignore-scripts'],ROOT/'plugins/research-report-writer')
        if args.with_xats:
            run([executable('pnpm'),'install','--frozen-lockfile','--ignore-scripts'],ROOT/'components/xats-routing-lab/upstream')
            print('XATS native better-sqlite3 must be rebuilt for this OS/Node ABI; run build-local only after native verification.')
    print('Dependencies installed locally. No plugin, hook, schedule or service was activated.')
if __name__=='__main__': main()

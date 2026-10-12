"""Allowlisted immutable Markdown snapshots. Transport/install is separate."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path,PurePosixPath


def sha(data): return hashlib.sha256(data).hexdigest()


def relative_md(name):
    if not isinstance(name,str) or '\\' in name or ':' in name:
        raise ValueError('invalid Markdown path')
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or p.as_posix()!=name or p.suffix.lower()!='.md':
        raise ValueError('invalid Markdown path')
    return p


def safe_file(root,name):
    rel=relative_md(name); path=root
    for part in rel.parts:
        path=path/part
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise ValueError('linked Markdown path')
    path.resolve().relative_to(root.resolve())
    return path


def build_bundle(root,names):
    root=Path(root).resolve()
    if not names or len(names)>128 or len(set(n.casefold() for n in names))!=len(names):
        raise ValueError('invalid Markdown allowlist')
    files={}
    for name in sorted(names):
        data=safe_file(root,name).read_bytes()
        if len(data)>1024*1024: raise ValueError('Markdown file too large')
        files[name]=data.decode('utf-8')
    if sum(len(v.encode()) for v in files.values())>4*1024*1024:
        raise ValueError('Markdown bundle too large')
    manifest={k:sha(v.encode()) for k,v in files.items()}
    version=sha(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode())
    return {'version':version,'manifest':manifest,'files':files}


def verify_snapshot(path,manifest):
    path=Path(path)
    if not path.is_dir() or path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
        raise ValueError('invalid snapshot directory')
    actual={p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file()}
    if actual!=set(manifest): raise ValueError('snapshot file set mismatch')
    for name,expected in manifest.items():
        if sha(safe_file(path,name).read_bytes())!=expected: raise ValueError('snapshot hash mismatch')


def materialize(bundle,versions_root):
    manifest=bundle['manifest']; files=bundle['files']
    if not files or set(files)!=set(manifest) or len(files)>128:
        raise ValueError('invalid snapshot bundle')
    if len(set(n.casefold() for n in files))!=len(files): raise ValueError('colliding paths')
    for name,data in files.items():
        relative_md(name)
        if not isinstance(data,str) or len(data.encode())>1024*1024 or sha(data.encode())!=manifest[name]:
            raise ValueError('bundle hash mismatch')
    if sum(len(v.encode()) for v in files.values())>4*1024*1024: raise ValueError('bundle too large')
    version=sha(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode())
    if version!=bundle['version']: raise ValueError('version mismatch')
    root=Path(versions_root).resolve(); root.mkdir(parents=True,exist_ok=True)
    target=root/version
    if target.exists(): verify_snapshot(target,manifest); return target
    temporary=Path(tempfile.mkdtemp(prefix='.staging-',dir=root))
    try:
        for name,data in files.items():
            path=safe_file(temporary,name); path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('wb') as handle:
                handle.write(data.encode()); handle.flush(); os.fsync(handle.fileno())
        verify_snapshot(temporary,manifest)
        # Never replace a published version; concurrent publishers verify winner.
        try: temporary.rename(target)
        except OSError:
            if not target.exists(): raise
            verify_snapshot(target,manifest)
        return target
    finally:
        if temporary.exists(): shutil.rmtree(temporary)

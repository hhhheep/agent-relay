"""Transactional task binding registry provisioned by the trusted publisher."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path


def key(task_id,work_unit_id):
    return hashlib.sha256(json.dumps({'task_id':task_id,'work_unit_id':work_unit_id},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def install(path,binding,*,stale_seconds=600,markdown=None,workplace=None):
    if set(binding)!={'task_id','work_unit_id','worker_id','origin_chat_id'} or any(not isinstance(v,str) or not v.strip() or len(v)>256 for v in binding.values()):
        raise ValueError('invalid publication binding')
    if type(stale_seconds) is not int or not 1<=stale_seconds<=86400: raise ValueError('invalid deadline')
    if markdown is not None:
        from solution_agent.harness.md_snapshot import verify_snapshot
        if set(markdown)!={'version','manifest'} or not isinstance(markdown['manifest'],dict) or not markdown['manifest']:
            raise ValueError('invalid Markdown publication')
        version=hashlib.sha256(json.dumps(markdown['manifest'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if version!=markdown['version'] or workplace is None:
            raise ValueError('invalid Markdown publication version or workplace')
        cwd=Path(workplace).resolve(strict=True)
        snapshot=cwd/'.solution-agent'/'md-versions'/version
        snapshot.resolve().relative_to(cwd)
        verify_snapshot(snapshot,markdown['manifest'])
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(path),timeout=10)
    try:
        # Small configuration registry: default rollback journal avoids racing
        # journal-mode switches when several publishers initialize it together.
        db.executescript('''PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS publications (key TEXT PRIMARY KEY,body TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);''')
        payload={'binding':binding,'stale_seconds':stale_seconds}
        if markdown is not None: payload['markdown']=markdown
        body=json.dumps(payload,sort_keys=True,ensure_ascii=False)
        identity=key(binding['task_id'],binding['work_unit_id'])
        with db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT body,enabled FROM publications WHERE key=?',(identity,)).fetchone()
            if old and (old[0]!=body or old[1]!=1): raise ValueError('publication rebinding or disabled task requires explicit migration')
            db.execute('INSERT OR IGNORE INTO publications(key,body) VALUES (?,?)',(identity,body))
        receipt={'installed':True,'binding':binding,'key':identity}
        if markdown is not None: receipt['markdown']=markdown
        return receipt
    finally: db.close()


def lookup(path,identity):
    path=Path(path)
    if not path.is_file(): return None
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    try:
        row=db.execute('SELECT body,enabled FROM publications WHERE key=?',(identity,)).fetchone()
        if not row: return None
        return {**json.loads(row[0]),'enabled':row[1]==1}
    finally: db.close()

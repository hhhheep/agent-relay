"""Synthetic managed-file/state preservation; not an M4 deployment claim."""
from __future__ import annotations
import hashlib, importlib.util, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
builder=load('p1_bundle','scripts/harness/build_deployment_bundle.py')
installer=load('p1_overlay','scripts/harness/install_overlay.py')
def apply(archive,target):
    plan=installer.install_bundle(archive,target)
    assert not plan['conflicts']
    return installer.install_bundle(archive,target,apply=True,expected_plan_sha256=plan['plan_sha256'])
def test_managed_update_and_rollback_preserve_external_config_and_wal_state(tmp_path):
    old=tmp_path/'old';new=tmp_path/'new';target=tmp_path/'installed'
    for directory in [old,new,target]: directory.mkdir()
    (old/'runtime.txt').write_text('old-runtime',encoding='utf-8')
    (new/'runtime.txt').write_text('candidate-runtime',encoding='utf-8')
    allowlist=tmp_path/'files.txt';allowlist.write_text('runtime.txt\n')
    config=target/'config.local.json';config.write_text('{"binding_id":"original-workplace"}')
    state=target/'state';state.mkdir();db=sqlite3.connect(state/'jobs.sqlite')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE receipts(command_id TEXT PRIMARY KEY, execution_release TEXT, artifact_sha TEXT)')
        row=('original-command-id','original-execution-release','a'*64)
        db.execute('INSERT INTO receipts VALUES(?,?,?)',row);db.commit()
        backup=sqlite3.connect(tmp_path/'backup.sqlite')
        db.backup(backup);assert backup.execute('SELECT * FROM receipts').fetchall()==[row];backup.close()
        initial=tmp_path/'initial.zip';builder.build_bundle(old,tmp_path/'absent-baseline',allowlist,initial)
        apply(initial,target)
        forward=tmp_path/'forward.zip';builder.build_bundle(new,old,allowlist,forward)
        before=config.read_bytes();apply(forward,target)
        assert (target/'runtime.txt').read_text()=='candidate-runtime'
        assert config.read_bytes()==before
        assert db.execute('SELECT * FROM receipts').fetchall()==[row]
        reverse=tmp_path/'reverse.zip';builder.build_bundle(old,new,allowlist,reverse)
        apply(reverse,target)
        assert (target/'runtime.txt').read_text()=='old-runtime'
        assert config.read_bytes()==before
        assert db.execute('SELECT * FROM receipts').fetchall()==[row]
    finally: db.close()

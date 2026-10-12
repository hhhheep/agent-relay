"""Local old-install rehearsal only. Requires an explicit original install artifact.

Does not connect SSH, activate plugins, touch original archive/configuration, or
claim M1-M4. All installation/state changes stay in the supplied isolated root.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, sqlite3, subprocess, sys, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
bootstrap=load('rehearsal_bootstrap','scripts/harness/install_bootstrap_release.py')
overlay=load('rehearsal_overlay','scripts/harness/install_overlay.py')
builder=load('rehearsal_builder','scripts/harness/build_bootstrap_release.py')
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def run_code(python,target,code):
    env=dict(os.environ,PYTHONPATH=str(target),SOLUTION_AGENT_ROOT=str(target))
    for key in ['SOLUTION_AGENT_PROFILES_FILE','SOLUTION_AGENT_PROFILE_ID','REPORT_WRITER_ENV_FILE']:
        env.pop(key,None)
    result=subprocess.run([str(python),'-c',code],cwd=target,env=env,capture_output=True,text=True,check=True)
    return json.loads(result.stdout)
def identity(python,target):
    result=run_code(python,target,"from solution_agent.harness.release_identity import build_runtime_identity;from dataclasses import asdict;from pathlib import Path;import json;print(json.dumps(asdict(build_runtime_identity(Path.cwd()))))")
    return {key:result[key] for key in ['release_id','source_root_sha256','mcp_schema_sha256']}
def apply(archive,target):
    plan=overlay.install_bundle(archive,target)
    if plan['conflicts']: raise RuntimeError(f"isolated update has {len(plan['conflicts'])} conflicts")
    return overlay.install_bundle(archive,target,apply=True,expected_plan_sha256=plan['plan_sha256'])
def main():
    p=argparse.ArgumentParser();p.add_argument('--old-archive',type=Path,required=True);p.add_argument('--old-sha256',required=True)
    p.add_argument('--python',type=Path,required=True);p.add_argument('--workdir',type=Path,required=True);args=p.parse_args()
    old=args.old_archive.resolve();python=args.python.resolve();work=args.workdir.resolve()
    allowed=(ROOT/'.verification').resolve()
    if allowed not in work.parents: p.error('Rehearsal workdir must be strictly below this repo .verification')
    if work.exists(): p.error('Use a new empty rehearsal workdir; preserve previous receipts')
    if digest(old)!=args.old_sha256: p.error('Original install archive hash mismatch')
    with zipfile.ZipFile(old) as z:
        original=json.loads(z.read('release-manifest.json'))
        for entry in original['files']:
            if hashlib.sha256(z.read('release/'+entry['path'])).hexdigest()!=entry['sha256']: p.error('Old payload hash mismatch')
    work.mkdir(parents=True);target=work/'installed'
    installed=bootstrap.install_release(old,args.old_sha256,target,apply=True)
    # Archived profile remains inert so original identity hashes stay intact; no profile loader is called.
    loaded_old=identity(python,target)
    assert loaded_old['release_id']==original['release_id']
    state=target/'synthetic-state';state.mkdir()
    config=target/'config.local.json';config.write_bytes(b'{"binding_id":"synthetic-original-workplace"}')
    configured_hash=digest(config)
    command=run_code(python,target,
        "import json,sys;from pathlib import Path;from solution_agent.harness.terminal_commands import TerminalCommandRunner;"
        f"r=TerminalCommandRunner(Path('synthetic-state/jobs'),cwd=Path.cwd(),shell=sys.executable,runtime_release_id={original['release_id']!r});"
        "print(json.dumps(r.execute(command=\"print('original-result')\",command_id=None,task_id='original-task',wait_ms=5000,max_preview_bytes=128)))")
    assert command['state']=='succeeded'
    command_id=command['command_id'];receipt=Path(command['result_manifest_path']);receipt_hash=digest(receipt)
    db=sqlite3.connect(state/'synthetic.sqlite');db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE commands(command_id TEXT PRIMARY KEY, execution_release TEXT)')
    db.execute('INSERT INTO commands VALUES(?,?)',(command_id,original['release_id']));db.commit()
    backup=sqlite3.connect(work/'consistent-backup.sqlite');db.backup(backup);backup.close()
    old_hashes={entry['path']:entry['sha256'] for entry in original['files']}
    reviewed={}
    for name in builder.collect_release_files(ROOT):
        if name in old_hashes and digest(ROOT/name)!=old_hashes[name]:
            reviewed[name]={'remote_sha256':old_hashes[name],'source_sha256':digest(ROOT/name),'reason':'isolated original install artifact exact baseline hash'}
    forward=work/'forward.zip';built=builder.build_release(ROOT,forward,migration_review={'files':reviewed})
    apply(forward,target)
    loaded_new=identity(python,target);assert loaded_new['release_id']==built['manifest']['release_id']
    def preserved():
        assert digest(config)==configured_hash and digest(receipt)==receipt_hash
        assert db.execute('SELECT * FROM commands').fetchall()==[(command_id,original['release_id'])]
        recovered=run_code(python,target,
            "import json,sys;from pathlib import Path;from solution_agent.harness.terminal_commands import TerminalCommandRunner;"
            "r=TerminalCommandRunner(Path('synthetic-state/jobs'),cwd=Path.cwd(),shell=sys.executable,runtime_release_id='current-reader');"
            f"print(json.dumps(r.execute(command=None,command_id={command_id!r},task_id='original-task',wait_ms=0,max_preview_bytes=128)))")
        assert recovered['state']=='succeeded' and recovered['runtime_release_id']==original['release_id']
    preserved()
    current={entry['path']:entry['sha256'] for entry in built['manifest']['files']}
    reverse=json.loads(json.dumps(original))
    for entry in reverse['files']:
        if entry['path'] in current: entry['base_sha256']=current[entry['path']]
    reverse['migration_review']={'origin_archive_sha256':args.old_sha256,'scope':'isolated rollback exact current hashes; original payload bytes unchanged'}
    rollback=work/'rollback.zip'
    with zipfile.ZipFile(old) as source,zipfile.ZipFile(rollback,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('release-manifest.json',json.dumps(reverse))
        for entry in reverse['files']: z.writestr('release/'+entry['path'],source.read('release/'+entry['path']))
    apply(rollback,target)
    loaded_back=identity(python,target);assert loaded_back==loaded_old;preserved();db.close()
    report={'scope':'LOCAL_HARNESS_COMPONENT_REHEARSAL_NOT_M4','original_archive_sha256':args.old_sha256,
        'old_identity':loaded_old,'candidate_identity':loaded_new,'rollback_identity':loaded_back,
        'original_command_id':command_id,'receipt_sha256':receipt_hash,'config_preserved':True,
        'sqlite_consistent_backup':True,'original_command_recovered_without_resubmit':True,'rollback_passed':True,
        'linux_native':'NOT_RUN','M1_M4':'NOT_RUN'}
    (work/'report.local.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in {'original_command_id','receipt_sha256'}}))
if __name__=='__main__':main()

"""Build source-only P1 candidates through the existing Harness builder."""
from __future__ import annotations
import hashlib, importlib.util, json, subprocess, sys, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
def main():
    guard=load('publication','scripts/extraction/check-publication.py')
    names=guard.publication_files(); report=guard.inspect(names)
    if not report['passed']: print(json.dumps(report));raise SystemExit(1)
    subprocess.run([sys.executable,str(ROOT/'scripts/release/provenance.py')],check=True,cwd=ROOT)
    builder=load('bootstrap','scripts/harness/build_bootstrap_release.py')
    selected=builder.collect_release_files(ROOT)
    if set(selected)-set(names): raise SystemExit('Worker builder selected files outside Git candidates')
    out=ROOT/'dist/p1-candidate';out.mkdir(parents=True,exist_ok=True)
    worker=builder.build_release(ROOT,out/'harness-worker.zip')
    prefixes=('plugins/','packages/report-provider/','tools/research-background/',
              'components/lifecycle/','components/goal-routing-integration/','components/xats-routing-lab/upstream/',
              'scripts/extraction/','scripts/release/','docs/engineering/phase-one/')
    client_names=sorted(set(selected) | {name for name in names if name.startswith(prefixes) or name in {'README.md','project.json'}})
    subprocess.run(['node',str(ROOT/'scripts/release/build_writer_plugin.mjs')],check=True,cwd=ROOT)
    plugin=out/'research-report-writer.zip'
    with zipfile.ZipFile(plugin,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((out/'research-report-writer').rglob('*')):
            if path.is_file(): archive.write(path,path.relative_to(out/'research-report-writer').as_posix())
    client=out/'windows-client-source.zip'
    with zipfile.ZipFile(client,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in client_names: archive.write(ROOT/name,name)
    with zipfile.ZipFile(client) as archive:
        if archive.namelist()!=client_names: raise RuntimeError('Client inventory mismatch')
        for name in client_names:
            if archive.read(name)!=(ROOT/name).read_bytes(): raise RuntimeError('Client byte mismatch')
    commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    dirty=bool(subprocess.run(['git','status','--porcelain'],cwd=ROOT,capture_output=True,text=True,check=True).stdout)
    artifacts={}
    for name in ['harness-worker.zip','windows-client-source.zip','install_bootstrap_release.py','install_overlay.py','research-report-writer.zip']:
        path=out/name
        artifacts[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
    manifest={'status':'P1_CANDIDATE_NOT_VERIFIED','source_commit':commit,'source_dirty':dirty,
        'targets':{'windows-x64-client':'LOCAL_VALIDATION_ONLY','linux-x64-worker':'NOT_RUN'},
        'matrix':{key:'NOT_RUN' for key in ['M1','M2','M3','M4']},
        'harness_identity':{key:worker['manifest'][key] for key in ['protocol_version','release_id','source_root_sha256','mcp_schema_sha256']},
        'artifacts':artifacts,'client_files':len(client_names),'worker_files':worker['files'],
        'source_changes_sha256':hashlib.sha256((ROOT/'docs/engineering/phase-one/source-changes.json').read_bytes()).hexdigest()}
    (out/'candidate.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest))
if __name__=='__main__': main()

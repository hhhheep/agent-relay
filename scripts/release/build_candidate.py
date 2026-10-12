"""Build source-only P1 candidates through the existing Harness builder."""
from __future__ import annotations
import hashlib, importlib.util, json, subprocess, sys, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def delivery_verified(project, identity, dirty):
    review=project.get('delivery_verification',{})
    current=project.get('fresh_archive_acceptance',{})
    return (not dirty and project.get('phase_one_verified') is True
        and review.get('status')=='S1_S5_VERIFIED'
        and all(review.get('requirements',{}).get(key)=='PASSED' for key in ['S1','S2','S3','S4','S5'])
        and all(str(project.get('deployment_matrix',{}).get(key,'')).startswith('PASSED') for key in ['M1','M2','M3','M4'])
        and review.get('release_id')==current.get('release_id')==identity['release_id']
        and review.get('source_root_sha256')==current.get('source_root_sha256')==identity['source_root_sha256'])
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
    client_manifest_name='plugins/solution-agent-harness/release_manifest.json'
    if client_manifest_name in client_names: raise RuntimeError('Client release manifest must be generated, not authored')
    # The Worker archive's payload digest identifies archive contents. The
    # launcher compares runtime source identity, exactly as its route() expects.
    client_manifest={key:worker['manifest'][key] for key in
                     ['protocol_version','release_id','source_root_sha256','mcp_schema_sha256']}
    client_manifest['worker_archive_sha256']=worker['sha256']
    client_manifest_bytes=(json.dumps(client_manifest,indent=2)+'\n').encode('utf-8')
    with zipfile.ZipFile(client,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in client_names: archive.write(ROOT/name,name)
        archive.writestr(client_manifest_name,client_manifest_bytes)
    with zipfile.ZipFile(client) as archive:
        if archive.namelist()!=client_names+[client_manifest_name]: raise RuntimeError('Client inventory mismatch')
        if archive.read(client_manifest_name)!=client_manifest_bytes: raise RuntimeError('Client manifest byte mismatch')
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
        'artifacts':artifacts,'client_files':len(client_names)+1,'worker_files':worker['files'],
        'source_changes_sha256':hashlib.sha256((ROOT/'docs/engineering/phase-one/source-changes.json').read_bytes()).hexdigest()}
    project=json.loads((ROOT/'project.json').read_text(encoding='utf-8'))
    acceptance=project.get('workplace_acceptance',{})
    if (acceptance.get('candidate_release_id')==worker['manifest']['release_id'] and
        acceptance.get('candidate_source_root_sha256')==worker['manifest']['source_root_sha256']):
        manifest['targets']['linux-x64-worker']='ISOLATED_INSTALL_AND_HARNESS_STDIO_VERIFIED'
        manifest['matrix']=project['deployment_matrix']
        manifest['prior_component_acceptance']=acceptance
    current=project.get('fresh_archive_acceptance',{})
    if (current.get('release_id')==worker['manifest']['release_id'] and
        current.get('source_root_sha256')==worker['manifest']['source_root_sha256']):
        manifest['targets']={
            'windows-x64-client':'SAME_HOST_CLEAN_INSTALL_COMPONENTS_VERIFIED',
            'linux-x64-worker':'ISOLATED_CLEAN_INSTALL_COMPONENTS_VERIFIED'}
        manifest['matrix']=project['deployment_matrix']
        manifest['prior_runtime_acceptance']=current
        # These receipts describe the matching runtime, not the bytes of a
        # rebuilt archive. Keep the tested artifact hashes and source commit.
        # No component receipt promotes the whole system to P1 Verified.
    # Component acceptance alone cannot promote the delivery. Only a completed
    # S1-S5 review of the matching runtime, full matrix and clean producer can.
    if delivery_verified(project,manifest['harness_identity'],dirty):
        manifest['status']='P1_ENGINEERING_VERIFIED'
        manifest['delivery_verification']=project['delivery_verification']
    (out/'candidate.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest))
if __name__=='__main__': main()

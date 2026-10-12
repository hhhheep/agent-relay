"""Build and verify a design inventory; does not migrate or edit runtime files."""
from __future__ import annotations
import argparse, ast, collections, csv, hashlib, json, pathlib, re, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parent
BASELINE = '6337753ed436e9d230b50c2e1c2356f6c8bf4ee5'

def git(*args):
    return subprocess.check_output(['git', '--no-optional-locks', *args], cwd=ROOT)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def classify(p):
    # Each baseline file has exactly one future authority. Additional generated
    # launchers/bindings are build outputs, not alternate editable source paths.
    if p.startswith('solution_agent/'):
        group = p.split('/')[1] if len(p.split('/')) > 2 else 'root'
        treatment = 'retain-namespace'
        if group in {'continuation', 'orchestrator', 'remote_materializer', 'small_task_worker', 'agent', 'registry'} or p == 'solution_agent/web.py':
            treatment = 'retain-legacy-candidate-no-delete'
        return 'harness-runtime', 'packages/harness-runtime/src/' + p, ['harness-wheel','harness-worker'], treatment, 'Original Python namespace remains one distribution; support closure verified in C1.'
    if p == 'cli.py':
        return 'harness-runtime', 'deployment/compatibility/cli.py', ['harness-wheel'], 'compatibility-launcher', 'Legacy entry delegates to existing public CLI; no duplicated runtime.'
    if p.startswith('components/xats-routing-lab/upstream/'):
        return 'xats-vendor', 'vendor/xats/' + p.removeprefix('components/xats-routing-lab/upstream/'), ['xats-bundle'], 'vendor-preserve-selected-source', 'Selected upstream snapshot includes existing plugins/tests; not all upstream features are supported targets.'
    if p.startswith('components/goal-routing-integration/vendor/'):
        tail = p.rsplit('/',1)[1]
        if tail == 'paperclip-goal.ts':
            return 'lifecycle-manager', 'apps/lifecycle-manager/src/integrations/paperclip-goal.ts', ['lifecycle-bundle'], 'derived-adapter-preserve-attribution', 'Locally adapted Paperclip source; provenance and license remain linked.'
        return 'paperclip-provenance', 'vendor/paperclip-provenance/' + tail, ['lifecycle-bundle'], 'provenance-license', 'Original revision and license; not a second editable parser.'
    if p.startswith('components/goal-routing-integration/'):
        tail = p.removeprefix('components/goal-routing-integration/')
        if tail == 'package.json':
            return 'lifecycle-manager', 'docs/extraction/legacy-inputs/goal-routing.package.json', ['source-only'], 'consolidate-metadata-retain-provenance', 'Merge supported dependencies into lifecycle manifest; archive original metadata.'
        if tail == 'app-chat-phase2.json':
            return 'lifecycle-manager', 'templates/lifecycle/app-chat-phase2.example.json', ['test-only'], 'synthetic-template', 'Generic extracted fixture; not a live binding.'
        dest = 'apps/lifecycle-manager/tests/goal-routing/' + tail if '.test.' in tail else 'apps/lifecycle-manager/src/integrations/goal-routing/' + tail
        return 'lifecycle-manager', dest, ['test-only'] if '.test.' in tail else ['lifecycle-bundle'], 'move-owner-preserve-behavior', 'Same lifecycle and routing ownership; rewrite import paths once in C2.'
    if p.startswith('components/lifecycle/'):
        tail = p.removeprefix('components/lifecycle/')
        if tail == 'production-xats-service-20261008.ts':
            return 'xats-service', 'apps/xats-service/src/service.ts', ['xats-bundle'], 'isolate-vendor-entry', 'Only new vendor-port may deep-import pinned startServer; no upstream public API claim.'
        if tail.endswith('-rpc.py'):
            stem = tail.removesuffix('.py').replace('-','_')
            return 'harness-runtime', 'packages/harness-runtime/src/solution_agent/harness/integration_rpc/' + stem + '.py', ['harness-wheel','lifecycle-bundle'], 'public-rpc-generated-compat-wrapper', 'Runtime owns RPC implementation; legacy filepath wrappers generated only.'
        if tail == 'build-router-package.mjs':
            return 'deployment', 'deployment/lifecycle/build-router-package.mjs', ['lifecycle-bundle'], 'adapt-existing-builder', 'Keep existing manifest/release authority; update declared inputs.'
        if tail == 'package.json':
            dest = 'apps/lifecycle-manager/package.json'
        elif tail == 'worker-md-allowlist.json':
            dest = 'deployment/lifecycle/worker-md-allowlist.json'
        elif '.test.' in tail:
            dest = 'apps/lifecycle-manager/tests/' + tail
        else:
            dest = 'apps/lifecycle-manager/src/' + tail
        return 'lifecycle-manager', dest, ['test-only'] if '.test.' in tail else ['lifecycle-bundle'], 'move-owner-preserve-behavior', 'Keep Windows session/manager behavior and external state roots.'
    if p.startswith('plugins/research-report-writer/'):
        tail = p.removeprefix('plugins/research-report-writer/')
        if tail.startswith(('.codex-plugin/','skills/')) or tail == '.mcp.json':
            return 'writer-plugin', p, ['writer-plugin'], 'plugin-metadata-skill-launcher', 'Plugin declaration targets generated official app payload.'
        if tail == 'lib/provider.mjs':
            return 'report-provider', 'packages/report-provider/src/provider.mjs', ['writer-plugin','background-bundle'], 'extract-shared-transport-caller-schema', 'Remove reverse Writer import; move report orchestration to Writer and preserve body/config/usage fixtures.'
        if tail.startswith('examples/'):
            return 'report-writer', 'templates/report-writer/' + tail.removeprefix('examples/'), ['test-only'], 'synthetic-template', 'Generic evidence/rules examples only.'
        if tail == 'pnpm-lock.yaml':
            return 'report-writer', 'docs/extraction/legacy-inputs/writer.pnpm-lock.yaml', ['source-only'], 'lock-provenance-new-root-lock', 'Retain original lock provenance; formal own workspace has one root resolution authority.'
        return 'report-writer', 'apps/report-writer/' + tail, ['test-only'] if tail.startswith('tests/') else ['writer-plugin','writer-remote'], 'move-owner-preserve-behavior', 'Writer owns report schema/business and remote adapter; packaged paths verified in C2/C3.'
    if p.startswith('plugins/research-token-guard/'):
        tail = p.removeprefix('plugins/research-token-guard/')
        scripts = 'skills/research-token-guard/scripts/'
        if tail.startswith(scripts):
            return 'token-guard', 'packages/token-guard/src/research_token_guard/' + tail.removeprefix(scripts), ['guard-wheel','guard-plugin'], 'single-package-generated-compat-launcher', 'Five existing CLI programs become one authority; old skill script paths generated as launchers.'
        if tail.startswith('tests/'):
            return 'token-guard', 'packages/token-guard/' + tail, ['test-only'], 'retain-regression-suite', 'Keep current 37-check baseline and required local/remote packaging checks.'
        return 'guard-plugin', p, ['guard-plugin'], 'plugin-metadata-skill-reference', 'Keep skill-only templates and references; no new MCP server.'
    if p.startswith('plugins/solution-agent-harness/'):
        tail = p.removeprefix('plugins/solution-agent-harness/')
        if tail == 'scripts/remote_launcher.py':
            return 'harness-runtime', 'packages/harness-runtime/src/solution_agent/harness/client_launcher.py', ['harness-client-plugin'], 'single-transport-source-generated-launcher', 'SSH transport implementation is runtime-owned; plugin entry generated with manifest identity.'
        return 'harness-plugin', p, ['harness-client-plugin'], 'plugin-metadata-skill-launcher', 'Keep existing launch/check and skill interfaces, no core code copy.'
    if p.startswith('tools/research-background/'):
        tail = p.removeprefix('tools/research-background/')
        if tail in {'windows.ps1','launch-hidden.vbs','NoConsoleLauncher.cs'}:
            return 'deployment', 'deployment/background/' + tail, ['background-bundle'], 'adapt-existing-launcher-no-live-registration', 'Preserve hidden launch and development identities; no execution during design.'
        if tail in {'config.json','config.example.json'} or tail.startswith('fixture-vault/'):
            return 'research-background', 'templates/research-background/' + tail, ['test-only'], 'synthetic-template', 'Extracted config points only to empty fixture; not production settings.'
        dest = 'apps/research-background/tests/' + tail if tail in {'tests.mjs','worker-fixture.mjs'} else 'apps/research-background/' + tail
        return 'research-background', dest, ['test-only'] if tail in {'tests.mjs','worker-fixture.mjs'} else ['background-bundle'], 'move-owner-preserve-behavior', 'Provider adapter stays app-owned and consumes formal common package.'
    if p.startswith('scripts/harness/'):
        return 'deployment', 'deployment/harness/' + p.removeprefix('scripts/harness/'), ['build-input-only'], 'adapt-existing-helper-support-scope-review', 'Keep build/deploy helper identities; research-specific runners remain historical/support-scope candidates.'
    if p.startswith('deploy/'):
        return 'deployment', 'deployment/compatibility/' + p.removeprefix('deploy/'), ['build-input-only'], 'regenerate-allowlist-after-migration', 'Current input allowlist is provenance; formal list follows accepted mapping and package resources.'
    if p.startswith('tests/'):
        return 'runtime-tests', 'tests/runtime/' + p.removeprefix('tests/'), ['test-only'], 'retain-suite-classify-supported-legacy', 'Preserve fixtures/helpers and classify support; no silent skip or live config imports.'
    if p.startswith('config/'):
        return 'templates', 'templates/' + p, ['source-only'], 'generic-template', 'No real credentials/account/host/root bindings.'
    if p.startswith(('benchmarks/','evals/','fixtures/')):
        return 'historical-test-inputs', p, ['test-only'], 'retain-historical-metadata', 'Not an implicit commitment to every research scenario.'
    if p.startswith('docs/'):
        return 'documentation', p, ['source-only'], 'retain-provenance-history', 'Historical statements do not override newest normalization taskbook.'
    if p.startswith('scripts/extraction/'):
        return 'extraction-tooling', p, ['source-only'], 'retain-extraction-audit', 'Keep standalone extraction checks; do not confuse them with formal packaging gates.'
    if p.startswith('requirements'):
        return 'dependency-metadata', 'deployment/compatibility/' + p, ['build-input-only'], 'lock-migration-provenance', 'Original declarations retained; formal compatible locks created in C2.'
    if p in {'.gitattributes','.gitignore','AGENTS.md','README.md','project.json','pytest.ini','.solutionignore'}:
        return 'repository-metadata', p, ['source-only'], 'retain-adapt-repository-metadata', 'Root identity/config/tests guidance updated without production registration.'
    raise ValueError('Unclassified baseline input: ' + p)

def build():
    source = json.loads((ROOT/'docs/extraction/source-map.json').read_text(encoding='utf-8-sig'))
    provenance = {x['target']:x for x in source['files']}
    tree = git('ls-tree','-r','--name-only',BASELINE).decode().splitlines()
    rows=[]; python_edges=collections.defaultdict(set); node_edges=[]
    for path in tree:
        data=git('show',f'{BASELINE}:{path}')
        owner,target,artifacts,treatment,reason=classify(path)
        p=provenance.get(path)
        rows.append({'baseline_path':path,'baseline_sha256':digest(data),'source_alias':p['source'] if p else 'publication-metadata','source_path':p['source_path'] if p else path,'origin_sha256':p['source_sha256'] if p else None,'owner':owner,'target_module':owner,'planned_authority_path':target,'artifact_disposition':artifacts,'treatment':treatment,'editable_authority':not treatment.startswith(('vendor-','provenance-','lock-provenance')),'status':'planned-not-migrated','reason':reason})
        text=data.decode('utf-8-sig')
        if path.startswith('solution_agent/') and path.endswith('.py'):
            group=path.split('/')[1] if len(path.split('/'))>2 else 'root'
            for node in ast.walk(ast.parse(text)):
                names=[node.module or ''] if isinstance(node,ast.ImportFrom) else [n.name for n in node.names] if isinstance(node,ast.Import) else []
                for name in names:
                    if name.startswith('solution_agent.'):
                        python_edges[group].add(name.split('.')[1])
        if path.startswith(('components/lifecycle/','components/goal-routing-integration/','plugins/research-report-writer/','tools/research-background/')) and path.endswith(('.ts','.mjs')) and '.test.' not in path:
            imports=re.findall(r"(?:from\s+|import\s*\(|require\s*\()['\"]([^'\"]+)['\"]",text)
            if imports:node_edges.append({'file':path,'static_literal_imports':imports})
    assert len(rows)==941 and len(provenance)==904
    paths=[x['planned_authority_path'] for x in rows]
    assert len(set(paths))==len(paths), 'Destination collision'
    payload={'schema':'agent-relay-migration-design/v1','baseline_commit':BASELINE,'status':'planned-not-migrated','mapped_source_count':904,'baseline_file_count':941,'files':rows}
    (OUT/'migration-map.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    with (OUT/'migration-map.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=list(rows[0]);w=csv.DictWriter(f,fields,lineterminator='\n');w.writeheader()
        for row in rows:w.writerow({**row,'artifact_disposition':'|'.join(row['artifact_disposition'])})
    evidence={'baseline_commit':BASELINE,'analysis_limit':'Python absolute solution_agent imports and Node literal imports only; relative/dynamic runtime closure not fully proven.','python_absolute_import_edges':{k:sorted(v) for k,v in sorted(python_edges.items())},'node_literal_imports':node_edges,'manual_dynamic_dependencies':[{'file':'tools/research-background/provider.mjs','dependency':'config.provider_module -> writer lib/provider.mjs; createRequire(openai) from that module','decision':'C3 common package, caller-owned schema/preset, runtime allowlist/fixtures'}],'vendor_facts':{'xats':'0.8.6 CLI/channel bins; no declared startServer library export; one vendor-port internal import exception','paperclip':'Derived adapter with local structural type/RPC function adaptation; keep attribution/provenance'}}
    (OUT/'dependency-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    summary={'baseline_commit':BASELINE,'files':len(rows),'mapped_sources':len(provenance),'publication_metadata':len(rows)-len(provenance),'by_owner':dict(sorted(collections.Counter(x['owner'] for x in rows).items())),'by_treatment':dict(sorted(collections.Counter(x['treatment'] for x in rows).items())),'source_runtime_moves_performed':False,'formal_gates_executed':False}
    (OUT/'migration-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(summary,ensure_ascii=False))

def check():
    payload=json.loads((OUT/'migration-map.json').read_text(encoding='utf-8'))
    rows=payload['files']; source=json.loads((ROOT/'docs/extraction/source-map.json').read_text(encoding='utf-8-sig'))['files']
    expected=set(git('ls-tree','-r','--name-only',BASELINE).decode().splitlines())
    assert len(rows)==941 and {r['baseline_path'] for r in rows}==expected
    assert len({r['planned_authority_path'] for r in rows})==941
    assert len(source)==904 and {x['target'] for x in source}<=expected
    source_lookup={x['target']:x for x in source}
    for r in rows:
        assert digest(git('show',f"{BASELINE}:{r['baseline_path']}"))==r['baseline_sha256']
        target=pathlib.PurePosixPath(r['planned_authority_path'])
        assert not target.is_absolute() and '..' not in target.parts
        assert r['owner'] and r['artifact_disposition'] and r['status']=='planned-not-migrated'
        if r['baseline_path'] in source_lookup:
            assert r['baseline_sha256']==source_lookup[r['baseline_path']]['target_sha256']
        if not r['baseline_path'].startswith('docs/') and r['baseline_path'] not in {'README.md','AGENTS.md'}:
            assert digest((ROOT/r['baseline_path']).read_bytes())==r['baseline_sha256'], 'Runtime/baseline unexpectedly changed: '+r['baseline_path']
    with (OUT/'migration-map.csv').open(encoding='utf-8-sig',newline='') as f:
        csv_rows=list(csv.DictReader(f))
    assert len(csv_rows)==len(rows)
    for a,b in zip(rows,csv_rows):
        assert a['baseline_path']==b['baseline_path'] and a['planned_authority_path']==b['planned_authority_path']
        assert '|'.join(a['artifact_disposition'])==b['artifact_disposition']
    missing=[]
    for p in OUT.glob('*.md'):
        for link in re.findall(r'\]\(([^)]+)\)',p.read_text(encoding='utf-8')):
            if ':' in link or link.startswith('#'):continue
            path=(p.parent/link.split('#')[0]).resolve()
            if not path.exists():missing.append(f'{p.name}:{link}')
    assert not missing, missing
    print(json.dumps({'design_inventory':'passed','baseline_files':941,'mapped_sources':904,'unique_destinations':941,'csv_matches':True,'runtime_and_nonpolicy_baseline_unchanged':True,'intentional_policy_edits':['README.md','AGENTS.md'],'local_links':'passed','formal_engineering_gates':'not_run'},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    check() if args.check else build()

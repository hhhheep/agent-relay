"""Local fake transports only; never invokes SSH or original research state."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import uuid

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/solution-agent-harness/scripts/workplace_owner.py'
spec = importlib.util.spec_from_file_location('workplace_owner_fixture', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def until(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(.03)
    raise AssertionError('fixture did not reach expected state')


def test_reconnect_idle_proof_refuses_live_descendant_then_allows_empty_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'private-state'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'private-state'))
    monkeypatch.delenv(guard.LEASE_ENV, raising=False)
    identity = tmp_path / 'workplace.id'
    identity.write_bytes(('solution-agent-workplace-v1:' + uuid.uuid4().hex + '\n').encode())
    host = tmp_path / 'host.py'
    host.write_text('''import subprocess,sys,json
args=[sys.executable,sys.argv[1],'--identity',sys.argv[2],'--verify-transport-idle']
before=subprocess.run(args,capture_output=True,text=True)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
try: busy=subprocess.run(args,capture_output=True,text=True)
finally: child.terminate();child.wait(timeout=5)
after=subprocess.run(args,capture_output=True,text=True)
print(json.dumps({'before':before.returncode,'busy':busy.returncode,'after':after.returncode,'error':busy.stderr}))
''', encoding='utf8')
    result = subprocess.run([sys.executable, str(SCRIPT), '--identity', str(identity), '--cooldown-seconds', '0',
                             '--', sys.executable, str(host), str(SCRIPT), str(identity)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof['before'] == proof['after'] == 0
    assert proof['busy'] == 2 and 'owner_transport_tree_still_active' in proof['error']


def test_node_host_can_verify_empty_tree_with_async_python_redirector(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'private-state'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'private-state'))
    monkeypatch.delenv(guard.LEASE_ENV, raising=False)
    identity = tmp_path / 'workplace.id'
    identity.write_bytes(('solution-agent-workplace-v1:' + uuid.uuid4().hex + '\n').encode())
    host = tmp_path / 'host.mjs'
    host.write_text("""import {spawn} from 'node:child_process';
const p=spawn(process.argv[2],[process.argv[3],'--identity',process.argv[4],'--verify-transport-idle'],{windowsHide:true,stdio:['ignore','pipe','pipe']});
p.stdout.pipe(process.stdout);p.stderr.pipe(process.stderr);p.once('close',code=>process.exitCode=code);
""", encoding='utf8')
    result = subprocess.run([sys.executable, str(SCRIPT), '--identity', str(identity), '--cooldown-seconds', '0',
                             '--', shutil.which('node'), str(host), sys.executable, str(SCRIPT), str(identity)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['transport_idle'] is True


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'private-state'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'private-state'))
    monkeypatch.delenv(guard.LEASE_ENV, raising=False)
    identity = tmp_path / 'workplace.id'
    identity.write_bytes(('solution-agent-workplace-v1:' + uuid.uuid4().hex + '\n').encode('utf8'))
    alias = tmp_path / 'other-profile-workplace.id'; alias.write_bytes(identity.read_bytes())
    fake = tmp_path / 'fake_ssh.py'
    fake.write_text('''import json,os,subprocess,sys,time
from pathlib import Path
p=Path(sys.argv[1])
if sys.argv[2]=='quick':
 with (p/'starts.jsonl').open('a') as f:f.write(json.dumps({'pid':os.getpid()})+'\\n')
 sys.exit(0)
result=subprocess.run([sys.executable,sys.argv[3],'--identity',sys.argv[4],'--verify-inherited'],capture_output=True,text=True)
if result.returncode:sys.stderr.write(result.stderr);sys.exit(4)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
with (p/'starts.jsonl').open('a') as f:f.write(json.dumps({'pid':os.getpid(),'child':child.pid,'inherited':json.loads(result.stdout)['verified']})+'\\n')
while not (p/'finish').exists():time.sleep(.02)
''', encoding='utf8')
    processes = []
    def start(selected=identity, mode='hold', cooldown=0):
        p = subprocess.Popen([sys.executable, str(SCRIPT), '--identity', str(selected), '--cooldown-seconds', str(cooldown),
                              '--', sys.executable, str(fake), str(tmp_path), mode, str(SCRIPT), str(alias)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p); return p
    def records():
        p = tmp_path / 'starts.jsonl'
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    def owner():return json.loads((guard.state_root() / (guard.workplace_key(identity) + '.json')).read_text())
    yield tmp_path, identity, alias, start, records, owner
    (tmp_path / 'finish').touch()
    for p in processes:
        try:p.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            # Recorded guardian identity, never a guessed or unrelated process.
            record = owner()
            if guard.process_marker(record['guardian']['pid']) == record['guardian']['marker']:
                os.kill(record['guardian']['pid'], signal.SIGTERM)
            p.communicate(timeout=8)


def test_workplace_aliases_and_parallel_entrypoints_share_one_os_lease(fixture):
    root, identity, alias, start, records, owner = fixture
    first = start(); until(lambda: records())
    assert records()[0]['inherited'] is True
    others = [start(alias) for _ in range(8)]
    for p in others:
        _, err = p.communicate(timeout=8)
        assert p.returncode == 2 and 'owner_workplace_busy' in err
    assert len(records()) == 1
    child_pid = records()[0]['child']
    (root / 'finish').touch(); _, err = first.communicate(timeout=8)
    assert first.returncode == 0, err
    until(lambda: guard.process_marker(child_pid) is None)
    assert owner()['state'] == 'closed'


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object process-tree crash contract')
@pytest.mark.parametrize('victim', ['guardian', 'root'])
def test_crash_reaps_owned_tree_before_a_new_transport(fixture, victim):
    root, identity, alias, start, records, owner = fixture
    first = start(); until(lambda: records())
    before = records()[0]; record = owner(); target = record[victim]
    assert guard.process_marker(target['pid']) == target['marker']
    os.kill(target['pid'], signal.SIGTERM)
    first.communicate(timeout=8)
    until(lambda: guard.process_marker(before['pid']) is None and guard.process_marker(before['child']) is None)
    second = start(alias, 'quick'); _, err = second.communicate(timeout=8)
    assert second.returncode == 0, err
    assert len(records()) == 2


def test_attempt_cooldown_survives_owner_exit(fixture):
    root, identity, alias, start, records, owner = fixture
    first = start(mode='quick'); _, err = first.communicate(timeout=8)
    assert first.returncode == 0, err
    second = start(alias, 'quick', cooldown=60); _, err = second.communicate(timeout=8)
    assert second.returncode == 2 and 'owner_start_cooldown' in err
    assert len(records()) == 1


def test_manual_broker_log_and_exit_receipt_are_durable(fixture):
    root, identity, alias, start, records, owner = fixture
    log = root / 'private-host.log'
    result = subprocess.run([sys.executable, str(SCRIPT), '--identity', str(identity),
                             '--log-file', str(log), '--', sys.executable, '-c',
                             'print("bounded fixture output");raise SystemExit(7)'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 7
    receipt = owner()
    assert receipt['state'] == 'closed' and receipt['root_exit_code'] == 7
    assert receipt['closed_at'] >= receipt['root_exited_at'] >= receipt['attempt_at']
    assert receipt['parent_pid'] > 0
    text = log.read_text(encoding='utf-8')
    assert 'guardian_manual_start' in text and 'guardian_manual_exit' in text
    assert 'bounded fixture output' in text and receipt['nonce'] not in text


def test_long_private_state_path_does_not_prevent_guarded_start(fixture, monkeypatch):
    root, identity, alias, start, records, owner = fixture
    long_state = root / ('private-state-' + 's' * 130) / ('nested-' + 'n' * 70)
    monkeypatch.setenv('LOCALAPPDATA', str(long_state));monkeypatch.setenv('XDG_STATE_HOME', str(long_state))
    try:
        first = start(mode='quick'); _, err = first.communicate(timeout=8)
        assert first.returncode == 0, err
        assert len(records()) == 1
    finally:
        target = long_state.parent.resolve()
        assert target.is_relative_to(root.resolve()) and target.name.startswith('private-state-')
        extended = Path('\\\\?\\' + str(target)) if os.name == 'nt' else target
        if extended.exists():shutil.rmtree(extended)


def test_inherited_nonce_cannot_authorize_an_uncontained_process(fixture, monkeypatch):
    root, identity, alias, start, records, owner = fixture
    first = start(); until(lambda: records())
    record = owner()
    monkeypatch.setenv(guard.LEASE_ENV, json.dumps({'key':record['key'], 'nonce':record['nonce']}))
    with pytest.raises(RuntimeError, match='inherited_lease_invalid'):
        guard.validate_inherited(alias)
    assert len(records()) == 1


def test_corrupt_attempt_record_cannot_disable_cooldown(fixture):
    root, identity, alias, start, records, owner = fixture
    first=start(mode='quick');_,err=first.communicate(timeout=8);assert first.returncode==0,err
    path=guard.state_root()/(guard.workplace_key(identity)+'.json')
    record=owner();record['attempt_at']=float('nan');path.write_text(json.dumps(record),encoding='utf8')
    second=start(alias,'quick');_,err=second.communicate(timeout=8)
    assert second.returncode==2 and 'owner_state_invalid' in err
    assert len(records())==1


@pytest.mark.skipif(os.name != 'nt', reason='Windows execution Job lifetime contract')
def test_manual_broker_survives_caller_job_cleanup_with_fake_host(tmp_path):
    repo = Path(__file__).resolve().parents[1]; audit=tmp_path; run=tmp_path/'lifetime';run.mkdir()
    guard_path=repo/'plugins/solution-agent-harness/scripts/workplace_owner.py'
    spec=importlib.util.spec_from_file_location('fixture_guard',guard_path);guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    base=Path(getattr(sys,'_base_executable',sys.executable));node=Path(shutil.which('node'));loader=repo/'components/lifecycle/node_modules/tsx/dist/loader.mjs';launch=repo/'components/lifecycle/persistent-harness-launch.ts'
    hold=run/'persistent-harness-host-cli.mjs';hold.write_text("import{existsSync,writeFileSync}from'node:fs';import{dirname,join}from'node:path';const r=dirname(process.argv[3]);writeFileSync(join(r,'ready.json'),JSON.stringify({pid:process.pid}));setInterval(()=>{if(existsSync(join(r,'finish')))process.exit(0);},25);",encoding='utf-8')
    broker=run/'broker.mjs';broker.write_text("import{readFileSync}from'node:fs';import{pathToFileURL}from'node:url';const {launchPersistentWindows}=await import(pathToFileURL(process.argv[2]).href);const p=JSON.parse(readFileSync(process.argv[3],'utf8'));console.log(JSON.stringify(await launchPersistentWindows(p,process.env)));",encoding='utf-8')
    bootstrap=run/'bootstrap.py';bootstrap.write_text('''import json,os,subprocess,sys,time
    from pathlib import Path
    p=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));root=Path(p['root']);os.environ['LOCALAPPDATA']=str(root/'state')
    with (root/'bootstrap.log').open('w',encoding='utf-8') as log:
     if p['mode']=='old':
      subprocess.Popen([p['python'],p['guardian'],'--identity',p['identity'],'--cooldown-seconds','0','--log-file',p['hostPath']+'.log','--',p['node'],p['hostEntry'],'host',p['hostPath']],creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
     else:
      result=subprocess.run([p['node'],'--import',p['loader'],p['broker'],p['launch'],p['input']],stdout=log,stderr=log);assert result.returncode==0
     while not (root/'finish').exists():time.sleep(.05)
    '''.replace('\n    ', '\n'),encoding='utf-8')
    def until(predicate):
     end=time.monotonic()+15
     while time.monotonic()<end:
      if predicate():return
      time.sleep(.05)
     raise AssertionError('fixture wait timeout')
    results=[]
    for mode in ['old','broker']:
     root=run/mode;root.mkdir();identity=root/'workplace.id';identity.write_bytes(('solution-agent-workplace-v1:'+uuid.uuid4().hex+'\n').encode('utf-8'));host=root/'host.json';host.write_text(json.dumps({'owner':{'workplaceIdentityPath':str(identity)}}),encoding='utf-8')
     # Host path and identity are fixture-only; actual launcher uses the same validator.
     input_path=root/'input.json';payload={'python':str(base),'guardian':str(guard_path),'node':str(node),'hostEntry':str(hold),'hostPath':str(host),'identity':str(identity),'cooldown':0}
     input_path.write_text(json.dumps(payload),encoding='utf-8');params={**payload,'root':str(root),'mode':mode,'loader':loader.as_uri(),'broker':str(broker),'launch':str(launch),'input':str(input_path)};parameter=root/'params.json';parameter.write_text(json.dumps(params),encoding='utf-8')
     tree=guard.WindowsTree(hashlib.sha256(uuid.uuid4().bytes).hexdigest());log=(root/'parent.log').open('w',encoding='utf-8');process=subprocess.Popen([str(base),str(bootstrap),str(parameter)],creationflags=4,stdout=log,stderr=log)
     guardian_record=None
     try:
      tree.adopt_and_resume(process.pid);until(lambda:(root/'ready.json').exists());ready=json.loads((root/'ready.json').read_text());record_path=root/'state/AgentRelay/transport-owners'/(guard.workplace_key(identity)+'.json');until(record_path.exists);guardian_record=json.loads(record_path.read_text(encoding='utf-8'))
      tree.close();process.wait(timeout=5);log.close();time.sleep(.3)
      alive=guard.process_marker(ready['pid']) is not None;assert alive==(mode=='broker')
      before=json.loads(record_path.read_text(encoding='utf-8'));assert before['state']=='running'
      if mode=='broker':(root/'finish').touch();until(lambda:json.loads(record_path.read_text(encoding='utf-8'))['state']=='closed');until(lambda:guard.process_marker(ready['pid']) is None)
      else:until(lambda:guard.process_marker(guardian_record['guardian']['pid']) is None)
      results.append({'mode':mode,'survivedCallerJobClose':alive,'staleRunningRecordAfterKilledOldCaller':mode=='old','fixtureProcessesExited':True})
     finally:
      (root/'finish').touch();tree.close();log.close()
    report={'passed':True,'run':str(run),'schoolConnections':0,'realOwnerChanges':0,'results':results};(audit/'lifetime-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))

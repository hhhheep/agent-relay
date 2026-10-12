import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from solution_agent.harness import terminal_commands as commands


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='actual Linux supervisor death and independent wait')
@pytest.mark.parametrize('outcome',['exit','timeout','cancel'])
def test_guardian_recovers_after_supervisor_kill(tmp_path,outcome):
    import signal
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    code='from pathlib import Path;import time,sys;Path("executions").open("a").write("once\\n");print("started",flush=True)\nwhile not Path("release").exists():time.sleep(.05)\nsys.exit(7)'
    original=runner.execute(command=code,command_id=None,wait_ms=0,max_preview_bytes=128,
                            task_id='RECOVERY-TASK',work_unit_id='RECOVERY-UNIT',
                            timeout_seconds=2 if outcome=='timeout' else 15)
    directory=runner._directory(original['command_id'])
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        status=json.loads((directory/'status.json').read_text())
        if status['state']=='running' and (tmp_path/'executions').exists():break
        time.sleep(.02)
    assert status['state']=='running' and status['execution_guardian_pid']!=status['supervisor_pid']
    try:
        os.kill(status['supervisor_pid'],signal.SIGKILL)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            observed=runner.reconcile(original['command_id'])
            if observed.get('process_tracking',{}).get('supervisor_missing'):break
            time.sleep(.02)
        assert observed['state']=='running' and observed['process_tracking']['supervisor_missing']
        binding={key:original[key] for key in ('command_id','task_id','work_unit_id')}
        monitor=runner.read_result(original['command_id'],128)['recovery_monitor']
        assert monitor['command_id']==original['command_id']
        again=commands.recover_command_monitor(runner,binding,duration=12,interval=.1)
        assert again['pid']==monitor['pid'] and again['relaunch_permitted'] is False
        if outcome=='exit':
            os.kill(monitor['pid'],signal.SIGKILL)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and commands._process_snapshot(monitor['pid'],monitor['process_marker'],monitor['host_boot_id'])['state']!='exited':time.sleep(.02)
            replacement=commands.recover_command_monitor(runner,binding,duration=12,interval=.1)
            assert replacement['pid']!=monitor['pid'] and replacement['command_id']==original['command_id']
            monitor=replacement
        if outcome=='exit':(tmp_path/'release').touch()
        if outcome=='cancel':runner.cancel(original['command_id'])
        result=runner.wait_result(original['command_id'],10000)
        assert result['state']==('failed' if outcome=='exit' else 'cancelled')
        assert result['exit_code']==(7 if outcome=='exit' else -signal.SIGTERM)
        assert result.get('failure_reason')==(None if outcome=='exit' else 'timeout_exceeded' if outcome=='timeout' else 'cancel_requested')
        receipt=json.loads((directory/'result_manifest.json').read_text())
        assert receipt['exit_code']==result['exit_code'] and receipt['command_id']==original['command_id']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if Path(monitor['snapshot']).exists():
                snapshot=json.loads(Path(monitor['snapshot']).read_text())
                if snapshot['monitor_state']=='complete':break
            time.sleep(.02)
        assert snapshot['monitor_state']=='complete' and snapshot['commands'][0]['exit_code']==result['exit_code']
        assert (tmp_path/'executions').read_text()=='once\n'
        assert len(list(runner.jobs_root.glob('*/request.json')))==1
    finally:
        for key in ('supervisor_pid','execution_guardian_pid','child_pid'):
            try:os.kill(status[key],signal.SIGKILL)
            except ProcessLookupError:pass


def test_recovery_rejects_other_scope_and_ambiguous_observer(tmp_path):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("never submitted")',task_id='TASK',work_unit_id='UNIT')
    binding={'command_id':request['command_id'],'task_id':'OTHER','work_unit_id':'UNIT'}
    with pytest.raises(commands.CommandReceiptError):commands.recover_command_monitor(runner,binding)
    assert not (runner.jobs_root/'.monitor').exists()
    binding['task_id']='TASK';root=runner.jobs_root/'.monitor';root.mkdir()
    (root/(request['command_id']+'.observer.json')).write_text('{')
    result=commands.recover_command_monitor(runner,binding)
    assert result['monitor_state']=='unknown' and result['relaunch_permitted'] is False
    assert not list(root.glob('*.bindings.json'))


def test_guardian_startup_gap_preserves_uncertainty(tmp_path):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("never submitted")');directory=runner._directory(request['command_id'])
    (directory/'launcher.json').write_text(json.dumps({'supervisor_pid':2147483647,'supervisor_process_marker':'gone','host_boot_id':commands._boot_id()}))
    result=runner.reconcile(request['command_id'])
    assert result['state']=='prepared' and result['process_tracking']['state']=='unknown'
    assert not (directory/'completion_event.json').exists()


@pytest.mark.parametrize('fault',['valid','foreign','corrupt'])
def test_existing_terminal_receipt_recovers_finalization_gap(tmp_path,fault):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("never resubmit")',task_id='TASK',work_unit_id='UNIT')
    directory=runner._directory(request['command_id'])
    receipt={**{key:request[key] for key in ('command_id','command_sha256','task_id','work_unit_id')},
             'schema_version':2,'state':'failed','exit_code':7,'started_at':'original-start','finished_at':'original-finish',
             'failure_reason':None,'signal':None}
    if fault=='foreign':receipt['task_id']='OTHER'
    (directory/'result_manifest.json').write_text('{' if fault=='corrupt' else json.dumps(receipt))
    original=(directory/'result_manifest.json').read_bytes()
    result=runner.reconcile(request['command_id'])
    if fault=='valid':
        assert result['state']=='failed' and result['exit_code']==7 and result['finished_at']=='original-finish'
        event=json.loads((directory/'completion_event.json').read_text())
        assert event['task_id']=='TASK' and event['occurred_at']=='original-finish'
        assert runner.reconcile(request['command_id'])==result
    else:
        assert result['state']=='prepared' and result['process_tracking']['state']=='unknown'
        assert not (directory/'completion_event.json').exists()
    assert (directory/'result_manifest.json').read_bytes()==original


def test_real_monitor_keeps_original_command_identity(tmp_path):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    result=runner.execute(command='import time;time.sleep(0.4);print("monitor done")',command_id=None,
                          wait_ms=0,max_preview_bytes=128,task_id='MONITOR-TASK',work_unit_id='MONITOR-UNIT')
    binding={'command_id':result['command_id'],'task_id':'MONITOR-TASK','work_unit_id':'MONITOR-UNIT'}
    snapshot=runner.jobs_root/'.monitor'/'snapshot.json'
    observed=commands.monitor_commands(runner,[binding],snapshot,interval=.1,duration=10)
    assert observed['monitor_state']=='complete'
    assert observed['commands'][0]['command_id']==result['command_id']
    assert observed['commands'][0]['state']=='succeeded'
    assert observed['commands'][0]['exit_code']==0
    assert observed['relaunch_permitted'] is False
    assert len(list(runner.jobs_root.glob('*/request.json')))==1


def test_missing_supervisor_does_not_lose_live_child(tmp_path):
    child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE)
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("not dispatched")')
    request['execution_guardian']=False
    (runner._directory(request['command_id'])/'request.json').write_text(json.dumps(request))
    status_path=runner._directory(request['command_id'])/'status.json'
    status=json.loads(status_path.read_text())
    status.update(state='running',execution_guardian=False,supervisor_pid=2147483647,supervisor_process_marker='missing',
                  child_pid=child.pid,child_process_marker=commands._process_marker(child.pid),host_boot_id=commands._boot_id())
    status_path.write_text(json.dumps(status))
    try:
        result=runner.reconcile(request['command_id'])
        assert result['state']=='running'
        assert result['process_tracking']['state']=='alive'
        assert result['process_tracking']['supervisor_missing'] is True
        assert not (runner._directory(request['command_id'])/'completion_event.json').exists()
        assert runner.read_result(request['command_id'],128)['process_tracking']['state']=='alive'
        child.communicate(input=b'x',timeout=5)
        assert runner.reconcile(request['command_id'])['state']=='lost'
    finally:
        if child.poll() is None:child.kill();child.wait()


@pytest.mark.parametrize('evidence', ['unknown_process','unknown_systemd','no_identity'])
def test_uncertain_observation_never_fabricates_terminal_receipt(tmp_path,monkeypatch,evidence):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("not dispatched")');directory=runner._directory(request['command_id'])
    status_path=directory/'status.json';status=json.loads(status_path.read_text())
    if evidence!='no_identity':status.update(state='running',supervisor_pid=123)
    status_path.write_text(json.dumps(status))
    monkeypatch.setattr(commands,'_process_snapshot',lambda *args:{'state':'unknown' if evidence=='unknown_process' else 'exited','reason':'injected'})
    if evidence=='unknown_systemd':
        (directory/'launcher.json').write_text(json.dumps({'systemd_unit':'unit'}))
        monkeypatch.setattr(commands,'_systemd_liveness',lambda *args:{'state':'unknown','reason':'timeout'})
    before={p.name:p.read_bytes() for p in directory.iterdir()}
    result=runner.reconcile(request['command_id'])
    assert result['process_tracking']['state']=='unknown'
    assert result['state'] in {'running','prepared'}
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before


@pytest.mark.parametrize('content', ['{"half":', '[]'])
def test_bad_launcher_preserves_unknown_without_lost_receipt(tmp_path,content):
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("not dispatched")');directory=runner._directory(request['command_id'])
    (directory/'launcher.json').write_text(content)
    before={p.name:p.read_bytes() for p in directory.iterdir()}
    result=runner.reconcile(request['command_id'])
    assert result['process_tracking']['state']=='unknown'
    assert result['process_tracking']['observations'][0]['reason']=='launcher_evidence_unreadable'
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before


@pytest.mark.parametrize('active,pid,expected', [('active',123,'alive'),('activating',0,'alive'),('inactive',0,'exited'),('failed',0,'exited'),('inactive',123,'unknown'),(None,0,'unknown')])
def test_systemd_liveness_uses_execution_state_not_loaded_state(monkeypatch,active,pid,expected):
    monkeypatch.setattr(commands,'_systemd_snapshot',lambda unit:{'LoadState':'loaded','ActiveState':active,'MainPID':pid})
    assert commands._systemd_liveness('unit')['state']==expected


def test_unavailable_systemd_is_unknown(monkeypatch):
    monkeypatch.setattr(commands,'_systemd_snapshot',lambda unit:None)
    assert commands._systemd_liveness('unit')['state']=='unknown'


@pytest.mark.parametrize('fault', ['permission','malformed','gone'])
def test_linux_failed_query_is_not_process_death(monkeypatch,fault):
    monkeypatch.setattr(commands.sys,'platform','linux')
    monkeypatch.setattr(commands,'_boot_id',lambda:'boot')
    original=Path.read_text
    def read(path,*args,**kwargs):
        if str(path).replace('\\','/')=='/proc/123/stat':
            if fault=='permission':raise PermissionError('injected')
            if fault=='gone':raise FileNotFoundError('injected')
            return 'half-written'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    result=commands._process_snapshot(123,'marker','boot')
    assert result['state']==('exited' if fault=='gone' else 'unknown')


@pytest.mark.parametrize('case,expected', [('descendant','alive'),('unrelated','exited'),('unreadable','unknown'),('older_group','unknown')])
def test_linux_group_inventory_handles_orphans_and_incomplete_evidence(monkeypatch,case,expected):
    from types import SimpleNamespace
    monkeypatch.setattr(commands.sys,'platform','linux')
    monkeypatch.setattr(commands,'_boot_id',lambda:'boot')
    monkeypatch.setattr(commands.os,'getuid',lambda:88,raising=False)
    class Entry:
        name='124'
        def stat(self):return SimpleNamespace(st_uid=88)
        def __truediv__(self,other):return self
        def read_text(self,**kwargs):
            if case=='unreadable':raise PermissionError('injected')
            fields=['S','1',str(999 if case=='unrelated' else 123),'123']+['0']*15+[str(50 if case=='older_group' else 200)]
            return '124 (worker with spaces) '+' '.join(fields)
    original=Path.iterdir
    def entries(path):
        if str(path).replace('\\','/')=='/proc':return iter([Entry()])
        return original(path)
    monkeypatch.setattr(Path,'iterdir',entries)
    snapshot=commands._process_group_snapshot(123,'100','boot')
    assert snapshot['state']==expected
    if case=='descendant':assert snapshot['members']==[124]


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='actual Linux process/boot identity')
def test_linux_pid_marker_and_boot_identity():
    pid=os.getpid();marker=commands._process_marker(pid);boot=commands._boot_id()
    assert commands._process_snapshot(pid,marker,boot)['state']=='alive'
    assert commands._process_snapshot(pid,'different',boot)['reason']=='pid_reused'
    assert commands._process_snapshot(pid,marker,'previous-boot')['reason']=='host_rebooted'
    assert commands._process_snapshot(pid,None,boot)['state']=='unknown'


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='actual Linux descendant process group')
def test_surviving_grandchild_is_not_lost_when_leader_exits(tmp_path):
    pidfile=tmp_path/'grandchild.pid'
    code="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(0.5)"
    leader=subprocess.Popen([sys.executable,'-c',code,str(pidfile)],start_new_session=True)
    marker=commands._process_marker(leader.pid);boot=commands._boot_id()
    runner=commands.TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('print("not dispatched")');directory=runner._directory(request['command_id'])
    request['execution_guardian']=False
    (directory/'request.json').write_text(json.dumps(request))
    status=json.loads((directory/'status.json').read_text())
    status.update(state='running',execution_guardian=False,supervisor_pid=2147483647,supervisor_process_marker='missing',child_pid=leader.pid,
                  child_process_marker=marker,child_pgid=leader.pid,host_boot_id=boot)
    (directory/'status.json').write_text(json.dumps(status))
    try:
        leader.wait(timeout=5)
        grandchild=int(pidfile.read_text())
        result=runner.reconcile(request['command_id'])
        assert result['state']=='running'
        group=next(o for o in result['process_tracking']['observations'] if o['role']=='job_group')
        assert group['state']=='alive' and grandchild in group['members']
        assert not (directory/'completion_event.json').exists()
    finally:
        import signal
        try:os.killpg(leader.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        leader.wait(timeout=5)

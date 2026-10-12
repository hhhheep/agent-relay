import json
import sys
import subprocess
from pathlib import Path
import pytest
from solution_agent.harness.terminal_commands import TerminalCommandRunner, monitor_commands


def setup(tmp_path):
    runner=TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    snapshot=tmp_path/'jobs'/'.monitor'/'snapshot.json'
    return runner,snapshot


def test_real_command_monitor_collects_same_command_after_launch(tmp_path):
    runner,snapshot=setup(tmp_path)
    pending=runner.execute(command='import time;time.sleep(0.4);print("finished")',command_id=None,
                           wait_ms=0,max_preview_bytes=128,task_id='TASK',work_unit_id='UNIT')
    original=pending['command_id']
    observed=monitor_commands(runner,[{'command_id':original,'task_id':'TASK','work_unit_id':'UNIT'}],snapshot,interval=.1,duration=5)
    assert observed['monitor_state']=='complete'
    assert observed['commands'][0]['command_id']==original
    assert observed['commands'][0]['state']=='succeeded'
    assert observed['commands'][0]['exit_code']==0
    assert len(list(runner.jobs_root.glob('*/request.json')))==1
    assert json.loads(snapshot.read_text())==observed
    assert observed['relaunch_permitted'] is False


@pytest.mark.parametrize('case',['scope','missing','corrupt'])
def test_unknown_or_foreign_command_never_relaunches_or_mutates_job(tmp_path,case):
    runner,snapshot=setup(tmp_path)
    request=runner.prepare('print("never launched")',task_id='TASK',work_unit_id='UNIT')
    directory=runner._directory(request['command_id'])
    if case=='corrupt':(directory/'status.json').write_text('{')
    before={p.name:p.read_bytes() for p in directory.iterdir()}
    binding={'command_id':'f'*32 if case=='missing' else request['command_id'],
             'task_id':'OTHER' if case=='scope' else 'TASK','work_unit_id':'UNIT'}
    observed=monitor_commands(runner,[binding],snapshot,interval=.1,duration=.1)
    assert observed['monitor_state']=='deadline'
    assert observed['commands'][0]['state']=='observation_unknown'
    assert observed['relaunch_permitted'] is False
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before
    assert not (runner.jobs_root/('f'*32)).exists()


def test_monitor_cannot_overwrite_authoritative_job_or_ignore_bounds(tmp_path):
    runner,snapshot=setup(tmp_path)
    request=runner.prepare('print("never launched")',task_id='TASK',work_unit_id='UNIT')
    binding={'command_id':request['command_id'],'task_id':'TASK','work_unit_id':'UNIT'}
    path=runner._directory(request['command_id'])/'status.json';before=path.read_bytes()
    with pytest.raises(ValueError):monitor_commands(runner,[binding],path,interval=.1,duration=.1)
    with pytest.raises(ValueError):monitor_commands(runner,[binding,binding],snapshot,interval=.1,duration=.1)
    with pytest.raises(ValueError):monitor_commands(runner,[binding],snapshot,interval=0,duration=.1)
    assert path.read_bytes()==before


def test_stop_request_keeps_original_prepared_state(tmp_path):
    runner,snapshot=setup(tmp_path)
    request=runner.prepare('print("never launched")',task_id='TASK',work_unit_id='UNIT')
    observed=monitor_commands(runner,[{'command_id':request['command_id'],'task_id':'TASK','work_unit_id':'UNIT'}],snapshot,
                              interval=.1,duration=20,stopped=lambda:True)
    assert observed['monitor_state']=='stopped'
    assert observed['commands'][0]['state']=='prepared'
    assert observed['commands'][0]['process_tracking']['state']=='unknown'


@pytest.mark.parametrize('target',['outside','command'])
def test_monitor_rejects_snapshot_directory_link(tmp_path,target):
    runner,snapshot=setup(tmp_path)
    request=runner.prepare('print("never launched")',task_id='TASK',work_unit_id='UNIT')
    outside=tmp_path/'outside';outside.mkdir()
    destination=outside if target=='outside' else runner._directory(request['command_id'])
    try:
        (runner.jobs_root/'.monitor').symlink_to(destination,target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks unavailable to this user')
    binding={'command_id':request['command_id'],'task_id':'TASK','work_unit_id':'UNIT'}
    with pytest.raises(ValueError):
        monitor_commands(runner,[binding],snapshot,interval=.1,duration=.1)
    assert not (outside/'snapshot.json').exists()


def test_real_monitor_cli_survives_without_model_or_launch_permission(tmp_path):
    runner,snapshot=setup(tmp_path)
    result=runner.execute(command='import time;time.sleep(0.4);print("done")',command_id=None,
                          wait_ms=0,max_preview_bytes=128,task_id='TASK',work_unit_id='UNIT')
    binding={'command_id':result['command_id'],'task_id':'TASK','work_unit_id':'UNIT'}
    path=tmp_path/'bindings.json';path.write_text(json.dumps([binding]))
    process=subprocess.run([sys.executable,'-m','solution_agent.harness.terminal_commands','monitor',
                            '--jobs-root',str(runner.jobs_root),'--cwd',str(tmp_path),'--bindings',str(path),
                            '--snapshot',str(snapshot),'--interval','.1','--duration','5'],
                           capture_output=True,text=True,timeout=10)
    assert process.returncode==0,process.stderr
    receipt=json.loads(process.stdout)
    assert receipt['monitor_state']=='complete' and receipt['relaunch_permitted'] is False
    assert json.loads(snapshot.read_text())['commands'][0]['command_id']==result['command_id']
    assert len(list(runner.jobs_root.glob('*/request.json')))==1

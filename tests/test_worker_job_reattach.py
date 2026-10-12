"""Recovery across the pre-worker launch and lost-response boundaries."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from solution_agent.harness import codex_worker as worker
from solution_agent.harness.terminal_commands import TerminalCommandRunner, CommandReceiptError
from tests.test_harness_codex_worker import packet


def prepare(packet, jobs):
    runner = TerminalCommandRunner(jobs, cwd=Path(packet['cwd']), shell=sys.executable)
    request = runner.prepare("print('isolated test')", task_id=packet['task_id'],
                             work_unit_id=packet['work_unit_id'], access_mode='writer',
                             writer_lease_id='test-only-lease', worker_packet=packet)
    return runner, request


def test_fresh_adapter_recovers_job_before_worker_claim(packet, tmp_path):
    jobs = tmp_path/'jobs'
    _, original = prepare(packet, jobs)
    assert not Path(packet['control']).exists()
    # A new adapter has only the durable packet and OS-job directory.
    restored = json.loads(json.dumps(packet))
    assert worker.submission(restored, jobs_root=jobs) == (None, original['command_id'])
    assert len(list(jobs.glob('*/request.json'))) == 1


@pytest.mark.parametrize('fault', ['different_text', 'missing_link', 'different_command', 'different_scope', 'multiple_jobs'])
def test_ambiguous_or_changed_identity_never_relaunches(packet, tmp_path, fault):
    jobs = tmp_path/'jobs';_, original = prepare(packet, jobs)
    directory = jobs/original['command_id']
    if fault == 'different_text': packet = {**packet, 'text': 'changed task'}
    elif fault == 'missing_link': (directory/'worker.json').unlink()
    elif fault in {'different_command', 'different_scope'}:
        path = directory/('request.json' if fault == 'different_command' else 'worker.json')
        value = json.loads(path.read_text())
        value['command_id' if fault == 'different_command' else 'work_unit_id'] = 'other'
        path.write_text(json.dumps(value))
    else:
        import shutil
        duplicate = jobs/('b'*32);shutil.copytree(directory, duplicate)
        value = json.loads((duplicate/'request.json').read_text());value['command_id'] = duplicate.name
        (duplicate/'request.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):worker.submission(packet, jobs_root=jobs)


def test_existing_os_launch_lock_prevents_two_preparations(packet, tmp_path):
    jobs = tmp_path/'jobs'
    def attempt(_):
        try:return prepare(packet, jobs)[1]['command_id']
        except CommandReceiptError:return 'duplicate-refused'
    with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(attempt, range(2)))
    assert results.count('duplicate-refused') == 1
    assert len(list(jobs.glob('*/request.json'))) == 1


def test_lost_launch_response_recovers_completed_os_result(packet, tmp_path):
    jobs = tmp_path/'jobs';runner, original = prepare(packet, jobs)
    runner.launch(original)
    restored = TerminalCommandRunner(jobs, cwd=Path(packet['cwd']), shell=sys.executable)
    command, command_id = worker.submission(packet, jobs_root=jobs)
    result = restored.execute(command=command, command_id=command_id, task_id=packet['task_id'],
                              work_unit_id=packet['work_unit_id'], wait_ms=5000,max_preview_bytes=256)
    assert result['command_id'] == original['command_id']
    assert result['state'] == 'succeeded' and result['exit_code'] == 0
    assert len(list(jobs.glob('*/request.json'))) == 1


def test_legacy_claim_remains_recoverable_without_new_request_field(packet, tmp_path):
    jobs=tmp_path/'jobs';_,request=prepare(packet,jobs)
    path=jobs/request['command_id']/'request.json';old=json.loads(path.read_text())
    old.pop('worker_request_sha256');path.write_text(json.dumps(old))
    control=Path(packet['control']);control.mkdir(parents=True)
    (control/'claim.json').write_text(json.dumps({'command_id':request['command_id'],'request_sha256':worker._digest(packet)}))
    assert worker.submission(packet,jobs_root=jobs)==(None,request['command_id'])


@pytest.mark.parametrize('found', [False,True])
def test_mcp_lookup_only_never_submits_a_worker(packet,tmp_path,monkeypatch,found):
    from types import SimpleNamespace
    from solution_agent.harness import mcp_server as server
    jobs=tmp_path/'jobs';original=prepare(packet,jobs)[1] if found else None
    calls=[]
    def execute(*args,**kwargs):
        calls.append(kwargs);assert kwargs['command'] is None
        assert kwargs['writer_lease_id'] is None
        return {'ok':True,'state':'running','task_id':packet['task_id'],
                'work_unit_id':packet['work_unit_id'],'command_id':original['command_id']}
    profile=SimpleNamespace(deployment_profile_name='FAN',default_terminal='main',slots={'main':SimpleNamespace(cwd=Path(packet['cwd']))})
    monkeypatch.setattr(server,'_require_activation',lambda:None)
    monkeypatch.setattr(server,'_with_server',lambda value:value)
    monkeypatch.setattr(server,'_solution_root',lambda:Path(packet['root']))
    monkeypatch.setattr(server,'_terminal_manager',lambda:SimpleNamespace(base=tmp_path,profile=profile,exec=execute))
    result=server.terminal_exec(packet['task_id'],worker_task=packet['text'],work_unit_id=packet['work_unit_id'],timeout_seconds=60,recover_worker=True)
    assert result['ok']
    assert bool(calls)==found
    assert result['command_id']==(original['command_id'] if found else None)
    if not found:assert result['state']=='submission_uncertain' and result['lookup_only']
    assert not Path(packet['control']).exists()

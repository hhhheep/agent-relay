import hashlib
from types import SimpleNamespace
import pytest
from solution_agent.harness import mcp_server as server, codex_worker as worker
from solution_agent.harness.maintenance_completion import finish_owned_maintenance
from tests.test_harness_codex_worker import packet


def test_production_allowlist_actual_transfer_registry_and_worker_version(packet):
    import json
    import subprocess
    import sys
    from pathlib import Path
    from solution_agent.harness.task_md_sync import plan_sync, accept_sync
    from solution_agent.harness.publication_registry import install
    root=Path(__file__).resolve().parents[1]
    names=json.loads((root/'worker-md-allowlist.json').read_text())['files']
    assert len(names)==3 and not any(name.startswith('sync-input/') for name in names)
    plan=plan_sync(root,names,remote_python=sys.executable,remote_workplace=packet['cwd'])
    assert len(plan['command'].encode())<=48000
    result=subprocess.run(plan['argv'],capture_output=True,text=True,check=True)
    synced=accept_sync(plan,dict(state='succeeded',exit_code=0,stdout={'preview':result.stdout}))
    snapshot={key:plan[key] for key in ('version','manifest')}
    binding=dict(task_id=packet['task_id'],work_unit_id=packet['work_unit_id'],worker_id='worker',origin_chat_id='origin')
    runtime=Path(packet['root'])
    install(runtime/'config/help-publications.sqlite',binding,markdown=snapshot,workplace=packet['cwd'])
    routed=worker.worker_packet(runtime,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])
    assert routed['help_route']['binding']==binding
    assert routed['markdown_snapshot']['version']==synced['version']
    (Path(synced['directory'])/names[0]).write_text('tampered')
    with pytest.raises(ValueError,match='hash'):
        worker.worker_packet(runtime,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])


@pytest.mark.parametrize('conflict', ['none', 'control', 'job', 'corrupt', 'active', 'missing_request', 'legacy_identity', 'wrong_command', 'foreign_tombstone', 'matching_tombstone'])
def test_legacy_absence_inspects_authoritative_paths_and_fails_closed(packet, monkeypatch, conflict):
    import json
    from pathlib import Path
    from solution_agent.harness import publication_recovery as recovery
    from solution_agent.harness.task_capsule import TaskCapsuleStore, TaskCapsule, ScopeContract
    root=Path(packet['root']);base=root/'jobs-base';base.mkdir()
    store=TaskCapsuleStore(root/'state')
    store.create(TaskCapsule(task_id=packet['task_id'],objective='test',source_release_id='candidate',
                             approval_owner='user',scope=ScopeContract()))
    monkeypatch.setattr(recovery,'load_terminal_profile',lambda root: object())
    monkeypatch.setattr(recovery,'ProfileTerminalManager',lambda profile: SimpleNamespace(base=base))
    if conflict in {'foreign_tombstone','matching_tombstone'}:
        modern=base/'jobs'/'original';legacy=base/'main'/'commands'/'original'
        modern.mkdir(parents=True);legacy.mkdir(parents=True)
        (modern/'status.json').write_text(json.dumps({'command_id':'original','state':'lost'}))
        request={'command_id':'original','task_id':packet['task_id'] if conflict=='matching_tombstone' else 'other-task',
                 'work_unit_id':packet['work_unit_id'],'access_mode':'writer'}
        (legacy/'request.json').write_text(json.dumps(request))
        (legacy/'status.json').write_text(json.dumps({'command_id':'original','state':'running'}))
    if conflict=='control': Path(packet['control']).mkdir(parents=True)
    if conflict=='active': store.start_work_unit(packet['task_id'],'other',expected_version=0)
    if conflict in {'job','corrupt','missing_request','legacy_identity','wrong_command'}:
        job=base/'jobs'/'original';job.mkdir(parents=True)
        request={'task_id':packet['task_id'],'work_unit_id':packet['work_unit_id'],'access_mode':'writer'}
        if conflict=='legacy_identity': request={'command_id':'original','terminal_id':'main'}
        if conflict=='wrong_command': request['command_id']='someone-else'
        if conflict!='missing_request': (job/'request.json').write_text(json.dumps(request))
        (job/'status.json').write_text('broken' if conflict=='corrupt' else json.dumps({'state':'succeeded'}))
    binding=dict(task_id=packet['task_id'],work_unit_id=packet['work_unit_id'],worker_id='worker',origin_chat_id='origin')
    if conflict in {'none','foreign_tombstone'}:
        proof=recovery.inspect_unstarted(root,packet['profile_id'],binding,packet['text'],packet['phase'])
        assert proof['matching_jobs']==[] and proof['binding']==binding
        if conflict=='foreign_tombstone':
            assert proof['inspected_command_count']==1 and len(proof['history_warnings'])==1
    else:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            recovery.inspect_unstarted(root,packet['profile_id'],binding,packet['text'],packet['phase'])
        if conflict in {'missing_request','legacy_identity','wrong_command'}:
            with pytest.raises(recovery.RecoveryEvidenceError) as blocked:
                recovery.inspect_unstarted(root,packet['profile_id'],binding,packet['text'],packet['phase'])
            assert blocked.value.command_id=='original'
            assert blocked.value.evidence_path==str(job)
            assert 'reconcile history' in str(blocked.value)


@pytest.mark.parametrize('entered', [False, True])
def test_worker_failure_proves_only_pre_executor_rejection(tmp_path, monkeypatch, entered):
    calls = []
    class Manager:
        profile = SimpleNamespace(deployment_profile_name='FAN', default_terminal='main',
                                  slots={'main': SimpleNamespace(cwd=tmp_path)})
        def exec(self, *args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('receipt lost after possible launch')
    monkeypatch.setattr(server, '_solution_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_terminal_manager', lambda: Manager())
    monkeypatch.setattr(server, '_with_server', lambda data: data)
    monkeypatch.setattr(server, '_require_activation', lambda: None)
    monkeypatch.setattr(server, '_server_identity', lambda: {'release_id': 'candidate'})
    def packet(*args):
        if not entered:
            raise ValueError('preflight failure')
        return {'cwd': str(tmp_path)}
    monkeypatch.setattr(worker, 'worker_packet', packet)
    monkeypatch.setattr(worker, 'submission', lambda packet: ('never a real command', None))
    result = server.terminal_exec('TASK', work_unit_id='WU', worker_task='任務', timeout_seconds=60)
    assert result['ok'] is False
    assert result['task_id'] == 'TASK' and result['work_unit_id'] == 'WU'
    assert result['submission']['worker_task_sha256'] == hashlib.sha256('任務'.encode()).hexdigest()
    assert result['submission']['outcome'] == ('unknown' if entered else 'rejected')
    assert bool(calls) == entered
    if entered:
        current = server._capsule_store().load('TASK')
        assert current.active_work_unit == 'WU' and len(current.writer_leases) == 1


@pytest.mark.parametrize('case', ['foreign', 'matching', 'unknown', 'conflicting_task', 'conflicting_hash', 'status_conflict'])
def test_recovery_reconciles_generations_without_hiding_unknown_execution(tmp_path, case):
    import json
    from solution_agent.harness.publication_recovery import _inspect_command, RecoveryEvidenceError
    modern=tmp_path/'jobs'/'command';legacy=tmp_path/'main'/'commands'/'command'
    modern.mkdir(parents=True);legacy.mkdir(parents=True)
    binding={'task_id':'current','work_unit_id':'unit'}
    request={'command_id':'command','task_id':'foreign','work_unit_id':'other',
             'access_mode':'writer','command_sha256':'digest'}
    if case=='matching':request.update(task_id='current',work_unit_id='unit')
    if case=='unknown':request={'command_id':'command'}
    (legacy/'request.json').write_text(json.dumps(request))
    (legacy/'status.json').write_text(json.dumps({'command_id':'command','state':'running'}))
    tombstone={'command_id':'command','state':'lost'}
    if case=='status_conflict':tombstone['task_id']='current'
    (modern/'status.json').write_text(json.dumps(tombstone))
    if case in {'conflicting_task','conflicting_hash'}:
        other=dict(request)
        other['task_id' if case=='conflicting_task' else 'command_sha256']='different'
        (modern/'request.json').write_text(json.dumps(other))
    if case in {'unknown','conflicting_task','conflicting_hash','status_conflict'}:
        with pytest.raises(RecoveryEvidenceError):_inspect_command([modern,legacy],binding)
    else:
        match,warnings=_inspect_command([modern,legacy],binding)
        assert warnings==[{'path':str(modern/'request.json'),'reason':'missing'}]
        if case=='foreign':assert match is None
        else:assert match=={'command_id':'command','states':['lost','running']}


@pytest.mark.parametrize('state,active,leases,expected', [
    ('succeeded', 'maintenance', [], True), ('failed', 'maintenance', [], True),
    ('cancelled', 'maintenance', [], True), ('lost', 'maintenance', [], False),
    ('running', 'maintenance', [], False), ('succeeded', 'someone-else', [], False),
    ('succeeded', 'maintenance', ['lease'], False), ('succeeded', None, [], True),
])
def test_maintenance_never_closes_unknown_active_or_other_work(state, active, leases, expected):
    calls = []
    current = SimpleNamespace(active_work_unit=active, writer_leases=leases, version=17)
    def finish(task, unit, *, expected_version):
        calls.append((task, unit, expected_version))
        return SimpleNamespace(version=18)
    store = SimpleNamespace(load=lambda task: current, finish_work_unit=finish)
    receipt = dict(ok=True, task_id='TASK', work_unit_id='maintenance', command_id='original',
                   access_mode='writer', state=state)
    if expected:
        result = finish_owned_maintenance(store, receipt, task_id='TASK', work_unit_id='maintenance')
        assert result['finished']
        assert calls == ([] if active is None else [('TASK', 'maintenance', 17)])
    else:
        with pytest.raises(ValueError):
            finish_owned_maintenance(store, receipt, task_id='TASK', work_unit_id='maintenance')
        assert calls == []

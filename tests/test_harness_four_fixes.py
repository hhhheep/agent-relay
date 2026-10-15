"""Regression coverage for validation, phase, partial and lost-response recovery."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from solution_agent.harness import codex_worker as worker
from solution_agent.harness.terminal_commands import (
    CommandObservationError, TerminalCommandRunner,
)
from tests.test_harness_codex_worker import packet, write_result


@pytest.mark.parametrize('wait', [-1, 30001, 60000, True, 0.5, '1'])
@pytest.mark.parametrize('operation', ['launch', 'cancel'])
def test_invalid_wait_cannot_prepare_launch_or_cancel(tmp_path, monkeypatch, wait, operation):
    runner = TerminalCommandRunner(tmp_path / 'jobs', cwd=tmp_path, shell=sys.executable)
    calls = []
    for name in ('prepare', 'launch', 'cancel', '_read_status'):
        monkeypatch.setattr(runner, name, lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(ValueError, match='wait_ms'):
        runner.execute(command='print(1)' if operation == 'launch' else None,
                       command_id=None if operation == 'launch' else 'a' * 32,
                       cancel=operation == 'cancel', wait_ms=wait, max_preview_bytes=128)
    assert calls == []
    assert not list((tmp_path / 'jobs').glob('*/request.json'))


@pytest.mark.parametrize('wait', [0, 30000])
def test_valid_wait_boundaries_preserve_execution(tmp_path, monkeypatch, wait):
    runner = TerminalCommandRunner(tmp_path / 'jobs', cwd=tmp_path, shell=sys.executable)
    calls = []
    monkeypatch.setattr(runner, 'prepare', lambda *a, **kw: {'command_id': 'a' * 32})
    monkeypatch.setattr(runner, 'launch', lambda r: calls.append(r['command_id']))
    monkeypatch.setattr(runner, 'wait_result', lambda c, w: {'state': 'succeeded'})
    monkeypatch.setattr(runner, 'read_result', lambda c, n: {'command_id': c, 'state': 'succeeded'})
    result = runner.execute(command='print(1)', command_id=None, wait_ms=wait, max_preview_bytes=128)
    assert calls == ['a' * 32] and result['command_id'] == 'a' * 32
    assert result['wait_timed_out'] is False


@pytest.mark.parametrize('wait', [-1, 60000, True, 0.5])
def test_mcp_invalid_wait_has_no_packet_capsule_or_lease(packet, monkeypatch, wait):
    from solution_agent.harness import mcp_server as server
    calls = []
    monkeypatch.setattr(server, '_with_server', lambda r: r)
    monkeypatch.setattr(server, '_terminal_manager', lambda: calls.append('manager'))
    monkeypatch.setattr(server, '_require_activation', lambda: calls.append('activation'))
    monkeypatch.setattr(worker, 'worker_packet', lambda *a, **kw: calls.append('packet'))
    result = server.terminal_exec('TASK', worker_task=packet['text'], work_unit_id='prepare-1',
                                  timeout_seconds=60, wait_ms=wait)
    assert not result['ok'] and result['submission']['outcome'] == 'rejected'
    assert calls == []
    assert not Path(packet['control']).exists()
    assert not (Path(packet['root']) / 'state').exists()


def test_loaded_tool_schema_exposes_wait_bounds_and_safe_phase_default():
    from solution_agent.harness import mcp_server as server
    catalog = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in catalog if t.name == 'terminal_exec')
    fields = tool.input_schema['properties']
    assert fields['wait_ms']['minimum'] == 0
    assert fields['wait_ms']['maximum'] == 30000
    assert fields['worker_phase']['default'] == 'prepare_code'


@pytest.mark.parametrize('phase,status,exit_code', [('prepare_code', 'partial', 1), ('full', 'success', 0),
                                                ('execute_run', 'success', 0)])
def test_selected_phase_reaches_actual_worker_invocation(packet, monkeypatch, phase, status, exit_code):
    from solution_agent.orchestrator.prompt import build_worker_prompt
    observed = []
    monkeypatch.setattr(worker, 'worker_packet', lambda *a, **kw: {**packet, 'phase': phase})
    def prompt(task, text, *, output_dir, phase):
        observed.append(phase)
        return build_worker_prompt(task, text, output_dir=output_dir, phase=phase)
    def run(argv, **kw):
        write_result(packet, status)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker, 'build_worker_prompt', prompt)
    monkeypatch.setattr(worker.subprocess, 'run', run)
    assert worker.run_worker({**packet, 'phase': phase}) == exit_code
    assert observed == [phase]


@pytest.mark.parametrize('code,status,valid,action', [
    (0, 'partial', True, 'valid partial'),
    (0, 'failed', True, 'task failure'),
    (0, 'success', True, 'original acceptance'),
    (9, 'partial', True, 'process exited'),
    (0, None, False, 'invalid or missing'),
])
def test_result_layers_have_distinct_follow_up(code, status, valid, action):
    receipt = {'codex_exit_code': code, 'task_status': status, 'contract_valid': valid}
    original = dict(receipt)
    assert action in worker.worker_result_action(receipt)
    assert receipt == original


def test_pending_questions_are_not_silently_accepted():
    receipt = {'codex_exit_code': 0, 'contract_valid': True, 'task_status': 'success',
               'unanswered_questions': ['existing question']}
    assert 'unresolved questions' in worker.worker_result_action(receipt)


def test_mcp_partial_keeps_failed_exit_and_collects_original_artifacts(packet, monkeypatch):
    from solution_agent.harness import mcp_server as server
    command_dir = Path(__import__('os').environ['SOLUTION_AGENT_COMMAND_DIR'])
    for stream in ('stdout', 'stderr'):
        (command_dir / (stream + '.bin')).write_bytes(b'isolated evidence')
    def run(*a, **kw):
        write_result(packet, 'partial')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess, 'run', run)
    def execute(*a, **kw):
        assert worker.run_worker(packet) == 1
        return {'ok': True, 'state': 'failed', 'exit_code': 1, 'command_id': 'a' * 32,
                'command_dir': str(command_dir), 'task_id': 'TASK', 'work_unit_id': 'prepare-1',
                'writer_lease_id': kw['writer_lease_id'],
                'stdout': {'path': str(command_dir / 'stdout.bin')},
                'stderr': {'path': str(command_dir / 'stderr.bin')}}
    profile = SimpleNamespace(deployment_profile_name='FAN', default_terminal='main',
                              run_root=Path(packet['control']).parents[1],
                              slots={'main': SimpleNamespace(cwd=Path(packet['cwd']))})
    monkeypatch.setattr(server, '_with_server', lambda r: r)
    monkeypatch.setattr(server, '_require_activation', lambda: None)
    monkeypatch.setattr(server, '_solution_root', lambda: Path(packet['root']))
    monkeypatch.setattr(server, '_server_identity', lambda: {'release_id': 'test'})
    monkeypatch.setattr(server, '_terminal_manager', lambda: SimpleNamespace(profile=profile, exec=execute))
    result = server.terminal_exec('TASK', worker_task=packet['text'], work_unit_id='prepare-1', timeout_seconds=60)
    assert result['ok'], result
    assert result['state'] == 'failed' and result['exit_code'] == 1
    assert result['worker']['codex_exit_code'] == 0
    assert result['worker']['contract_valid'] and result['worker']['task_status'] == 'partial'
    assert 'valid partial' in result['next_action']
    assert {'worker_receipt', 'worker_result', 'worker_notes'} <= result['artifacts'].keys()


@pytest.mark.parametrize('fault', ['launch', 'wait_result', 'read_result'])
def test_post_prepare_error_retains_durable_identity(packet, tmp_path, monkeypatch, fault):
    jobs = tmp_path / 'jobs'
    runner = TerminalCommandRunner(jobs, cwd=Path(packet['cwd']), shell=sys.executable)
    monkeypatch.setattr(runner, 'launch', lambda r: None)
    monkeypatch.setattr(runner, 'wait_result', lambda *a: {'state': 'running'})
    def interrupted(*a, **kw):
        raise ConnectionError('lost observation')
    monkeypatch.setattr(runner, fault, interrupted)
    with pytest.raises(CommandObservationError) as caught:
        runner.execute(command='print(1)', command_id=None, wait_ms=0, max_preview_bytes=128,
                       task_id=packet['task_id'], work_unit_id=packet['work_unit_id'],
                       access_mode='writer', writer_lease_id='synthetic-lease', worker_packet=packet)
    requests = list(jobs.glob('*/request.json'))
    assert len(requests) == 1
    assert caught.value.command_id == requests[0].parent.name
    assert worker.submission(packet, jobs_root=jobs) == (None, caught.value.command_id)
    assert isinstance(caught.value.__cause__, ConnectionError)


def test_mcp_lost_launch_keeps_original_id_and_lookup_never_relaunches(packet, tmp_path, monkeypatch):
    from solution_agent.harness import mcp_server as server
    jobs = tmp_path / 'jobs'
    runner = TerminalCommandRunner(jobs, cwd=Path(packet['cwd']), shell=sys.executable)
    launches = []
    def lost(request):
        launches.append(request['command_id'])
        raise ConnectionError('launch response lost')
    monkeypatch.setattr(runner, 'launch', lost)
    def execute(*a, **kw):
        return runner.execute(**{k: v for k, v in kw.items() if k != 'runtime_release_id'})
    profile = SimpleNamespace(deployment_profile_name='FAN', default_terminal='main',
                              slots={'main': SimpleNamespace(cwd=Path(packet['cwd']))})
    monkeypatch.setattr(server, '_with_server', lambda r: r)
    monkeypatch.setattr(server, '_require_activation', lambda: None)
    monkeypatch.setattr(server, '_solution_root', lambda: Path(packet['root']))
    monkeypatch.setattr(server, '_server_identity', lambda: {'release_id': 'test'})
    monkeypatch.setattr(server, '_terminal_manager', lambda: SimpleNamespace(base=tmp_path, profile=profile, exec=execute))
    result = server.terminal_exec('TASK', worker_task=packet['text'], work_unit_id='prepare-1',
                                  timeout_seconds=60, wait_ms=0)
    assert result['submission']['outcome'] == 'unknown'
    assert result['command_id'] == launches[0]
    assert result['error']['code'] == 'command_outcome_unknown'
    # Restore observation only; a prepared job is not relaunched by lookup.
    monkeypatch.setattr(runner, 'wait_result', lambda *a: {'state': 'prepared'})
    monkeypatch.setattr(runner, 'read_result', lambda c, n: {'ok': True, 'state': 'prepared',
                         'task_id': 'TASK', 'work_unit_id': 'prepare-1', 'command_id': c})
    restored = server.terminal_exec('TASK', worker_task=packet['text'], work_unit_id='prepare-1',
                                    timeout_seconds=60, wait_ms=0, recover_worker=True)
    assert restored['command_id'] == result['command_id']
    assert launches == [result['command_id']]
    assert len(list(jobs.glob('*/request.json'))) == 1

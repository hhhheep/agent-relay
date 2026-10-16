"""Synthetic startup-budget tests: no SSH server, real credentials or live state."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from solution_agent.harness import ssh_admission as admission


class Clock:
    def __init__(self): self.now=1000.;self.elapsed=0.;self.waits=[]
    def advance(self,seconds): self.now+=seconds;self.elapsed+=seconds
    def sleep(self,seconds): self.waits.append(seconds);self.advance(seconds)


def target(host='worker.example.invalid',ip='192.0.2.1',user='worker',port=22,command='runtime-A',extra=''):
    return admission.target_from_config(f'hostname {host}\nport {port}\nuser {user}\nidentityfile /synthetic/key\n'+extra,[ip],command)


def budget(path,clock):
    return admission.Budget(path,clock=lambda:clock.now,monotonic=lambda:clock.elapsed,sleep=clock.sleep,jitter=lambda:0)


def test_ten_normal_clients_queue_without_an_exclusive_connection_owner(tmp_path):
    clock=Clock();gate=budget(tmp_path/'b.db',clock);times=[]
    for _ in range(10):
        token,started=gate.admit(target());times.append(started)
        # Leave sessions live: admission is not held until finish().
    assert times==[1000.]*3+[1010.]*3+[1020.]*3+[1030.]
    assert all(sum(t-10<x<=t for x in times)<=3 for t in set(times))


def test_effective_aliases_ip_names_and_ports_cannot_bypass_or_cross_block(tmp_path):
    clock=Clock();gate=budget(tmp_path/'b.db',clock)
    a=target(host='WORKER.EXAMPLE.INVALID.')
    assert a==target()
    for _ in range(3):gate.admit(a)
    with pytest.raises(admission.AdmissionError,match='budget'):
        gate.admit(target(host='another-name.example.invalid'),max_wait=0)
    gate.admit(target(host='other.example.invalid',ip='192.0.2.2'),max_wait=0)
    gate.admit(target(port=23),max_wait=0)


def test_cross_process_atomic_budget_and_crashed_reservation_expiry(tmp_path):
    path=tmp_path/'b.db'
    code="""import sys;sys.path.insert(0,sys.argv[1]);from solution_agent.harness import ssh_admission as a
b=a.Budget(sys.argv[2],clock=lambda:1000.)
try:b.admit(a.Target(('endpoint',),'credential','trust','route'),max_wait=0);print('admitted')
except a.AdmissionError:print('blocked')
"""
    def child(_):
        return subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]),str(path)],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
    with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(child,range(10)))
    assert results.count('admitted')==3 and results.count('blocked')==7
    clock=Clock();clock.advance(11)
    budget(path,clock).admit(admission.Target(('endpoint',),'credential','trust','route'),max_wait=0)


def test_failure_backoff_persists_after_restart_and_is_bounded(tmp_path):
    clock=Clock();path=tmp_path/'b.db';waits=[]
    for _ in range(7):
        gate=budget(path,clock);token,started=gate.admit(target())
        gate.finish(target(),token,started,'network')
        with sqlite3.connect(path) as db:until=db.execute('SELECT max(until) FROM failures').fetchone()[0]
        waits.append(until-clock.now)
        with pytest.raises(admission.AdmissionError):budget(path,clock).admit(target(),max_wait=1)
        clock.advance(max(10,until-clock.now))
    assert waits==[2,4,300,2,4,300,2]
    assert not clock.waits  # Too-small admission budgets stop instead of sleeping forever.


@pytest.mark.parametrize('kind',['authentication_diagnostic','host_key_diagnostic'])
def test_diagnostic_cooldown_stops_without_retry_and_expires(tmp_path,kind):
    clock=Clock();path=tmp_path/'b.db';gate=budget(path,clock)
    token,started=gate.admit(target());gate.finish(target(),token,started,kind)
    with pytest.raises(admission.AdmissionError,match='stop without retry'):
        budget(path,clock).admit(target())
    assert clock.waits==[]
    if kind=='authentication_diagnostic':
        gate.admit(target(user='independent-credential'),max_wait=0)
    else:
        gate.admit(target(host='independent-trust-name.example.invalid'),max_wait=0)
    clock.advance(300)
    budget(path,clock).admit(target(),max_wait=0)


def test_unknown_error_is_scoped_and_old_success_cannot_clear_new_failure(tmp_path):
    clock=Clock();path=tmp_path/'b.db';gate=budget(path,clock);a=target()
    old,old_start=gate.admit(a);new,new_start=gate.admit(a)
    gate.finish(a,new,new_start,'unknown_transport')
    gate.finish(a,old,old_start,'success')
    with pytest.raises(admission.AdmissionError):gate.admit(a,max_wait=0)
    gate.admit(target(command='independent-runtime'),max_wait=0)
    with sqlite3.connect(path) as db:assert db.execute('SELECT max(count) FROM failures').fetchone()[0]==1
    gate.finish(a,new,new_start,'unknown_transport')
    with sqlite3.connect(path) as db:assert db.execute('SELECT max(count) FROM failures').fetchone()[0]==1


def test_established_session_failure_is_not_a_startup_burst(tmp_path):
    clock=Clock();path=tmp_path/'b.db';gate=budget(path,clock)
    token,started=gate.admit(target());clock.advance(61)
    gate.finish(target(),token,started,'network')
    with sqlite3.connect(path) as db:assert db.execute('SELECT count(*) FROM failures').fetchone()[0]==0


def test_clock_reversal_and_crash_do_not_create_permanent_owner_lock(tmp_path):
    clock=Clock();path=tmp_path/'b.db';gate=budget(path,clock)
    token,started=gate.admit(target());gate.finish(target(),token,started,'network')
    clock.now-=100
    gate.admit(target())
    assert max(clock.waits)<=31
    try:
        with gate._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO attempts VALUES (?,?,?,?)',('rollback','token',clock.now,'admitted'))
            raise RuntimeError('simulated process interruption')
    except RuntimeError:pass
    with sqlite3.connect(path) as db:assert db.execute("SELECT count(*) FROM attempts WHERE endpoint='rollback'").fetchone()[0]==0


def test_corrupt_ledger_fails_closed_without_resetting_it(tmp_path):
    path=tmp_path/'b.db';path.write_bytes(b'invalid SQLite evidence')
    with pytest.raises(sqlite3.DatabaseError):budget(path,Clock()).admit(target())
    assert path.read_bytes()==b'invalid SQLite evidence'


@pytest.mark.parametrize('code,text,expected',[(0,b'permission denied (publickey)','success'),
    (1,b'permission denied (publickey)','remote_exit'),(255,b'unspecified error','unknown_transport'),
    (255,b'Permission denied','unknown_transport'),(255,b'user@host: Permission denied (publickey).','authentication_diagnostic'),
    (255,b'Host key verification failed.','host_key_diagnostic'),(255,b'Connection timed out','network')])
def test_exit_255_is_not_assumed_to_be_authentication(code,text,expected):
    assert admission.classify(code,text)==expected


@pytest.mark.parametrize('extra',['proxyjump jump.example.invalid\n','proxycommand opaque-command\n'])
def test_opaque_proxy_routes_do_not_silently_bypass_target_verification(extra):
    with pytest.raises(ValueError,match='not proxy routes'):target(extra=extra)


def test_effective_config_and_address_resolution_are_local_stubbed_inputs(monkeypatch):
    calls=[]
    def config(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0,stdout='hostname worker.example.invalid\nport 22\nuser worker\n')
    monkeypatch.setattr(admission.subprocess,'run',config)
    monkeypatch.setattr(admission.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('192.0.2.1',22))])
    a=admission.resolve_target('ALIAS-A','same-command');b=admission.resolve_target('ALIAS-B','same-command')
    assert a==b and all(argv[1]=='-G' for argv in calls)
    monkeypatch.setattr(admission.socket,'getaddrinfo',lambda *a,**k:(_ for _ in ()).throw(OSError('synthetic DNS failure')))
    with pytest.raises(RuntimeError,match='resolution unavailable'):admission.resolve_target('ALIAS','command')


def test_parallel_synthetic_stdio_clients_and_multiple_requests_share_only_start_budget(tmp_path,monkeypatch):
    original=admission.subprocess.Popen;launches=[]
    script="""import pathlib,sys,time
root=pathlib.Path(sys.argv[1]);(root/sys.argv[2]).write_text('ready')
deadline=time.monotonic()+5
while len(list(root.iterdir()))<2:
 if time.monotonic()>deadline:raise SystemExit(9)
 time.sleep(.01)
for line in sys.stdin.buffer:sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'diagnostic only\\n')
"""
    ready=tmp_path/'ready';ready.mkdir()
    def popen(argv,**kwargs):
        launches.append(argv)
        return original([sys.executable,'-c',script,str(ready),argv[1]],**kwargs)
    monkeypatch.setattr(admission.subprocess,'Popen',popen)
    gate=admission.Budget(tmp_path/'b.db')
    def client(index):
        request=(json.dumps({'client':index,'request':1})+'\n'+json.dumps({'client':index,'request':2})+'\n').encode()
        source,out,err=tmp_path/f'in-{index}',tmp_path/f'out-{index}',tmp_path/f'err-{index}'
        source.write_bytes(request)
        token,started=gate.admit(target())
        with source.open('rb') as stdin,out.open('wb') as stdout,err.open('wb') as stderr:
            code,tail=admission.run_transport(['synthetic-ssh',str(index)],stdin=stdin,stdout=stdout,stderr=stderr)
        gate.finish(target(),token,started,admission.classify(code,tail))
        assert code==0 and out.read_bytes()==request and err.read_bytes()==b'diagnostic only\n'
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(client,range(2)))
    assert len(launches)==2
    with sqlite3.connect(tmp_path/'b.db') as db:
        assert db.execute('SELECT count(DISTINCT token) FROM attempts').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM failures').fetchone()[0]==0


@pytest.fixture
def shared_guard(tmp_path, monkeypatch):
    from solution_agent.orchestrator import adapters
    from solution_agent.harness import profile_deploy
    clock = Clock()
    gate = budget(tmp_path/'shared.db', clock)
    resolved = []
    def resolve(alias, command, **options):
        resolved.append((alias, command, options))
        return target(command=command)
    monkeypatch.setattr(admission, 'resolve_target', resolve)
    monkeypatch.setattr(admission, 'Budget', lambda path: gate)
    monkeypatch.setattr(admission, 'state_path', lambda: tmp_path/'shared.db')
    monkeypatch.setattr(adapters, '_log_remote_call', lambda *a, **k: None)
    monkeypatch.setattr(adapters, 'command_exists', lambda name: True)
    monkeypatch.setenv('SOLUTION_AGENT_SSH_CONFIG', str(tmp_path/'synthetic-ssh-config'))
    return clock, gate, resolved, adapters, profile_deploy


def test_existing_maintenance_and_legacy_entrances_share_start_budget(shared_guard, tmp_path, monkeypatch):
    clock, gate, resolved, adapters, deploy = shared_guard
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{}', stderr='')
    monkeypatch.setattr(adapters, 'run_subprocess', run)
    monkeypatch.setattr(deploy, '_run', run)
    assert adapters.run_ssh('ALIAS-A', 'diagnose')['returncode'] == 0
    assert adapters.scp_to_remote(tmp_path/'archive', 'ALIAS-B', '/synthetic/in')['returncode'] == 0
    assert adapters.scp_from_remote('ALIAS-C', '/synthetic/out', tmp_path/'received')['returncode'] == 0
    profile = SimpleNamespace(host='ALIAS-D', python_bootstrap='python3')
    assert deploy._ssh_python(profile, 'print(1)', [])['transport_returncode'] == 0
    # Different aliases/operations share the effective endpoint ledger.
    assert clock.waits == [10.0] and len(calls) == 4
    assert resolved[0][2]['ssh_prefix'][1:] == ['-F', str(tmp_path/'synthetic-ssh-config')]
    assert resolved[0][2]['options'] == admission.SSH_COMMON_OPTIONS
    assert all(call[0] in ('ssh', 'scp') and 'ConnectionAttempts=1' in call for call in calls)


def test_maintenance_failure_circuit_refuses_without_any_fallback(shared_guard, monkeypatch):
    clock, gate, resolved, adapters, deploy = shared_guard
    calls = []
    def fail(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=255, stdout='', stderr='Connection refused')
    monkeypatch.setattr(adapters, 'run_subprocess', fail)
    monkeypatch.setattr(deploy, '_run', fail)
    for index in range(3):
        assert adapters.run_ssh('ALIAS', 'diagnose')['returncode'] == 255
    assert len(calls) == 3
    before = list(clock.waits)
    for operation in (lambda: adapters.run_ssh('ALIAS', 'different-command'),
        lambda: adapters.scp_to_remote(Path('synthetic'), 'ALIAS', '/synthetic'),
        lambda: deploy._guarded_run('ALIAS', ['scp', 'synthetic'])):
        with pytest.raises(admission.AdmissionError, match='stop without retry'):
            operation()
    assert len(calls) == 3 and clock.waits == before
    clock.advance(300)
    assert adapters.run_ssh('ALIAS', 'diagnose')['returncode'] == 255
    assert len(calls) == 4


def test_stream_start_preserves_process_and_does_not_consume_output(shared_guard, monkeypatch):
    import threading
    clock, gate, resolved, adapters, deploy = shared_guard
    exited = threading.Event()
    process = SimpleNamespace(stdout=object(), wait=lambda: (exited.set() or 0))
    calls = []
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return process
    monkeypatch.setattr(adapters.subprocess, 'Popen', spawn)
    assert adapters.open_ssh_stream('ALIAS', 'diagnose') is process
    assert exited.wait(2) and len(calls) == 1
    assert calls[0][1]['stderr'] == subprocess.STDOUT


def test_timeout_records_failure_once_and_rethrows_original(shared_guard):
    clock, gate, resolved, adapters, deploy = shared_guard
    original = subprocess.TimeoutExpired('synthetic', 1, output=b'partial', stderr=b'diagnostic')
    calls = []
    def timeout():
        calls.append(1)
        raise original
    with pytest.raises(subprocess.TimeoutExpired) as error:
        admission.guarded_run('ALIAS', 'diagnose', timeout)
    assert error.value is original and calls == [1]
    with pytest.raises(admission.AdmissionError):
        gate.admit(target(command='diagnose'), max_wait=0)


def test_effective_config_preserves_maintenance_config_selection(monkeypatch):
    calls = []
    def config(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='hostname worker.example.invalid\nport 22\nuser worker\n')
    monkeypatch.setattr(admission.subprocess, 'run', config)
    monkeypatch.setattr(admission.socket, 'getaddrinfo', lambda *a, **k: [(2,1,6,'',('192.0.2.1',22))])
    admission.resolve_target('ALIAS', 'diagnose', ssh_prefix=['ssh', '-F', 'synthetic-config'], options=[])
    assert calls == [['ssh', '-F', 'synthetic-config', '-G', 'ALIAS']]

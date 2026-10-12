from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
import pytest
from solution_agent.harness.help_queue import HelpQueue
from solution_agent.harness import worker_help as help
from tests.test_harness_codex_worker import packet, write_result
from solution_agent.harness import codex_worker as worker


def binding(task='task',worker_id='worker',chat='chat'):
    return dict(task_id=task,work_unit_id='unit',worker_id=worker_id,origin_chat_id=chat)


def queue(path,b=None):
    q=HelpQueue(path); q.bind(b or binding()); return q


def test_offline_restart_duplicate_and_answer(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path)
    delivery=q.ask('q','same problem','passes check')
    q.close()  # Local manager absent; remote process also restarts.
    q=queue(path)
    assert q.ask('q','same problem','passes check')==delivery
    assert len(q.pending())==1
    q.accept(delivery,'chat'); q.close()
    q=queue(path)
    assert q.pending()[0]['state']=='accepted' # Receipt is not an answer.
    with pytest.raises(ValueError,match='wrong chat'): q.reply(delivery,'other','answer')
    q.reply(delivery,'chat','answer'); q.close()
    q=queue(path); q.reply(delivery,'chat','answer')
    assert q.pending()==[] and q.status('q')['answer']=='answer'
    with pytest.raises(ValueError,match='reply conflict'): q.reply(delivery,'chat','different')
    q.close()


def test_equal_questions_different_workers_tasks_and_chat(tmp_path):
    ids=[]
    for i,b in enumerate([binding(),binding(worker_id='worker2'),binding(task='task2'),binding(chat='chat2')]):
        q=queue(tmp_path/f'{i}.sqlite',b)
        ids.append(q.ask('same-id','same problem','same acceptance')); q.close()
    assert len(set(ids))==4


def test_capacity_and_conflicts(tmp_path):
    q=queue(tmp_path/'q.sqlite')
    for i in range(128): q.ask(str(i),'problem','check')
    with pytest.raises(ValueError,match='queue full'): q.ask('129','problem','check')
    assert len(q.pending())==128
    with pytest.raises(ValueError,match='content conflict'): q.ask('0','changed','check')
    with pytest.raises(ValueError,match='binding conflict'): q.bind(binding(chat='wrong'))
    first=q.pending()[0]; q.reply(first['delivery_id'],'chat','answer')
    q.ask('129','problem','check'); assert len(q.pending())==128
    q.close()


def test_worker_forgets_help_and_exits(tmp_path):
    q=queue(tmp_path/'q.sqlite'); q.progress(now=10)
    assert q.observe(now=19,stale_seconds=10) is None
    first=q.observe(now=20,stale_seconds=10)
    assert q.observe(now=21,stale_seconds=10)==first
    q.progress(now=25)
    assert q.observe(now=35,stale_seconds=10)!=first
    q.observe(exit_code=0,contract_valid=False)
    assert json.loads(q.pending()[-1]['body'])['kind']=='worker_incomplete'
    assert q.observe(now=9999,stale_seconds=10) is None
    q.close()


def test_real_cli_leaves_durable_question_without_manager(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path); q.close()
    script=Path(help.__file__).with_name('help_queue.py')
    args=[sys.executable,str(script),'--mailbox',str(path),'ask','--request-id','q','--problem','缺少資料','--acceptance','資料可讀取']
    result=subprocess.run(args,capture_output=True,check=True)
    receipt=json.loads(result.stdout)
    assert receipt['manager_received'] is False
    assert json.loads(subprocess.run(args,capture_output=True,check=True).stdout)['delivery_id']==receipt['delivery_id']
    q=queue(path); assert len(q.pending())==1; q.close()


def test_real_process_stall_without_model_help(tmp_path):
    route={'binding':binding(),'mailbox':str(tmp_path/'q.sqlite'),'stale_seconds':1}
    help.initialize(route)
    # An actual process accepts input but never calls the help command.
    code=help.run_with_watch([sys.executable,'-c','import sys,time; sys.stdin.read(); time.sleep(1.5)'],'prompt',str(tmp_path),None,route)
    help.finished(route,code,False)
    q=queue(tmp_path/'q.sqlite')
    kinds=[json.loads(row['body'])['kind'] for row in q.pending()]
    assert kinds==['no_progress_observed','worker_incomplete']; q.close()


def enable(packet):
    root=Path(packet['root']); (root/'config').mkdir()
    key=worker._digest({'task_id':packet['task_id'],'work_unit_id':packet['work_unit_id']})
    b=dict(task_id=packet['task_id'],work_unit_id=packet['work_unit_id'],worker_id='test-worker',origin_chat_id='test-chat')
    (root/'config'/'experimental-worker-routing.json').write_text(json.dumps({'enabled':True,'bindings':{key:b}}))
    return worker.worker_packet(root,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])


def test_existing_worker_hook_and_missing_help_fallback(packet,monkeypatch):
    routed=enable(packet)
    def run(argv,prompt,cwd,env,route):
        assert 'queued_locally' in prompt and '--request-id' in prompt
        return 0 # Model forgot to ask and produced no valid result.
    monkeypatch.setattr(help,'run_with_watch',run)
    assert worker.run_worker(routed)==1
    q=HelpQueue(routed['help_route']['mailbox'])
    assert len(q.pending())==1
    assert json.loads(q.pending()[0]['body'])['kind']=='worker_incomplete'
    assert worker.submission(routed)==(None,'a'*32)
    q.close()


def test_successful_worker_not_reported_as_failed(packet,monkeypatch):
    routed=enable(packet)
    def run(*args): write_result(routed); return 0
    monkeypatch.setattr(help,'run_with_watch',run)
    assert worker.run_worker(routed)==0
    q=HelpQueue(routed['help_route']['mailbox']); assert q.pending()==[]; q.close()


def test_changed_route_refuses_launch(packet):
    routed=enable(packet)
    routed['help_route']['binding']['origin_chat_id']='injected'
    with pytest.raises(ValueError,match='profile changed'): worker.run_worker(routed)
    assert not Path(routed['control']).exists()


def test_worker_claims_success_with_unanswered_question(packet,monkeypatch):
    routed=enable(packet)
    def run(*args):
        q=HelpQueue(routed['help_route']['mailbox'])
        q.ask('blocked','missing value','manager supplies value'); q.close()
        write_result(routed); return 0
    monkeypatch.setattr(help,'run_with_watch',run)
    assert worker.run_worker(routed)==1
    receipt=json.loads((Path(routed['control'])/'receipt.json').read_text())
    assert receipt['unanswered_questions']==1


def test_supervisor_reports_wrapper_death_and_preserves_job_receipt(tmp_path,monkeypatch):
    from solution_agent.harness.terminal_commands import finalize
    route={'binding':binding(),'mailbox':str(tmp_path/'q.sqlite'),'stale_seconds':10}
    help.initialize(route)
    request={'command_id':'command','command_sha256':'hash','task_id':'task','work_unit_id':'unit'}
    for name in ('request.json','status.json'): (tmp_path/name).write_text(json.dumps(request))
    (tmp_path/'help-watch.json').write_text(json.dumps({**request,'route':route,'worker_receipt':str(tmp_path/'missing-worker-receipt.json')}))
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    child.kill(); child.wait(timeout=5)
    result=finalize(tmp_path,child.returncode,'start',forced_state='cancelled')
    assert result['state']=='cancelled'
    q=HelpQueue(route['mailbox'])
    assert json.loads(q.pending()[0]['body'])['kind']=='worker_incomplete'
    q.close()
    finalize(tmp_path,child.returncode,'start',forced_state='cancelled')
    q=HelpQueue(route['mailbox']); assert len(q.pending())==1; q.close()
    def broken(*args): raise OSError('sidecar unavailable')
    monkeypatch.setattr(help,'finalize_watch',broken)
    assert finalize(tmp_path,child.returncode,'start',forced_state='cancelled')['state']=='cancelled'


def test_recovery_after_actual_supervisor_death_reports_once_without_retry(tmp_path):
    from solution_agent.harness.terminal_commands import TerminalCommandRunner
    runner=TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
    request=runner.prepare('pass',task_id='task',work_unit_id='unit')
    directory=tmp_path/'jobs'/request['command_id']
    request['execution_guardian']=False
    (directory/'request.json').write_text(json.dumps(request))
    initial=json.loads((directory/'status.json').read_text())
    (directory/'status.json').write_text(json.dumps({**initial,'execution_guardian':False}))
    route={'binding':binding(),'mailbox':str(tmp_path/'q.sqlite'),'stale_seconds':10}
    help.initialize(route)
    (directory/'help-watch.json').write_text(json.dumps({**request,'route':route,'worker_receipt':str(tmp_path/'absent.json')}))
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    try:
        status=json.loads((directory/'status.json').read_text())
        status.update(state='running',supervisor_pid=child.pid)
        (directory/'status.json').write_text(json.dumps(status))
        assert runner.reconcile(request['command_id'])['state']=='running'
        child.kill();child.wait(timeout=5)
        assert runner.reconcile(request['command_id'])['state']=='lost'
        # A newly created controller represents recovery after manager restart.
        restarted=TerminalCommandRunner(tmp_path/'jobs',cwd=tmp_path,shell=sys.executable)
        assert restarted.reconcile(request['command_id'])['state']=='lost'
        q=HelpQueue(route['mailbox']);pending=q.pending()
        assert len(pending)==1
        observed=json.loads(pending[0]['body'])
        assert observed['worker_exit_unknown'] is True
        assert observed['automatic_retry_allowed'] is False
        assert observed['command_id']==request['command_id'];q.close()
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)

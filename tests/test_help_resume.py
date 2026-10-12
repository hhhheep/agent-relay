import json
import pytest
from solution_agent.harness.help_resume import ResumeLedger
from tests.test_worker_help_queue import binding
from tests.test_worker_help_queue import queue
from solution_agent.harness.help_bridge import deliver_reply


def plan(ledger,suffix=''):
    question={'request_id':'q'+suffix,'delivery_id':'delivery'+suffix,'state':'answered',
              'body':json.dumps({'problem':'missing data','acceptance':'read value'}),
              'answer':json.dumps({'kind':'awaiting_verification','request_id':'delivery'+suffix,'evidence':{'text':'proposal'}})}
    return ledger.plan(binding(),question,python_path='/runtime/python',help_script='/runtime/help.py',mailbox='/work/mailbox.sqlite',previous_command_id='original')


def original(state='succeeded'):
    return {'command_id':'original','task_id':'task','state':state,'writer_lease_released':True}


def response(ledger,identity,state='running'):
    row=ledger.inspect(identity); args=json.loads(row['body'])['request']
    return {'task_id':args['task_id'],'work_unit_id':args['work_unit_id'],'command_id':'resumed','state':state}


def test_durable_intent_lost_ack_and_reconciliation(tmp_path):
    p=tmp_path/'resume.sqlite'; ledger=ResumeLedger(p); identity=plan(ledger); calls=[]
    def lost(args): calls.append(args); raise TimeoutError('accepted but lost acknowledgment')
    with pytest.raises(TimeoutError): ledger.dispatch(identity,original(),lost)
    ledger.close(); ledger=ResumeLedger(p)
    assert plan(ledger)==identity
    with pytest.raises(ValueError,match='reconciliation'): ledger.dispatch(identity,original(),lost)
    ledger.reconcile(identity,response(ledger,identity))
    assert ledger.dispatch(identity,original(),lost)['command_id']=='resumed'
    assert len(calls)==1
    ledger.reconcile(identity,response(ledger,identity,'succeeded'))
    assert ledger.inspect(identity)['state']=='finished'; ledger.close()


def test_original_running_or_unreleased_never_dispatches(tmp_path):
    ledger=ResumeLedger(tmp_path/'q.sqlite'); identity=plan(ledger)
    def unexpected(args): raise AssertionError('must not submit')
    assert ledger.dispatch(identity,original('running'),unexpected)['state']=='waiting_original_command'
    with pytest.raises(ValueError,match='released'): ledger.dispatch(identity,{**original(),'writer_lease_released':False},unexpected)
    with pytest.raises(ValueError,match='wrong original'): ledger.dispatch(identity,{**original(),'task_id':'other'},unexpected)
    ledger.close()


def test_only_one_running_continuation_per_task(tmp_path):
    ledger=ResumeLedger(tmp_path/'q.sqlite'); one=plan(ledger); two=plan(ledger,'2')
    ledger.dispatch(one,original(),lambda args:response(ledger,one))
    assert ledger.dispatch(two,original(),lambda args:None)['state']=='waiting_task_continuation'
    with pytest.raises(ValueError,match='wrong continuation'): ledger.reconcile(one,{**response(ledger,one),'task_id':'other'})
    ledger.close()


def test_reply_replay_repairs_missing_continuation_without_duplicate(tmp_path):
    mailbox=tmp_path/'mailbox.sqlite'; q=queue(mailbox); delivery=q.ask('q','problem','check'); q.close()
    receipt={'binding':binding(),'delivery_id':delivery,'answer':json.dumps({'kind':'awaiting_verification','request_id':delivery})}
    deliver_reply(mailbox,binding(),receipt) # process stopped before scheduling
    ledger=ResumeLedger(tmp_path/'ledger.sqlite')
    # Manager plan is for remote paths; local mailbox is a Windows test fixture.
    class RemotePlan:
        def plan(self,b,question,**kwargs):
            kwargs['mailbox']='/remote/mailbox.sqlite'
            return ledger.plan(b,question,**kwargs)
    context={'python_path':'/runtime/python','help_script':'/runtime/help.py','previous_command_id':'original'}
    first=deliver_reply(mailbox,binding(),receipt,resume_ledger=RemotePlan(),resume_context=context)
    second=deliver_reply(mailbox,binding(),receipt,resume_ledger=RemotePlan(),resume_context=context)
    assert first==second
    assert ledger.db.execute('SELECT count(*) FROM continuations').fetchone()[0]==1
    request=json.loads(ledger.inspect(first)['body'])['request']
    assert '--answer-sha256' in request['worker_task']
    assert '/runtime/python' in request['worker_task']; ledger.close()


def test_retry_requires_original_parent_and_exact_review_and_is_idempotent(tmp_path):
    import hashlib
    from solution_agent.harness.help_queue import canonical
    ledger=ResumeLedger(tmp_path/'ledger.sqlite'); parent=plan(ledger)
    ledger.dispatch(parent,original(),lambda args:response(ledger,parent,'succeeded'))
    question={'request_id':'q','delivery_id':'delivery','state':'answered',
              'body':json.dumps({'problem':'missing data','acceptance':'read value'}),
              'answer':json.dumps({'kind':'awaiting_verification','request_id':'delivery','evidence':{'text':'proposal'}})}
    review={'answer':{'kind':'retry_allowed','request_id':hashlib.sha256(canonical([binding(),'verify-'+parent[:24]]).encode()).hexdigest(),
                      'incident':'delivery'},'previous_verification':{'evidence':'insufficient'}}
    kwargs=dict(python_path='/runtime/python',help_script='/runtime/help.py',mailbox='/work/mailbox.sqlite',
                previous_command_id='resumed',parent_id=parent,retry_review=review)
    retry=ledger.plan(binding(),question,**kwargs)
    assert retry==ledger.plan(binding(),question,**kwargs) and retry!=parent
    with pytest.raises(ValueError,match='invalid or exhausted'):
        ledger.plan(binding(),question,**{**kwargs,'previous_command_id':'wrong'})
    with pytest.raises(ValueError,match='invalid or exhausted'):
        ledger.plan(binding(),question,**{**kwargs,'retry_review':{'answer':{**review['answer'],'incident':'other'}}})
    with pytest.raises(ValueError,match='invalid or exhausted'):
        ledger.plan(binding(),question,**{**kwargs,'parent_id':retry})
    assert ledger.db.execute('SELECT count(*) FROM continuations').fetchone()[0]==2
    ledger.close()


def test_opt_in_capture_plan_uses_declared_runner_without_manual_evidence(tmp_path):
    ledger=ResumeLedger(tmp_path/'ledger.sqlite')
    question={'request_id':'q','delivery_id':'delivery','state':'answered','body':json.dumps({'acceptance':'check'}),
              'answer':json.dumps({'kind':'awaiting_verification','request_id':'delivery'})}
    kwargs=dict(python_path='/runtime/python',help_script='/runtime/help.py',mailbox='/work/mailbox.sqlite',
                previous_command_id='original',verification_runner='/runtime/verification_runner.py',verification_cwd='/work')
    identity=ledger.plan(binding(),question,**kwargs)
    prompt=json.loads(ledger.inspect(identity)['body'])['request']['worker_task']
    assert '--argv-json <JSON-array>' in prompt and '--evidence <actual-check-result>' not in prompt
    assert '--cwd /work' in prompt
    assert ledger.plan(binding(),question,**kwargs)==identity
    with pytest.raises(ValueError,match='conflict'):
        ledger.plan(binding(),question,**{**kwargs,'verification_cwd':'/another'})
    ledger.close()

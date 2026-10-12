import hashlib
import json
import pytest
from solution_agent.harness.help_queue import HelpQueue
from tests.test_worker_help_queue import binding,queue


@pytest.mark.parametrize('passed,outcome',[(True,'closed'),(False,'retry_allowed'),(False,'human_required')])
def test_answer_is_not_resolution_and_review_survives_restart(tmp_path,passed,outcome):
    path=tmp_path/'q.sqlite'; q=queue(path)
    original=q.ask('question','problem','actual check')
    answer=json.dumps({'v':1,'kind':'awaiting_verification','request_id':original,'incident':original,'evidence':{'text':'proposal'}})
    q.reply(original,'chat',answer)
    assert q.unresolved_questions()==1
    digest=hashlib.sha256(answer.encode()).hexdigest()
    with pytest.raises(ValueError,match='stale'): q.verify('question','verify','wrong',True,'actual evidence')
    delivery=q.verify('question','verify',digest,passed,'actual test output')
    assert q.verify('question','verify',digest,passed,'actual test output')==delivery
    q.close(); q=queue(path)
    assert q.unresolved_questions()==1
    invalid=json.dumps({'kind':'closed','request_id':'other','incident':original})
    with pytest.raises(ValueError,match='invalid verification'): q.reply(delivery,'chat',invalid)
    assert q.status('verify')['state']=='pending'
    q.reply(delivery,'chat',json.dumps({'kind':outcome,'request_id':delivery,'incident':original}))
    q.close(); q=queue(path)
    assert q.status('question')['resolution']['state']==outcome
    assert q.unresolved_questions()==(0 if passed else 1)
    q.close()


def test_ack_or_unbound_answer_cannot_be_verified(tmp_path):
    q=queue(tmp_path/'q.sqlite'); d=q.ask('question','problem','check')
    q.reply(d,'chat',json.dumps({'kind':'model_received','request_id':d}))
    h=q.status('question')['resolution']['answer_sha256']
    with pytest.raises(ValueError,match='bound proposal'): q.verify('question','v',h,True,'evidence')
    assert q.unresolved_questions()==1; q.close()


def test_manager_escalation_is_unresolved_and_does_not_spawn_verifier(tmp_path):
    from solution_agent.harness.help_bridge import deliver_reply
    path=tmp_path/'q.sqlite'; q=queue(path); d=q.ask('q','problem','check'); q.close()
    class NoResume:
        def plan(self,*args,**kwargs): raise AssertionError('human decision cannot launch a worker')
    deliver_reply(path,binding(),{'binding':binding(),'delivery_id':d,'answer':json.dumps({'kind':'human_required','request_id':d})},resume_ledger=NoResume())
    q=queue(path)
    assert q.status('q')['resolution']['state']=='human_required'
    assert q.unresolved_questions()==1; q.close()

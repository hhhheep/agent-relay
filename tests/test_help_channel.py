import json
import pytest
from tests.test_worker_help_queue import queue,binding
from solution_agent.harness.help_channel import MailboxChannel


def test_offline_restart_resend_ack_and_answer(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path); delivery=q.ask('q','problem','check'); q.close()
    c=MailboxChannel(path,binding(),'manager'); sent=[]
    def offline(*args): raise ConnectionError('local off')
    with pytest.raises(ConnectionError): c.send_pending(offline,now=0)
    assert c.q.status('q')['state']=='pending'
    assert c.send_pending(lambda *args:sent.append(args),now=0)==1
    c.close(); c=MailboxChannel(path,binding(),'manager')
    assert c.send_pending(lambda *args:sent.append(args),now=10)==0
    assert c.send_pending(lambda *args:sent.append(args),now=31)==1
    assert sent[0]==sent[1]
    ack={'kind':'help_accepted','delivery_id':delivery,'origin_chat_id':'chat','durably_received':True}
    msg=lambda n,b,source='manager':{'event_id':n,'from_agent_id':source,'body':json.dumps(b)}
    assert c.receive([msg(1,ack,'wrong'),msg(2,ack)])==1
    assert c.q.status('q')['state']=='accepted'
    assert c.send_pending(offline,now=99)==0
    answer={'binding':binding(),'delivery_id':delivery,'answer':json.dumps({'kind':'awaiting_verification','request_id':delivery})}
    assert c.receive([msg(3,answer)])==1
    c.close(); c=MailboxChannel(path,binding(),'manager')
    assert c.cursor()==3 and c.q.unresolved_questions()==1
    assert c.receive([msg(3,answer),msg(4,answer)])==1
    assert c.q.status('q')['state']=='answered'; c.close()


def test_refuse_manager_rebinding(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path); q.close()
    c=MailboxChannel(path,binding(),'manager'); c.close()
    with pytest.raises(ValueError,match='rebind'): MailboxChannel(path,binding(),'other')


def test_answer_saved_confirmation_survives_crash_and_stops_retries(tmp_path):
    import hashlib
    path=tmp_path/'q.sqlite';q=queue(path);delivery=q.ask('q','problem','check');q.close()
    answer=json.dumps({'kind':'awaiting_verification','request_id':delivery})
    message={'event_id':1,'from_agent_id':'manager','body':json.dumps({'binding':binding(),'delivery_id':delivery,'answer':answer})}
    c=MailboxChannel(path,binding(),'manager');assert c.receive([message])==1;c.close()
    c=MailboxChannel(path,binding(),'manager');sent=[]
    assert c.send_saved_answers(lambda *args:sent.append(args),now=0)==1
    assert c.send_saved_answers(lambda *args:sent.append(args),now=31)==1
    saved=json.loads(sent[0][1]);assert saved['answer_sha256']==hashlib.sha256(answer.encode()).hexdigest()
    assert saved['request_id']=='q'
    assert c.receive([{'event_id':2,'from_agent_id':'manager','body':json.dumps({**saved,'kind':'help_answer_confirmed'})}])==1
    assert c.send_saved_answers(lambda *args:sent.append(args),now=99)==0;c.close()

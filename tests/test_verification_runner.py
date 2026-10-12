import hashlib
import json
import subprocess
import sys
from pathlib import Path
import pytest
from solution_agent.harness.help_queue import HelpQueue,canonical
from solution_agent.harness.verification_runner import capture
from solution_agent.harness import verification_runner


def setup(tmp_path):
    mailbox=tmp_path/'mailbox.sqlite';q=HelpQueue(mailbox)
    q.bind(dict(task_id='task',work_unit_id='unit',worker_id='worker',origin_chat_id='chat'))
    delivery=q.ask('q','check value','actual check command and result')
    answer=canonical(dict(kind='awaiting_verification',request_id=delivery,proposal={'candidate':2}))
    q.reply(delivery,'chat',answer);q.close()
    return mailbox,hashlib.sha256(answer.encode()).hexdigest()


def test_exact_argv_unicode_and_outputs_are_captured_once(tmp_path):
    mailbox,sha=setup(tmp_path);marker=tmp_path/'once'
    code="from pathlib import Path;import sys; p=Path(sys.argv[1]);assert not p.exists();p.write_text('done');sys.stdout.buffer.write(sys.argv[2].encode('utf-8'))"
    argv=[sys.executable,'-c',code,str(marker),'quotes " backslash \\ 中文 $(not a shell)']
    first=capture(mailbox,'q','verify',sha,argv,str(tmp_path))
    assert first==capture(mailbox,'q','verify',sha,argv,str(tmp_path))
    q=HelpQueue(mailbox);e=json.loads(json.loads(q.status('verify')['body'])['evidence'])
    assert e['argv']==argv and e['stdout']==argv[-1] and e['stderr']=='' and e['exit_code']==0
    assert len(q.pending())==1;q.close()
    with pytest.raises(ValueError,match='identity conflict'):capture(mailbox,'q','verify',sha,[sys.executable,'-c','pass'],str(tmp_path))


@pytest.mark.parametrize('code,timeout,reason',[("raise SystemExit(7)",5,None),("import time;time.sleep(30)",.1,'timeout')])
def test_failed_or_timed_out_check_is_not_a_pass(tmp_path,code,timeout,reason):
    mailbox,sha=setup(tmp_path)
    result=capture(mailbox,'q','verify',sha,[sys.executable,'-c',code],str(tmp_path),timeout)
    assert not result['check_pass']
    q=HelpQueue(mailbox);e=json.loads(json.loads(q.status('verify')['body'])['evidence'])
    assert e['termination_reason']==reason;q.close()


def test_uncertain_launch_and_large_evidence_are_not_rerun(tmp_path):
    mailbox,sha=setup(tmp_path);argv=[sys.executable,'-c',"print('x'*2500)"]
    with pytest.raises(ValueError,match='artifact publication'):capture(mailbox,'q','v',sha,argv,str(tmp_path))
    q=HelpQueue(mailbox)
    assert q.db.execute('select state from captured_checks').fetchone()[0]=='captured'
    assert q.pending()==[]
    q.db.execute("UPDATE captured_checks SET state='prepared'");q.db.commit();q.close()
    with pytest.raises(ValueError,match='uncertain'):capture(mailbox,'q','v',sha,argv,str(tmp_path))


def test_absolute_script_entry_works_outside_package_directory(tmp_path):
    mailbox,sha=setup(tmp_path)
    result=subprocess.run([sys.executable,str(Path(verification_runner.__file__).resolve()),'--mailbox',str(mailbox),
        '--request-id','q','--verification-id','v','--answer-sha256',sha,'--cwd',str(tmp_path),
        '--argv-json',json.dumps([sys.executable,'-c','print(2)'])],cwd=tmp_path,capture_output=True,check=True)
    assert json.loads(result.stdout)['check_pass']

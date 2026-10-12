"""Capture an authorized check as argv and exact streams, then report it once.

Runs with the calling worker's existing permissions. It grants no task scope or
sandbox bypass. A successful process is evidence for review, never closure.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
if __package__:
    from solution_agent.harness.help_queue import HelpQueue, canonical
else:
    from help_queue import HelpQueue, canonical


def capture(mailbox, request_id, verification_id, answer_sha256, argv, cwd, timeout=60):
    if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or '\0' in a for a in argv):
        raise ValueError('argv must be a nonempty string array')
    if not Path(argv[0]).is_absolute() or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('absolute executable and existing working directory required')
    if not 0<timeout<=300 or len(canonical(argv).encode())>16384:
        raise ValueError('bounded check required')
    if not Path(mailbox).is_file(): raise ValueError('mailbox not initialized')
    q=HelpQueue(mailbox)
    try:
        question=q.status(request_id)
        if (question['state']!='answered' or not question['answer'] or
            hashlib.sha256(question['answer'].encode()).hexdigest()!=answer_sha256):
            raise ValueError('missing or stale proposal')
        proposal=json.loads(question['answer'])
        if proposal.get('kind')!='awaiting_verification' or proposal.get('request_id')!=question['delivery_id']:
            raise ValueError('unbound proposal')
        if question['resolution']['state'] in ('closed','human_required'):
            raise ValueError('incident no longer accepts automatic verification')
        q.db.execute('CREATE TABLE IF NOT EXISTS captured_checks(id TEXT PRIMARY KEY,intent TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT)')
        intent=canonical(dict(binding=q.binding(),request_id=request_id,answer_sha256=answer_sha256,argv=argv,cwd=str(Path(cwd).resolve()),timeout=timeout))
        with q.db:
            q.db.execute('BEGIN IMMEDIATE')
            old=q.db.execute('SELECT * FROM captured_checks WHERE id=?',(verification_id,)).fetchone()
            if old and old['intent']!=intent: raise ValueError('check identity conflict')
            if old and old['state']!='captured': raise ValueError('check outcome uncertain; inspect before retry')
            if not old:q.db.execute('INSERT INTO captured_checks VALUES (?,?,?,NULL)',(verification_id,intent,'prepared'))
        if old:
            receipt=json.loads(old['receipt'])
        else:
            # Use files rather than unbounded communicate buffers. A deterministic
            # monitor stops excessive output; the model does not poll the process.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process=subprocess.Popen(argv,cwd=cwd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,shell=False)
                deadline=time.monotonic()+timeout;reason=None
                try:
                    while process.poll() is None:
                        if time.monotonic()>=deadline:reason='timeout'
                        elif out.tell()+err.tell()>2*1024*1024:reason='output_limit'
                        if reason:process.kill();break
                        time.sleep(.02)
                    code=process.wait(timeout=5)
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=5)
                out.seek(0);err.seek(0);stdout=out.read(2*1024*1024+1);stderr=err.read(2*1024*1024+1)
                if len(stdout)+len(stderr)>2*1024*1024:reason='output_limit'
                # UTF-8 strict: never silently replace characters in evidence.
                receipt=dict(v=1,argv=argv,cwd=str(Path(cwd).resolve()),stdout=stdout.decode('utf-8'),
                             stderr=stderr.decode('utf-8'),exit_code=code,termination_reason=reason,
                             proposal_id=question['delivery_id'],answer_sha256=answer_sha256)
                with q.db:q.db.execute("UPDATE captured_checks SET state='captured',receipt=? WHERE id=?",(canonical(receipt),verification_id))
        evidence=canonical(receipt)
        # Preserve full receipt locally on overflow; never silently truncate it or
        # enqueue an envelope that the manager's ingress must reject.
        if len(evidence)>1700:raise ValueError('captured evidence requires artifact publication; retained in mailbox')
        passed=receipt['exit_code']==0 and receipt['termination_reason'] is None
        delivery=q.verify(request_id,verification_id,answer_sha256,passed,evidence)
        return dict(delivery_id=delivery,state='queued_locally',manager_received=False,
                    captured_sha256=hashlib.sha256(evidence.encode()).hexdigest(),check_pass=passed)
    finally:q.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('mailbox','request-id','verification-id','answer-sha256','argv-json','cwd'):p.add_argument('--'+key,required=True)
    p.add_argument('--timeout',type=float,default=60)
    a=p.parse_args()
    print(json.dumps(capture(a.mailbox,a.request_id,a.verification_id,a.answer_sha256,json.loads(a.argv_json),a.cwd,a.timeout),ensure_ascii=True))


if __name__=='__main__':main()

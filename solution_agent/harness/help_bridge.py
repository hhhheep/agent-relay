"""Transport-independent bridge boundary. Trusted bindings come from publisher.

send must durably ingest with delivery_id dedup BEFORE returning an acceptance.
It must NOT directly call the non-idempotent App send API; NativeDispatch owns it.
"""
from __future__ import annotations
import json
from solution_agent.harness.help_queue import HelpQueue


def transfer(mailbox, expected_binding, send):
    q=HelpQueue(mailbox)
    try:
        if q.binding()!=expected_binding: raise ValueError('untrusted mailbox binding')
        delivered=0
        for row in q.pending():
            if row['state']=='accepted': continue
            envelope={'v':1,'delivery_id':row['delivery_id'],'binding':expected_binding,
                      'request_id':row['request_id'],'payload':json.loads(row['body'])}
            try: receipt=send(envelope)
            except (ConnectionError,TimeoutError,OSError):
                return {'state':'waiting_manager','delivered':delivered}
            if receipt!={'delivery_id':row['delivery_id'],'origin_chat_id':expected_binding['origin_chat_id'],'durably_received':True}:
                raise ValueError('invalid manager acceptance')
            q.accept(row['delivery_id'],expected_binding['origin_chat_id']); delivered+=1
        return {'state':'transferred','delivered':delivered}
    finally: q.close()


def deliver_reply(mailbox,expected_binding,receipt,*,resume_ledger=None,resume_context=None):
    q=HelpQueue(mailbox)
    try:
        if q.binding()!=expected_binding: raise ValueError('untrusted mailbox binding')
        if receipt.get('binding')!=expected_binding: raise ValueError('reply binding mismatch')
        q.reply(receipt['delivery_id'],expected_binding['origin_chat_id'],receipt['answer'])
        if resume_ledger is not None:
            question=q.db.execute('SELECT * FROM requests WHERE delivery_id=?',(receipt['delivery_id'],)).fetchone()
            if json.loads(question['body'])['kind']=='question':
                if json.loads(question['answer']).get('kind')=='human_required': return None
                if resume_context is None: raise ValueError('trusted resume context required')
                # A replay repairs interruption between mailbox commit and local
                # continuation planning. Caller must acknowledge only after return.
                return resume_ledger.plan(expected_binding,dict(question),mailbox=str(mailbox),**resume_context)
        return None
    finally: q.close()

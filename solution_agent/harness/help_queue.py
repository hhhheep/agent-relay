"""Experimental per-execution durable help mailbox; no network or model calls.

Run via the packet's absolute script path. The bridge must validate the binding
against its trusted registry; mailbox contents are worker data, not authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class HelpQueue:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests (
            seq INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, delivery_id TEXT UNIQUE NOT NULL,
            body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', answer TEXT);
          CREATE TABLE IF NOT EXISTS watch (
            id INTEGER PRIMARY KEY CHECK(id=1), progress REAL NOT NULL, episode INTEGER NOT NULL,
            finished INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS resolutions (
            delivery_id TEXT PRIMARY KEY, state TEXT NOT NULL, answer_sha256 TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def bind(self, binding):
        required = {'task_id', 'work_unit_id', 'worker_id', 'origin_chat_id'}
        if set(binding) != required or any(not isinstance(v, str) or not v.strip() or len(v)>256 for v in binding.values()):
            raise ValueError('invalid binding')
        body = canonical(binding)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.db.execute('SELECT body FROM binding').fetchone()
            if old and old['body'] != body:
                raise ValueError('binding conflict')
            self.db.execute('INSERT OR IGNORE INTO binding VALUES (1,?)', (body,))

    def binding(self):
        row = self.db.execute('SELECT body FROM binding').fetchone()
        if not row:
            raise ValueError('unbound mailbox')
        return json.loads(row['body'])

    def _enqueue(self, request_id, payload):
        if not isinstance(request_id, str) or not request_id or len(request_id)>128:
            raise ValueError('invalid request ID')
        body = canonical(payload)
        if len(body.encode())>16384:
            raise ValueError('request too large')
        identity = canonical([self.binding(), request_id])
        delivery = hashlib.sha256(identity.encode()).hexdigest()
        old = self.db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['body'] != body:
                raise ValueError('request content conflict')
            return old['delivery_id']
        if self.db.execute("SELECT count(*) FROM requests WHERE state!='answered'").fetchone()[0]>=128:
            raise ValueError('queue full; retain request and retry with same ID')
        self.db.execute('INSERT INTO requests(request_id,delivery_id,body) VALUES (?,?,?)', (request_id,delivery,body))
        return delivery

    def ask(self, request_id, problem, acceptance, evidence=''):
        if request_id.startswith('supervisor:'):
            raise ValueError('reserved request ID')
        if not all(isinstance(x,str) and x.strip() for x in (problem,acceptance)):
            raise ValueError('problem and acceptance required')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return self._enqueue(request_id, {'kind':'question','problem':problem,'acceptance':acceptance,'evidence':evidence})

    def pending(self):
        # Reading / failed transport never acknowledges or removes a request.
        return [dict(r) for r in self.db.execute("SELECT * FROM requests WHERE state!='answered' ORDER BY seq")]

    def accept(self, delivery_id, origin_chat_id):
        if origin_chat_id != self.binding()['origin_chat_id']:
            raise ValueError('wrong chat')
        with self.db:
            row=self.db.execute('SELECT state FROM requests WHERE delivery_id=?',(delivery_id,)).fetchone()
            if not row: raise ValueError('unknown delivery')
            self.db.execute("UPDATE requests SET state='accepted' WHERE delivery_id=? AND state='pending'",(delivery_id,))

    def reply(self, delivery_id, origin_chat_id, answer):
        if origin_chat_id != self.binding()['origin_chat_id']:
            raise ValueError('wrong chat')
        if not isinstance(answer,str) or not answer.strip() or len(answer.encode())>65536:
            raise ValueError('invalid answer')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT * FROM requests WHERE delivery_id=?',(delivery_id,)).fetchone()
            if not row: raise ValueError('unknown delivery')
            if row['answer'] is not None and row['answer']!=answer:
                raise ValueError('reply conflict')
            self.db.execute("UPDATE requests SET state='answered',answer=? WHERE delivery_id=?",(answer,delivery_id))
            payload=json.loads(row['body'])
            if payload['kind']=='question':
                try: response=json.loads(answer)
                except ValueError: response={}
                state='human_required' if isinstance(response,dict) and response.get('kind')=='human_required' else 'awaiting_verification'
                if state=='human_required' and response.get('request_id')!=delivery_id:
                    raise ValueError('unbound human escalation')
                self.db.execute('INSERT OR IGNORE INTO resolutions VALUES (?,?,?)',
                                (delivery_id,state,hashlib.sha256(answer.encode()).hexdigest()))
            elif payload['kind']=='verification':
                review=json.loads(answer)
                if review.get('request_id')!=delivery_id or review.get('incident')!=payload['proposal_id'] or review.get('kind') not in ('closed','retry_allowed','human_required'):
                    raise ValueError('invalid verification review')
                self.db.execute('UPDATE resolutions SET state=? WHERE delivery_id=?', (review['kind'],payload['proposal_id']))

    def verify(self, request_id, verification_id, answer_sha256, passed, evidence):
        if type(passed) is not bool or not isinstance(evidence,str) or not evidence.strip():
            raise ValueError('verification requires actual boolean and evidence')
        if verification_id.startswith('supervisor:'): raise ValueError('reserved request ID')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
            if not row or row['state']!='answered' or json.loads(row['body'])['kind']!='question':
                raise ValueError('question has no proposal to verify')
            actual=hashlib.sha256(row['answer'].encode()).hexdigest()
            if actual!=answer_sha256: raise ValueError('stale answer verification')
            proposal=json.loads(row['answer'])
            if proposal.get('kind')!='awaiting_verification' or proposal.get('request_id')!=row['delivery_id']:
                raise ValueError('answer is not a bound proposal')
            return self._enqueue(verification_id,{'kind':'verification','proposal_id':row['delivery_id'],
                                'pass':passed,'evidence':evidence,'answer_sha256':actual})

    def unresolved_questions(self):
        return self.db.execute("""SELECT count(*) FROM requests r LEFT JOIN resolutions s
          ON r.delivery_id=s.delivery_id WHERE json_extract(r.body,'$.kind')='question'
          AND (s.state IS NULL OR s.state!='closed')""").fetchone()[0]

    def status(self, request_id):
        row=self.db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
        if not row: raise ValueError('unknown request')
        result=dict(row)
        resolution=self.db.execute('SELECT state,answer_sha256 FROM resolutions WHERE delivery_id=?',(row['delivery_id'],)).fetchone()
        result['resolution']=dict(resolution) if resolution else None
        return result

    def progress(self, now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('INSERT INTO watch VALUES (1,?,0,0) ON CONFLICT(id) DO UPDATE SET progress=excluded.progress,episode=watch.episode+1', (now,))

    def observe(self, *, now=None, stale_seconds=600, exit_code=None, contract_valid=False):
        """Deterministic evidence only; never infers the model's underlying problem."""
        if stale_seconds<=0: raise ValueError('positive deadline required')
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT * FROM watch').fetchone()
            if not row or row['finished']: return None
            if exit_code is not None:
                delivery=None
                if exit_code != 0 or not contract_valid:
                    delivery=self._enqueue('supervisor:exit', {'kind':'worker_incomplete','exit_code':exit_code,'contract_valid':bool(contract_valid)})
                self.db.execute('UPDATE watch SET finished=1')
                return delivery
            if now-row['progress']>=stale_seconds:
                # Fixed evidence per progress episode makes repeated observations idempotent.
                return self._enqueue('supervisor:stale:'+str(row['episode']), {'kind':'no_progress_observed','last_progress':row['progress']})
        return None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mailbox',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    ask=sub.add_parser('ask')
    ask.add_argument('--request-id',required=True)
    ask.add_argument('--problem',required=True)
    ask.add_argument('--acceptance',required=True)
    ask.add_argument('--evidence',default='')
    status=sub.add_parser('status'); status.add_argument('--request-id',required=True)
    sub.add_parser('progress')
    verify=sub.add_parser('verify')
    verify.add_argument('--request-id',required=True)
    verify.add_argument('--verification-id',required=True)
    verify.add_argument('--answer-sha256',required=True)
    verify.add_argument('--passed',choices=['true','false'],required=True)
    verify.add_argument('--evidence',required=True)
    args=p.parse_args()
    if not Path(args.mailbox).is_file(): raise ValueError('mailbox must be initialized by supervisor')
    q=HelpQueue(args.mailbox)
    try:
        q.binding()
        if args.action=='ask': result={'delivery_id':q.ask(args.request_id,args.problem,args.acceptance,args.evidence),'state':'queued_locally','manager_received':False}
        elif args.action=='status': result=q.status(args.request_id)
        elif args.action=='verify': result={'delivery_id':q.verify(args.request_id,args.verification_id,args.answer_sha256,args.passed=='true',args.evidence),'state':'queued_locally','manager_received':False}
        else: q.progress(); result={'progress_recorded':True}
        print(canonical(result))
    finally: q.close()


if __name__=='__main__': main()

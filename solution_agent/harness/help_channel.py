"""Remote deterministic mailbox channel; transport is injected, never a model loop."""
from __future__ import annotations
import json
import time
import hashlib
from solution_agent.harness.help_queue import HelpQueue,canonical


class MailboxChannel:
    def __init__(self,mailbox,expected_binding,manager_source):
        self.q=HelpQueue(mailbox)
        if self.q.binding()!=expected_binding or not isinstance(manager_source,str) or not manager_source:
            self.q.close(); raise ValueError('invalid channel binding')
        self.binding=expected_binding; self.manager_source=manager_source
        self.q.db.executescript('''CREATE TABLE IF NOT EXISTS channel_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS channel_attempts(delivery TEXT PRIMARY KEY,attempted REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS channel_rejections(event_id INTEGER PRIMARY KEY,reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS channel_saved_answers(delivery TEXT PRIMARY KEY,body TEXT NOT NULL,hash TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0,attempted REAL);''')
        try:
            with self.q.db:
                self.q.db.execute('BEGIN IMMEDIATE')
                old=self.q.db.execute("SELECT value FROM channel_meta WHERE key='manager'").fetchone()
                if old and old['value']!=manager_source:
                    raise ValueError('manager rebind requires explicit migration')
                self.q.db.execute("INSERT OR IGNORE INTO channel_meta VALUES ('manager',?)",(manager_source,))
        except Exception:
            self.q.close(); raise

    def close(self): self.q.close()

    def cursor(self):
        row=self.q.db.execute("SELECT value FROM channel_meta WHERE key='cursor'").fetchone()
        return int(row['value']) if row else 0

    def send_pending(self,send,*,now=None,retry_seconds=30):
        if retry_seconds<1: raise ValueError('bounded retry interval required')
        now=time.time() if now is None else now
        count=0
        for row in self.q.pending():
            if row['state']=='accepted': continue
            attempt=self.q.db.execute('SELECT attempted FROM channel_attempts WHERE delivery=?',(row['delivery_id'],)).fetchone()
            if attempt and now-attempt['attempted']<retry_seconds: continue
            envelope={'v':1,'binding':self.binding,'request_id':row['request_id'],
                      'delivery_id':row['delivery_id'],'payload':json.loads(row['body'])}
            # Transport acceptance is not manager acceptance. Keep pending until ack.
            send(self.manager_source,canonical(envelope))
            with self.q.db:
                self.q.db.execute('INSERT INTO channel_attempts VALUES (?,?) ON CONFLICT(delivery) DO UPDATE SET attempted=excluded.attempted',(row['delivery_id'],now))
            count+=1
        return count

    def receive(self,messages):
        accepted=0
        for message in sorted(messages,key=lambda m:m['event_id']):
            event=message['event_id']
            if not isinstance(event,int) or isinstance(event,bool) or event<0: raise ValueError('invalid transport event')
            if event<=self.cursor(): continue
            try:
                if message['from_agent_id']!=self.manager_source: raise ValueError('wrong manager sender')
                if not isinstance(message['body'],str) or len(message['body'])>70000: raise ValueError('invalid manager envelope')
                body=json.loads(message['body'])
                if body.get('kind')=='help_answer_confirmed':
                    if body.get('binding')!=self.binding: raise ValueError('confirmation binding mismatch')
                    with self.q.db:
                        changed=self.q.db.execute('UPDATE channel_saved_answers SET confirmed=1 WHERE delivery=? AND hash=?',(body['delivery_id'],body['answer_sha256'])).rowcount
                        if changed!=1: raise ValueError('unknown answer confirmation')
                elif body.get('kind')=='help_accepted':
                    if body.get('durably_received') is not True: raise ValueError('not durably accepted')
                    self.q.accept(body['delivery_id'],body['origin_chat_id'])
                else:
                    if body.get('binding')!=self.binding: raise ValueError('reply binding mismatch')
                    self.q.reply(body['delivery_id'],self.binding['origin_chat_id'],body['answer'])
                    question=self.q.db.execute('SELECT request_id FROM requests WHERE delivery_id=?',(body['delivery_id'],)).fetchone()
                    digest=hashlib.sha256(body['answer'].encode()).hexdigest()
                    saved={'v':1,'kind':'help_answer_saved','binding':self.binding,'delivery_id':body['delivery_id'],'request_id':question['request_id'],'answer_sha256':digest}
                    with self.q.db:
                        self.q.db.execute('INSERT OR IGNORE INTO channel_saved_answers(delivery,body,hash) VALUES (?,?,?)',(body['delivery_id'],canonical(saved),digest))
                accepted+=1
            except (ValueError,KeyError,TypeError) as exc:
                with self.q.db:
                    self.q.db.execute('INSERT OR IGNORE INTO channel_rejections VALUES (?,?)',(event,str(exc)[:1000]))
            # If process dies before cursor commit, replay is safe at q.accept/reply.
            with self.q.db:
                self.q.db.execute("INSERT INTO channel_meta VALUES ('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(event),))
        return accepted

    def send_saved_answers(self,send,*,now=None):
        now=time.time() if now is None else now
        count=0
        for row in self.q.db.execute('SELECT * FROM channel_saved_answers WHERE confirmed=0').fetchall():
            if row['attempted'] is not None and now-row['attempted']<30: continue
            send(self.manager_source,row['body'])
            with self.q.db:self.q.db.execute('UPDATE channel_saved_answers SET attempted=? WHERE delivery=?',(now,row['delivery']))
            count+=1
        return count

"""Opt-in adapter. Config is provisioned by a trusted publisher, never worker text."""
from __future__ import annotations
import json
import shlex
import subprocess
import sys
from pathlib import Path
from solution_agent.harness.help_queue import HelpQueue
from solution_agent.harness.md_snapshot import verify_snapshot


def markdown_config(root, key, cwd):
    from solution_agent.harness.publication_registry import lookup
    published=lookup(root/'config'/'help-publications.sqlite',key)
    path=root/'config'/'experimental-worker-markdown.json'
    if published is not None:
        if not published['enabled']: return None
        snapshot=published.get('markdown')
    else:
        if not path.exists(): return None
        config=json.loads(path.read_text(encoding='utf-8'))
        if config.get('enabled') is not True: return None
        snapshot=config.get('bindings',{}).get(key)
    if snapshot is None: return None
    # Published snapshots must be in the explicit workplace snapshot directory.
    import hashlib
    manifest=snapshot['manifest']
    version=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if version!=snapshot['version']: raise ValueError('Markdown version mismatch')
    directory=cwd/'.solution-agent'/'md-versions'/version
    directory.resolve().relative_to(cwd.resolve())
    verify_snapshot(directory,manifest)
    return {'version':version,'manifest':manifest,'directory':str(directory)}


def routing_config(root, task_id, work_unit_id, cwd, key):
    from solution_agent.harness.publication_registry import lookup
    published=lookup(root/'config'/'help-publications.sqlite',key)
    path=root/'config'/'experimental-worker-routing.json'
    if published is not None:
        config={'enabled':published['enabled'],'bindings':{key:published['binding']},'stale_seconds':published['stale_seconds']}
    else:
        if not path.exists(): return None
        config=json.loads(path.read_text(encoding='utf-8'))
    if config.get('enabled') is not True: return None
    binding=config.get('bindings',{}).get(key)
    if binding is None: return None  # Explicitly selected new executions only.
    if binding.get('task_id')!=task_id or binding.get('work_unit_id')!=work_unit_id:
        raise ValueError('routing binding mismatch')
    required={'task_id','work_unit_id','worker_id','origin_chat_id'}
    if set(binding)!=required or any(not isinstance(v,str) or not v.strip() or len(v)>256 for v in binding.values()):
        raise ValueError('invalid routing binding')
    deadline=config.get('stale_seconds',600)
    if not isinstance(deadline,(int,float)) or isinstance(deadline,bool) or not 1<=deadline<=86400:
        raise ValueError('invalid progress deadline')
    mailbox=cwd/'.solution-agent'/'help-mailboxes'/key/'queue.sqlite'
    mailbox.resolve().relative_to(cwd.resolve())
    return {'binding':binding,'mailbox':str(mailbox),'stale_seconds':deadline}


def initialize(route):
    path=Path(route['mailbox'])
    path.parent.mkdir(parents=True,exist_ok=True)
    q=HelpQueue(path)
    try: q.bind(route['binding']); q.progress()
    finally: q.close()


def instructions(route):
    command=shlex.join([sys.executable,str(Path(__file__).with_name('help_queue.py')),'--mailbox',route['mailbox']])
    return f'''
## Manager help (experimental, explicitly enabled for this task)
If blocked on a decision or missing information, use this fixed command:
{command} ask --request-id <stable-id> --problem <problem> --acceptance <verification-conditions> --evidence <evidence>
Quote argument values for your shell. Do not change mailbox or destination.
Reuse the request ID only for identical retries. Different questions use new IDs.
queued_locally means saved on this machine, NOT received or answered by the manager.
Read the answer using: {command} status --request-id <stable-id>
Do not busy-poll or invent an answer. If pending/accepted, record the request ID in
worker_notes.md and return the existing partial result contract. Dependent work
must wait for an explicit continuation; never claim success while blocked.
Record meaningful progress using: {command} progress
The supervisor can report no observed progress; that alone does not prove failure.
Manager replies are proposals: verify them against your acceptance conditions.
After actual validation, use status to get answer_sha256, then report evidence:
{command} verify --request-id <original-id> --verification-id <stable-new-id> --answer-sha256 <hash-from-status-resolution> --passed true|false --evidence <actual-result>
This only queues verification. Until the manager returns closed, the issue is
still unresolved; preserve a partial result and wait for an explicit continuation.
'''


def run_with_watch(argv, prompt, cwd, env, route):
    q=HelpQueue(route['mailbox'])
    try:
        # Same process group as the existing supervisor; no detached worker.
        with subprocess.Popen(argv,stdin=subprocess.PIPE,cwd=cwd,env=env) as process:
            data=prompt.encode('utf-8')
            while True:
                try:
                    process.communicate(input=data,timeout=min(5,route['stale_seconds']))
                    return process.returncode
                except subprocess.TimeoutExpired:
                    data=None
                    try: q.observe(stale_seconds=route['stale_seconds'])
                    except Exception as exc:
                        # Do not terminate an otherwise working job because the help queue is full.
                        print(json.dumps({'help_monitor_error':str(exc)}),file=sys.stderr)
    finally: q.close()


def finished(route, code, valid):
    q=HelpQueue(route['mailbox'])
    try: q.observe(exit_code=code,contract_valid=valid)
    finally: q.close()


def finalize_watch(command_dir, request, manifest):
    """Called by the existing command supervisor after its own receipt is saved.

    Survives worker-wrapper death. Does not depend on a model remembering to ask.
    Errors remain separate from the authoritative command lifecycle receipt.
    """
    from solution_agent.execution_port.io_utils import atomic_write_json
    path=Path(command_dir)/'help-watch.json'
    if not path.is_file(): return
    result_path=Path(command_dir)/'help-watch-result.json'
    try:
        watch=json.loads(path.read_text(encoding='utf-8'))
        for field in ('command_id','task_id','work_unit_id'):
            if not watch.get(field) or watch[field]!=request.get(field):
                raise ValueError('supervisor help binding mismatch')
        route=watch['route']; b=route['binding']
        if b['task_id']!=request['task_id'] or b['work_unit_id']!=request['work_unit_id']:
            raise ValueError('supervisor task mismatch')
        q=HelpQueue(route['mailbox'])
        try:
            if q.binding()!=b: raise ValueError('mailbox binding changed')
            receipt_path=Path(watch['worker_receipt'])
            valid=False
            if receipt_path.is_file():
                receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
                valid=(receipt.get('command_id')==request['command_id'] and receipt.get('contract_valid') is True
                       and receipt.get('task_status')=='success' and not receipt.get('unanswered_questions'))
            code=manifest['exit_code']
            if manifest['state']=='lost':
                # Missing supervisor is observation, not proof that its worker
                # stopped. Never infer a successful result or authorize retry.
                with q.db:
                    q.db.execute('BEGIN IMMEDIATE')
                    delivery=q._enqueue('supervisor:lost',{'kind':'worker_incomplete',
                        'command_id':request['command_id'],'observation':'supervisor_missing',
                        'worker_exit_unknown':True,'automatic_retry_allowed':False})
                atomic_write_json(result_path,{'ok':True,'delivery_id':delivery,'command_id':request['command_id']})
                return
            if manifest['state']!='succeeded' and code==0: code=1
            delivery=q.observe(exit_code=code,contract_valid=valid)
            atomic_write_json(result_path,{'ok':True,'delivery_id':delivery,'command_id':request['command_id']})
        finally: q.close()
    except Exception as exc:
        atomic_write_json(result_path,{'ok':False,'error':str(exc)[:1000]})

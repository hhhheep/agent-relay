
# Independent extraction: resolve the sole project Python source, never a research checkout.
import sys as _extraction_sys
from pathlib import Path as _ExtractionPath
_extraction_sys.path.insert(0, str(_ExtractionPath(__file__).resolve().parents[2]))

"""Single bounded local mailbox operation for the deterministic transport driver."""
import json
import sys
import io
from pathlib import Path
from solution_agent.harness.help_channel import MailboxChannel

def transport_body(body, runtime_root=None):
    envelope=json.loads(body)
    payload=envelope.get('payload')
    if payload is None:return body
    local={'v':1,'id':envelope['delivery_id'],'incident':payload.get('proposal_id') if payload.get('kind')=='verification' else envelope['delivery_id'],
           'kind':'verification' if payload.get('kind')=='verification' else 'request','payload':payload}
    if len(json.dumps(local,ensure_ascii=False,separators=(',',':')))<=2400:return body
    if not runtime_root:raise ValueError('large_help_requires_artifact_runtime')
    from solution_agent.harness.artifact_runtime import ArtifactStore
    from solution_agent.harness.task_capsule import TaskCapsuleStore
    state=Path(runtime_root)/'state'
    task=TaskCapsuleStore(state).load(envelope['binding']['task_id'])
    manifest=ArtifactStore(state).put_bytes(task,io.BytesIO(body.encode('utf-8')),media_type='application/json',source_kind='generated',
        source_ref='help:'+envelope['delivery_id'],provenance={'request_id':envelope['request_id'],'binding':envelope['binding']})
    ref={'artifact_id':manifest.artifact_id,'sha256':manifest.sha256,'byte_size':manifest.byte_size,'task_id':task.task_id}
    if payload['kind']=='question':
        compact={'kind':'question','problem':'Read the complete original question and evidence from payload_artifact before deciding.',
                 'acceptance':'Use the original acceptance requirements in that artifact; verify its binding, request_id and delivery_id.'}
    elif payload['kind']=='verification':
        compact={k:v for k,v in payload.items() if k!='evidence'}
        compact['evidence']='Read the complete original verification evidence from payload_artifact before reviewing.'
    else:raise ValueError('large_help_kind_requires_review')
    compact['payload_artifact']=ref
    envelope['payload']=compact
    return json.dumps(envelope,ensure_ascii=False,separators=(',',':'))


def main():
    request=json.loads(sys.stdin.buffer.read(12*1024*1024+1))
    if set(request)-{'mailbox','binding','manager_source','messages','runtime_root'} or not {'mailbox','binding','manager_source','messages'}<=set(request):
        raise ValueError('invalid mailbox operation')
    if not isinstance(request['messages'],list) or len(request['messages'])>100:
        raise ValueError('invalid inbox page')
    channel=MailboxChannel(request['mailbox'],request['binding'],request['manager_source'])
    try:
        received=channel.receive(request['messages']);outgoing=[]
        # Reserving a send attempt before crossing the process boundary can
        # defer a crash retry by 30 s, but cannot acknowledge or lose a request.
        channel.send_pending(lambda to,body:outgoing.append({'to':to,'body':transport_body(body,request.get('runtime_root'))}))
        channel.send_saved_answers(lambda to,body:outgoing.append({'to':to,'body':body}))
        result={'cursor':channel.cursor(),'received':received,'outgoing':outgoing}
        print(json.dumps(result,ensure_ascii=True))
    finally:channel.close()


if __name__=='__main__':main()

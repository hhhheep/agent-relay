"""Deterministic Markdown transfer plan for the existing Harness command surface.

Small bundles use an encoded command. Large bundles fail before submission and
must use artifact transport; no silent truncation, shell interpolation or fallback.
"""
from __future__ import annotations
import base64
import hashlib
import json
import shlex
from pathlib import Path
from solution_agent.harness.md_snapshot import build_bundle


def plan_sync(source_root, names, *, remote_python, remote_workplace, versions_relative='.solution-agent/md-versions'):
    from pathlib import PurePosixPath
    relative=PurePosixPath(versions_relative)
    if relative.is_absolute() or '..' in relative.parts or relative.as_posix()!=versions_relative or '\\' in versions_relative or ':' in versions_relative:
        raise ValueError('invalid versions directory')
    if not isinstance(remote_python,str) or not remote_python.strip():
        raise ValueError('declared Python executable required')
    # Caller supplies the selected profile's absolute interpreter/workplace.
    if not (remote_python.startswith('/') or (len(remote_python)>2 and remote_python[1]==':')):
        raise ValueError('absolute Python executable required')
    if not (str(remote_workplace).startswith('/') or (len(str(remote_workplace))>2 and str(remote_workplace)[1]==':')):
        raise ValueError('absolute workplace required')
    bundle=build_bundle(source_root,names)
    receiver=Path(__file__).with_name('md_snapshot.py').read_bytes()
    payload={'bundle':bundle,'workplace':str(remote_workplace),'relative':versions_relative}
    encoded=base64.b64encode(json.dumps(payload,ensure_ascii=False).encode()).decode()
    program=receiver.decode('utf-8')+'''\n
import base64
payload=json.loads(base64.b64decode(PAYLOAD,validate=True))
workplace=Path(payload['workplace']).resolve(strict=True)
destination=workplace/payload['relative']
destination.resolve().relative_to(workplace)
result=materialize(payload['bundle'],destination)
verify_snapshot(result,payload['bundle']['manifest'])
print(json.dumps({'ok':True,'version':payload['bundle']['version'],'manifest':payload['bundle']['manifest'],'directory':str(result)},ensure_ascii=True))
'''.replace('PAYLOAD',repr(encoded))
    argv=[remote_python,'-c','import base64; exec(base64.b64decode('+repr(base64.b64encode(program.encode()).decode())+',validate=True))']
    command=shlex.join(argv)
    if len(command.encode())>48000:
        raise ValueError('bundle requires artifact transport; encoded command exceeds 48000 bytes')
    identity={'version':bundle['version'],'manifest':bundle['manifest'],'workplace':str(remote_workplace),'relative':versions_relative,'receiver_sha256':hashlib.sha256(receiver).hexdigest()}
    return {**identity,'plan_id':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),'command':command,'argv':argv}


def accept_sync(plan, command_result):
    """Only a completed original command plus matching remote hashes enables use."""
    if command_result.get('state')!='succeeded' or command_result.get('exit_code')!=0:
        raise ValueError('Markdown synchronization not completed')
    preview=command_result.get('stdout',{}).get('preview')
    if not isinstance(preview,str): raise ValueError('missing synchronization receipt')
    receipt=json.loads(preview)
    if receipt.get('ok') is not True or receipt.get('version')!=plan['version'] or receipt.get('manifest')!=plan['manifest']:
        raise ValueError('Markdown synchronization receipt mismatch')
    expected=str(plan['workplace']).replace('\\','/').rstrip('/')+'/'+plan['relative']+'/'+plan['version']
    if str(receipt.get('directory','')).replace('\\','/').rstrip('/')!=expected:
        raise ValueError('Markdown synchronization destination mismatch')
    return receipt

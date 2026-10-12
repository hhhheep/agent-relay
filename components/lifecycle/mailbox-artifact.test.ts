import test from 'node:test';import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';
test('large question uses existing task artifact CAS without truncation or changing delivery identity',()=>{
 const root=mkdtempSync(join(tmpdir(),'help-artifact-'));
 try{
  const code=`import json,sys,importlib.util
from pathlib import Path
from solution_agent.harness.task_capsule import TaskCapsule,TaskCapsuleStore
from solution_agent.harness.artifact_runtime import ArtifactStore
spec=importlib.util.spec_from_file_location('mailbox_rpc',sys.argv[2]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(sys.argv[1]);task=TaskCapsule(task_id='task',objective='test',source_release_id='test',approval_owner='test',scope={})
TaskCapsuleStore(root/'state').create(task)
original={'v':1,'binding':{'task_id':'task','work_unit_id':'unit','worker_id':'worker','origin_chat_id':'origin'},'request_id':'original-request','delivery_id':'original-delivery','payload':{'kind':'question','problem':'original problem','acceptance':'original acceptance','evidence':'完整證據'*1500}}
body=json.dumps(original,ensure_ascii=False);compact=json.loads(m.transport_body(body,str(root)))
ref=compact['payload']['payload_artifact'];cas=ArtifactStore(root/'state');manifest=cas.resolve(task,ref['artifact_id']);saved=cas._object_path(manifest.sha256).read_text(encoding='utf-8')
assert json.loads(saved)==original
assert compact['binding']==original['binding'] and compact['request_id']==original['request_id'] and compact['delivery_id']==original['delivery_id']
assert len(json.dumps(compact,separators=(',',':'),ensure_ascii=False))<2400
assert ref['sha256']==manifest.sha256 and ref['byte_size']==len(body.encode())
assert json.loads(m.transport_body(body,str(root)))==compact
assert TaskCapsuleStore(root/'state').load('task').version==task.version
try:m.transport_body(body)
except ValueError:pass
else:raise AssertionError('must not silently drop large question')
print(json.dumps({'verified':True,'ref':ref}))`;
  const result=JSON.parse(execFileSync(resolve('../../.venv/Scripts/python.exe'),['-c',code,root,resolve('mailbox-rpc.py')],{encoding:'utf8',env:{...process.env,PYTHONPATH:resolve('../..')},windowsHide:true}));
  assert.equal(result.verified,true);
 }finally{rmSync(root,{recursive:true,force:true});}
});

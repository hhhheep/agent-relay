import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';
import {ResumeController} from './resume-controller.ts';

test('real ledger process commits before submit and never retries an uncertain continuation',async()=>{
 const root=mkdtempSync(join(tmpdir(),'resume-controller-'));let submissions=0;let originalState='running';let launched:any;
 const harness={recover:async(task:string,command:string)=>({task_id:task,command_id:command,state:originalState,writer_lease_released:true}),submit:async(request:any)=>{submissions++;launched=request;throw Error('lost acknowledgment');}};
 const connect=()=>new ResumeController(resolve(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python'),resolve('resume-rpc.py'),join(root,'ledger.sqlite'),harness);
 try{
  let controller=connect();const binding={task_id:'task',work_unit_id:'original-unit',worker_id:'worker',origin_chat_id:'chat'};
  const question={request_id:'q',delivery_id:'delivery',state:'answered',body:JSON.stringify({kind:'question',problem:'blocked',acceptance:'actual check'}),answer:JSON.stringify({kind:'awaiting_verification',request_id:'delivery'})};
  const context={python_path:'/runtime/python',help_script:'/runtime/help.py',mailbox:'/work/mailbox.sqlite',previous_command_id:'original'};
  const {id}=await controller.plan(binding,question,context);
  assert.equal((await controller.tick(id)).state,'waiting_original_command');assert.equal(submissions,0);
  originalState='succeeded';await assert.rejects(controller.tick(id),/lost acknowledgment/);assert.equal(submissions,1);
  controller=connect();assert.equal((await controller.plan(binding,question,context)).id,id);
  assert.equal((await controller.tick(id)).state,'submission_uncertain');assert.equal(submissions,1);
  await controller.rpc({action:'reconcile',id,receipt:{task_id:'task',work_unit_id:launched.work_unit_id,command_id:'recovered-command',state:'succeeded'}});
  assert.equal((await controller.tick(id)).state,'finished');assert.equal(submissions,1);
 }finally{rmSync(root,{recursive:true,force:true});}
});

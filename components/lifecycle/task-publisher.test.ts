import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';
import {execFileSync} from 'node:child_process';
import {TaskPublisher} from './task-publisher.ts';import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';
import {HarnessOperationError} from './harness-port.ts';import {createHash} from 'node:crypto';

test('restart recovers prepared command by positive lookup; unknown legacy entry does not block another task',async()=>{
 const root=mkdtempSync(join(tmpdir(),'recover-publish-'));const store=new ReviewedStore(join(root,'queue.db'));let journal=new TaskPublisher(join(root,'pub.db'));
 try{
  const publications=[];let launches=0;
  for(const task of ['legacy','current']){
   const input={task_id:task,work_unit_id:'unit',worker_id:'w',source:task,worker_task:'task '+task,worker_phase:'prepare_code' as const,timeout_seconds:60};
   const p=await journal.prepare(input,{identity:()=>({threadId:'origin'}),read:async id=>({thread:{id}})});publications.push(p);
   await journal.admit(p.id,new PublisherRoutes(store),async binding=>({installed:true,binding}));
   await assert.rejects(journal.submit(p.id,async()=>{launches++;throw Error('lost response');}));
  }
  journal.close();journal=new TaskPublisher(join(root,'pub.db'));
  const recovered=await journal.recoverPending(async request=>request.task_id==='legacy'
   ?{task_id:'legacy',work_unit_id:'unit',state:'submission_uncertain',command_id:null,lookup_only:true}
   :{task_id:'current',work_unit_id:'unit',state:'running',command_id:'original-os-job'});
  assert.equal(recovered.find(row=>row.id===publications[0].id)?.state,'submission_uncertain');
  assert.equal(journal.inspect(publications[0].id).state,'prepared');
  assert.equal(journal.inspect(publications[1].id).command_id,'original-os-job');
  await journal.recoverPending(async request=>({task_id:request.task_id,work_unit_id:'unit',state:'submission_uncertain',command_id:null,lookup_only:true}),
   async(task,command)=>({task_id:task,work_unit_id:'unit',command_id:command,state:'succeeded',exit_code:0}));
  assert.equal(journal.inspect(publications[1].id).state,'finished');
  assert.equal(launches,2);
  assert.equal(launches,2);assert.equal(journal.attempts(publications[1].id).length,1);
  await journal.recoverPending(async()=>{throw Error('unavailable legacy lookup');});
  assert.equal(journal.inspect(publications[1].id).command_id,'original-os-job');
 }finally{journal.close();store.close();rmSync(root,{recursive:true,force:true});}
});

test('only exact pre-executor rejection rearms same publication; full evidence survives restart',async()=>{
 const root=mkdtempSync(join(tmpdir(),'reject-publish-'));const store=new ReviewedStore(join(root,'queue.db'));let journal=new TaskPublisher(join(root,'pub.db'));
 try{
  const input={task_id:'task',work_unit_id:'unit',worker_id:'w',source:'s',worker_task:'unicode 任務',worker_phase:'prepare_code' as const,timeout_seconds:60};
  const prepared=await journal.prepare(input,{identity:()=>({threadId:'origin'}),read:async id=>({thread:{id}})});
  await journal.admit(prepared.id,new PublisherRoutes(store),async binding=>({installed:true,binding}));
  const response={ok:false,task_id:'task',work_unit_id:'unit',error:{code:'task_capsule_conflict',detail:'complete rejection'},submission:{schema:'harness-submission/v1',outcome:'rejected',phase:'before_executor',worker_task_sha256:createHash('sha256').update(input.worker_task).digest('hex'),worker_phase:input.worker_phase,timeout_seconds:60}};
  const evidence={code:'task_capsule_conflict',response,envelope:{structuredContent:response,isError:true}};
  await assert.rejects(journal.submit(prepared.id,async()=>{throw new HarnessOperationError(evidence);}),/not_confirmed/);
  assert.equal(journal.inspect(prepared.id).state,'rejected');journal.close();journal=new TaskPublisher(join(root,'pub.db'));
  assert.deepEqual(JSON.parse(journal.inspect(prepared.id).result),evidence);
  assert.equal(journal.attempts(prepared.id).length,1);assert.equal(journal.recoverRejected(prepared.id).state,'ready');
  await assert.rejects(journal.submit(prepared.id,async()=>{throw new HarnessOperationError({...evidence,response:{...response,submission:{...response.submission,outcome:'unknown',phase:'executor_entered'}}});}),/not_confirmed/);
  assert.equal(journal.inspect(prepared.id).state,'prepared');assert.throws(()=>journal.recoverRejected(prepared.id),/authoritative/);
  await assert.rejects(journal.submit(prepared.id,async()=>{}),/uncertain/);
  journal.reconcile(prepared.id,{task_id:'task',work_unit_id:'unit',command_id:'original',state:'running'});
  assert.equal(journal.inspect(prepared.id).command_id,'original');assert.equal(journal.attempts(prepared.id).length,2);
 }finally{journal.close();store.close();rmSync(root,{recursive:true,force:true});}
});

test('legacy prepared publication rearms only with a fresh complete absence inspection and keeps its ID',async()=>{
 const root=mkdtempSync(join(tmpdir(),'legacy-recovery-')),store=new ReviewedStore(join(root,'q.db')),journal=new TaskPublisher(join(root,'p.db'));
 try{
  const input={task_id:'task',work_unit_id:'unit',worker_id:'w',source:'s',worker_task:'original task',worker_phase:'prepare_code' as const,timeout_seconds:60};
  const p=await journal.prepare(input,{identity:()=>({threadId:'original-chat'}),read:async id=>({thread:{id}})});
  await journal.admit(p.id,new PublisherRoutes(store),async binding=>({installed:true,binding}));
  await assert.rejects(journal.submit(p.id,async()=>{throw Error('old lost receipt');}));
  const proof={schema:'harness-unstarted/v1',binding:p.binding,worker_task_sha256:createHash('sha256').update(input.worker_task).digest('hex'),worker_phase:input.worker_phase,capsule_version:114,active_work_unit:null,writer_leases:0,worker_control_exists:false,worker_output_exists:false,matching_jobs:[],inspected_roots:['/declared/jobs','/declared/main/commands']};
  const receipt={task_id:'task',command_id:'fresh-inspection',state:'succeeded',exit_code:0,proof};
  await assert.rejects(journal.recoverUnstarted(p.id,async()=>({...receipt,proof:{...proof,matching_jobs:[{command_id:'existing'}]}})),/absence_not_confirmed/);
  assert.equal(journal.inspect(p.id).state,'prepared');
  const recovered=await journal.recoverUnstarted(p.id,async()=>receipt);assert.equal(recovered.id,p.id);assert.equal(recovered.state,'ready');assert.equal(recovered.command_id,null);
  await journal.submit(p.id,async()=>({task_id:'task',work_unit_id:'unit',command_id:'first-actual-worker',state:'running'}));
  await assert.rejects(journal.recoverUnstarted(p.id,async()=>receipt),/not_applicable/);
 }finally{journal.close();store.close();rmSync(root,{recursive:true,force:true});}
});
test('publisher captures origin, requires remote binding, and never resubmits uncertain launch',async()=>{
 const root=mkdtempSync(join(tmpdir(),'publish-'));const store=new ReviewedStore(join(root,'queue.db'));let publisher=new TaskPublisher(join(root,'publish.db'));
 try{
  const input={task_id:'task',work_unit_id:'unit',worker_id:'worker',source:'source',worker_task:'bounded task',worker_phase:'prepare_code' as const,timeout_seconds:60};
  const app={identity:()=>({threadId:'real-caller'}),read:async(thread:string)=>({thread:{id:thread}})};
  const p=await publisher.prepare(input,app);assert.equal(p.binding.origin_chat_id,'real-caller');
  await assert.rejects(publisher.prepare({...input,origin_chat_id:'fake'} as any,app),/unexpected/);
  await assert.rejects(publisher.prepare(input,{...app,identity:()=>({threadId:'other'})}),/conflict/);
  const routes=new PublisherRoutes(store);
  await assert.rejects(publisher.admit(p.id,routes,async()=>({installed:true,binding:{...p.binding,origin_chat_id:'wrong'}})),/not_confirmed/);
  await assert.rejects(publisher.submit(p.id,async()=>{throw Error('must not submit');}),/not_ready/);
  await publisher.admit(p.id,routes,async(binding)=>({installed:true,binding}));
  let calls=0;await assert.rejects(publisher.submit(p.id,async()=>{calls++;throw Error('lost acknowledgment');}),/lost/);
  const profile={python:'/runtime/python',runtimeRoot:'/runtime/candidate',workplace:'/workplace'};
  assert.deepEqual(publisher.resumeContexts(profile),[]);
  publisher.close();publisher=new TaskPublisher(join(root,'publish.db'));
  await assert.rejects(publisher.submit(p.id,async()=>{calls++;}),/uncertain/);assert.equal(calls,1);
  publisher.reconcile(p.id,{task_id:'task',work_unit_id:'unit',command_id:'original-command',state:'running'});
  const contexts=publisher.resumeContexts(profile);assert.equal(contexts.length,1);
  const key=execFileSync(resolve('../../.venv/Scripts/python.exe'),['-c',"from solution_agent.harness.publication_registry import key;print(key('task','unit'))"],{encoding:'utf8',windowsHide:true}).trim();
  assert.equal(contexts[0].context.mailbox,'/workplace/.solution-agent/help-mailboxes/'+key+'/queue.sqlite');
  assert.equal(contexts[0].context.previous_command_id,'original-command');assert.deepEqual(contexts[0].binding,p.binding);
  assert.throws(()=>publisher.resumeContexts({...profile,workplace:'/other'}),/migration_required/);
  assert.equal((await publisher.submit(p.id,async()=>{calls++;})).command_id,'original-command');assert.equal(calls,1);
 }finally{publisher.close();store.close();rmSync(root,{recursive:true,force:true});}
});

test('optional Markdown gate survives restart and cannot admit stale or unbound content',async()=>{
 const root=mkdtempSync(join(tmpdir(),'publish-md-'));const store=new ReviewedStore(join(root,'queue.db'));let publisher=new TaskPublisher(join(root,'publish.db'));
 try{
  const input={task_id:'task',work_unit_id:'unit',worker_id:'worker',source:'source',worker_task:'bounded task',worker_phase:'prepare_code' as const,timeout_seconds:60};
  const app={identity:()=>({threadId:'real-caller'}),read:async(thread:string)=>({thread:{id:thread}})};
  const snapshot={version:'a'.repeat(64),manifest:{'guide.md':'b'.repeat(64)}};
  const {id}=await publisher.prepare(input,app,snapshot);const routes=new PublisherRoutes(store);
  let installed=0;const install=async(binding:any,markdown:any)=>{installed++;return {installed:true,binding,markdown};};
  await assert.rejects(publisher.admit(id,routes,install),/sync_required/);
  await assert.rejects(publisher.admit(id,routes,install,async()=>({ok:true,...snapshot,version:'c'.repeat(64)})),/sync_not_confirmed/);
  assert.equal(installed,0);
  await assert.rejects(publisher.submit(id,async()=>{throw Error('must not dispatch');}),/not_ready/);
  publisher.close();publisher=new TaskPublisher(join(root,'publish.db'));
  await assert.rejects(publisher.prepare(input,app,{...snapshot,version:'c'.repeat(64)}),/conflict/);
  await assert.rejects(publisher.admit(id,routes,async binding=>({installed:true,binding}),async()=>({ok:true,...snapshot})),/markdown_binding_not_confirmed/);
  assert.equal(publisher.inspect(id).state,'queued');
  await publisher.admit(id,routes,install,async()=>({ok:true,...snapshot}));
  assert.equal(publisher.inspect(id).state,'ready');
  await publisher.submit(id,async request=>({task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'md-worker',state:'running'}));
  assert.equal(publisher.inspect(id).command_id,'md-worker');
  const profile={python:'/runtime/python',runtimeRoot:'/runtime/candidate',workplace:'/workplace'};
  const capture=publisher.resumeContexts(profile,true)[0].context;
  assert.equal(capture.verification_runner,'/runtime/candidate/solution_agent/harness/verification_runner.py');
  assert.equal(capture.verification_cwd,'/workplace');
  assert.throws(()=>publisher.resumeContexts(profile,false),/migration_required/);
 }finally{publisher.close();store.close();rmSync(root,{recursive:true,force:true});}
});

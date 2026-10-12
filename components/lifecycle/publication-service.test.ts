import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {TaskPublisher} from './task-publisher.ts';import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';
import {publishTask} from './publication-service.ts';
test('single entry pins origin and MD; replay recovers commands and never repeats maintenance or worker',async()=>{
 const root=mkdtempSync(join(tmpdir(),'publication-entry-')),journal=new TaskPublisher(join(root,'p.db')),store=new ReviewedStore(join(root,'q.db'));
 const input={task_id:'task',work_unit_id:'unit',worker_id:'w',source:'s',worker_task:'do a bounded task',worker_phase:'prepare_code' as const,timeout_seconds:60};
 let caller='origin',workerCalls=0,maintenanceCalls=0;const app={identity:()=>({threadId:caller}),read:async id=>({thread:{id}})};
 const profile={python:'/runtime/python',runtimeRoot:'/runtime',workplace:'/workplace'};
 const snapshot={version:'a'.repeat(64),manifest:{'guide.md':'b'.repeat(64)}};
 const plan={...snapshot,workplace:profile.workplace,command:'fixed-md-command'};
 let binding:any;const routes=new PublisherRoutes(store);const publish=routes.publish.bind(routes);
 routes.publish=(...args:any[])=>{binding=publish(...args as Parameters<typeof publish>);return binding;};
 const receipts=new Map<string,any>();
 const harness={maintenance:async request=>{
  maintenanceCalls++;const command='maintenance-'+maintenanceCalls;
  const payload=request.command===plan.command?{ok:true,...snapshot,directory:'/workplace/.solution-agent/md-versions/'+snapshot.version}:{installed:true,binding,markdown:snapshot};
  const receipt={ok:true,task_id:input.task_id,work_unit_id:request.work_unit_id,command_id:command,state:'succeeded',exit_code:0,maintenance:{finished:true},stdout:{preview:JSON.stringify(payload)}};receipts.set(command,receipt);return receipt;
 },recoverMaintenance:async(t,u,c)=>receipts.get(c),submit:async request=>{workerCalls++;return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original-worker',state:'running'};},recover:async(t,c)=>({task_id:t,work_unit_id:'unit',command_id:c,state:'succeeded'})};
 try{
  const first=await publishTask(journal,routes,input,app,profile,harness,{markdownPlan:plan});
  assert.equal(first.command_id,'original-worker');assert.equal(maintenanceCalls,2);assert.equal(workerCalls,1);
  caller='new-manager';const next=await publishTask(journal,routes,input,app,profile,harness,{markdownPlan:plan,existingId:first.id});
  assert.equal(next.id,first.id);assert.equal(next.state,'finished');assert.equal(binding.origin_chat_id,'origin');assert.equal(maintenanceCalls,2);assert.equal(workerCalls,1);
  await assert.rejects(publishTask(journal,routes,{...input,worker_task:'altered'},app,profile,harness,{markdownPlan:plan,existingId:first.id}),/resume_input_mismatch/);
 }finally{journal.close();store.close();rmSync(root,{recursive:true,force:true});}
});
test('maintenance uncertainty survives reopen and blocks repeat; known running command only recovers',async()=>{
 const root=mkdtempSync(join(tmpdir(),'publication-op-'));let journal=new TaskPublisher(join(root,'p.db'));let starts=0,recovers=0;
 try{
  const input={task_id:'task',work_unit_id:'unit',worker_id:'w',source:'s',worker_task:'bounded',worker_phase:'prepare_code' as const,timeout_seconds:60};
  const p=await journal.prepare(input,{identity:()=>({threadId:'origin'}),read:async id=>({thread:{id}})});
  const request={task_id:'task',work_unit_id:'maintenance',command:'fixed'};
  await assert.rejects(journal.operation(p.id,'registry',request,{start:async()=>{starts++;throw Error('lost');},recover:async()=>{throw Error('never');}}),/lost/);
  journal.close();journal=new TaskPublisher(join(root,'p.db'));
  await assert.rejects(journal.operation(p.id,'registry',request,{start:async()=>{starts++;},recover:async()=>{}}),/uncertain/);assert.equal(starts,1);
  await assert.rejects(journal.operation(p.id,'md',request,{start:async()=>{starts++;return {task_id:'task',work_unit_id:'maintenance',command_id:'original',state:'running'};},recover:async()=>{}}),/wait_or_attention/);
  const result=await journal.operation(p.id,'md',request,{start:async()=>{starts++;},recover:async command=>{recovers++;return {task_id:'task',work_unit_id:'maintenance',command_id:command,state:'succeeded',exit_code:0,maintenance:{finished:true}};}});
  assert.equal(result.command_id,'original');assert.equal(starts,2);assert.equal(recovers,1);
 }finally{journal.close();rmSync(root,{recursive:true,force:true});}
});

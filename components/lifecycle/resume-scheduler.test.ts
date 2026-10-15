import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';
import {Store,digest} from '../goal-routing-integration/store.ts';import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {ResumeController} from './resume-controller.ts';import {ResumeScheduler} from './resume-scheduler.ts';

test('one continuation fails without blocking another; original deliveries stay unchanged',async()=>{
 const root=mkdtempSync(join(tmpdir(),'isolated-resume-'));const store=new Store(join(root,'queue.sqlite'));const calls:string[]=[];
 try{
  const routes=new PublisherRoutes(store);
  const controller={plan:async(b:any)=>({id:b.task_id}),tick:async(id:string)=>{calls.push(id);if(id==='T')throw Error('task_capsule_conflict');return {id,state:'running'};}};
  const scheduler=new ResumeScheduler(store,controller as any);
  for(const task of ['T','U']){
   const binding=routes.publish(task,task,'unit','worker',{threadId:'origin'}),delivery=remoteDelivery(binding,'q');
   routes.ingest(task,{v:1,binding,request_id:'q',delivery_id:delivery,payload:{kind:'question',problem:'check',acceptance:'review'}});
   const answer=JSON.stringify({kind:'awaiting_verification',request_id:delivery});
   store.db.prepare("UPDATE events SET state='done',result=? WHERE id=?").run(answer,routes.job(delivery));
   routes.ingest(task,{v:1,kind:'help_answer_saved',binding,request_id:'q',delivery_id:delivery,answer_sha256:digest(answer)});
   scheduler.register(binding,{previous_command_id:task+'-original'});
  }
  const results=await scheduler.tick();assert.deepEqual(calls,['T','U']);
  assert.equal(results[0].state,'requires_attention');assert.equal(results[1].state,'running');
 }finally{store.close();rmSync(root,{recursive:true,force:true});}
});
test('scheduler waits for remote answer persistence then dispatches only once through real ledger',async()=>{
 const root=mkdtempSync(join(tmpdir(),'resume-schedule-'));let store=new Store(join(root,'queue.sqlite'));let submissions=0;
 const harness={recover:async(task:string,command:string)=>({task_id:task,command_id:command,state:'succeeded',writer_lease_released:true}),submit:async(request:any)=>{submissions++;return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'continuation-'+submissions,state:'succeeded'};}};
 const controller=new ResumeController(resolve(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python'),resolve('resume-rpc.py'),join(root,'resume.sqlite'),harness);
 try{
  let routes=new PublisherRoutes(store);const binding=routes.publish('sender','task','unit','worker',{threadId:'chat'});
  const delivery=remoteDelivery(binding,'q');routes.ingest('sender',{v:1,binding,request_id:'q',delivery_id:delivery,payload:{kind:'question',problem:'blocked',acceptance:'check'}});
  const answer=JSON.stringify({kind:'awaiting_verification',request_id:delivery});store.db.prepare('UPDATE events SET result=?,state=? WHERE id=?').run(answer,'done',routes.job(delivery));
  let scheduler=new ResumeScheduler(store,controller);scheduler.register(binding,{python_path:'/runtime/python',help_script:'/runtime/help.py',mailbox:'/work/mailbox.sqlite',previous_command_id:'original'});
  assert.deepEqual(await scheduler.tick(),[]);assert.equal(submissions,0);
  routes.ingest('sender',{v:1,kind:'help_answer_saved',binding,request_id:'q',delivery_id:delivery,answer_sha256:digest(answer)});
  const first=(await scheduler.tick())[0];assert.equal(first.state,'finished');assert.equal(submissions,1);
  store.close();store=new Store(join(root,'queue.sqlite'));routes=new PublisherRoutes(store);scheduler=new ResumeScheduler(store,controller);
  assert.equal((await scheduler.tick())[0].state,'finished');assert.equal(submissions,1);
  const verifyId='verify-'+first.id.slice(0,24),verifyDelivery=remoteDelivery(binding,verifyId);
  routes.ingest('sender',{v:1,binding,request_id:verifyId,delivery_id:verifyDelivery,payload:{kind:'verification',proposal_id:delivery,answer_sha256:digest(answer),pass:true,evidence:'insufficient'}});
  const rejected=JSON.stringify({kind:'retry_allowed',request_id:verifyDelivery,incident:delivery,evidence:{reason:'need exact command'}});
  store.db.prepare('UPDATE events SET result=?,state=? WHERE id=?').run(rejected,'done',routes.job(verifyDelivery));
  await scheduler.tick();assert.equal(submissions,1,'transport delivery alone must not trigger retry');
  routes.ingest('sender',{v:1,kind:'help_answer_saved',binding,request_id:verifyId,delivery_id:verifyDelivery,answer_sha256:digest(rejected)});
  const retry=(await scheduler.tick())[1];assert.equal(retry.state,'finished');assert.equal(submissions,2);
  assert.match(JSON.parse(retry.body).request.worker_task,/exact check command/);
  assert.equal(JSON.parse(retry.body).previous_command_id,'continuation-1');
  store.close();store=new Store(join(root,'queue.sqlite'));scheduler=new ResumeScheduler(store,controller);
  await scheduler.tick();assert.equal(submissions,2,'restart must not duplicate the evidence retry');
 }finally{store.close();rmSync(root,{recursive:true,force:true});}
});

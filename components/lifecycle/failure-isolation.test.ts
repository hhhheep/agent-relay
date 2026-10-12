import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {TaskPublisher} from './task-publisher.ts';import {HarnessOperationError} from './harness-port.ts';
import {publishTasksIsolated} from './publication-service.ts';
import {Store} from '../goal-routing-integration/store.ts';
import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {InboxPump} from './inbox-pump.ts';

test('failed acknowledgment and answer recipient do not prevent delivery to healthy recipient',async()=>{
 const root=mkdtempSync(join(tmpdir(),'isolated-outbox-'));const store=new Store(join(root,'queue.db'));
 try{
  const routes=new PublisherRoutes(store);const sent:string[]=[];
  const transport={send:async(source:string)=>{if(source==='T')throw Error('T offline');sent.push(source);}};
  const pump=new InboxPump(store,routes,transport);
  for(const source of ['T','U']){
   const binding=routes.publish(source,source,'unit','worker',{threadId:'origin'}),delivery=remoteDelivery(binding,'q');
   routes.ingest(source,{v:1,binding,request_id:'q',delivery_id:delivery,payload:{kind:'question',problem:'check',acceptance:'review'}});
   store.db.prepare("UPDATE events SET state='done',result=? WHERE id=?").run(JSON.stringify({kind:'awaiting_verification'}),routes.job(delivery));
   store.db.prepare('INSERT INTO help_ack_outbox(id,source,body) VALUES (?,?,?)').run(source,source,'ack');
  }
  await assert.rejects(pump.flush(),/T offline/);assert.deepEqual(sent,['U']);
  assert.equal(store.db.prepare('SELECT state FROM help_ack_outbox WHERE id=?').get('T')?.state,'pending');
  assert.equal(store.db.prepare('SELECT state FROM help_ack_outbox WHERE id=?').get('U')?.state,'sent');
  await assert.rejects(routes.resendUnconfirmed(transport,100000),/T offline/);assert.deepEqual(sent,['U','U']);
  assert.equal(store.db.prepare('SELECT COUNT(*) n FROM help_remote_answers').get()?.n,0);
 }finally{store.close();rmSync(root,{recursive:true,force:true});}
});

test('T finalization conflict retains original job; U is processed; reopen never repeats T side effect',async()=>{
 const root=mkdtempSync(join(tmpdir(),'publication-isolation-'));let journal=new TaskPublisher(join(root,'journal.db'));
 let starts=0,recovers=0;const saved:any[]=[];
 try{
  const items=[];
  for(const task of ['T','U']){
   const prepared=await journal.prepare({task_id:task,work_unit_id:'unit',worker_id:'worker',source:task,worker_task:'review',worker_phase:'prepare_code',timeout_seconds:60},
    {identity:()=>({threadId:'origin'}),read:async id=>({thread:{id}})});
   items.push({task,id:prepared.id});
  }
  const request=(task:string)=>({task_id:task,work_unit_id:'registry',command:'register'});
  const conflict=new HarnessOperationError({code:'task_capsule_conflict',response:{task_id:'T',work_unit_id:'registry',command_id:'T-original',state:'succeeded',exit_code:0}});
  const result=await publishTasksIsolated(items,async item=>journal.operation(item.id,'registry',request(item.task),{
   start:async()=>{starts++;if(item.task==='T')throw conflict;return {task_id:'U',work_unit_id:'registry',command_id:'U-original',state:'succeeded',exit_code:0,maintenance:{finished:true}};},
   recover:async()=>{recovers++;throw conflict;}
  }),(_,r)=>saved.push(r));
  assert.equal(result[0].ok,false);assert.equal(result[1].ok,true);assert.equal(saved.length,2);assert.equal(starts,2);assert.equal(recovers,1);
  journal.close();journal=new TaskPublisher(join(root,'journal.db'));
  const recovered=await journal.operation(items[0].id,'registry',request('T'),{
   start:async()=>{throw Error('duplicate launch');},recover:async id=>{recovers++;assert.equal(id,'T-original');return {task_id:'T',work_unit_id:'registry',command_id:id,state:'succeeded',exit_code:0,maintenance:{finished:true}};}
  });
  assert.equal(recovered.command_id,'T-original');assert.equal(starts,2);assert.equal(recovers,2);
 }finally{journal.close();rmSync(root,{recursive:true,force:true});}
});

test('succeeded OS command with unfinished maintenance only recovers existing ID',async()=>{
 const root=mkdtempSync(join(tmpdir(),'unfinished-finalization-'));const journal=new TaskPublisher(join(root,'journal.db'));
 try{
  const p=await journal.prepare({task_id:'T',work_unit_id:'u',worker_id:'w',source:'s',worker_task:'review',worker_phase:'prepare_code',timeout_seconds:60},
   {identity:()=>({threadId:'origin'}),read:async id=>({thread:{id}})});
  const request={task_id:'T',work_unit_id:'registry',command:'register'};let starts=0;
  await assert.rejects(journal.operation(p.id,'registry',request,{start:async()=>{starts++;return {...request,command_id:'original',state:'succeeded',exit_code:0};},recover:async()=>{throw Error('unexpected');}}));
  const result=await journal.operation(p.id,'registry',request,{start:async()=>{throw Error('duplicate');},recover:async id=>({...request,command_id:id,state:'succeeded',exit_code:0,maintenance:{finished:true}})});
  assert.equal(starts,1);assert.equal(result.command_id,'original');
 }finally{journal.close();rmSync(root,{recursive:true,force:true});}
});

import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync,readFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {Store} from '../goal-routing-integration/store.ts';
import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {RoutingService} from './routing-service.ts';
import {ReviewedStore} from './reviewed-store.ts';

test('one controller performs ingress, native Goal receipt, durable reply after restart',async()=>{
 const root=mkdtempSync(join(tmpdir(),'routing-service-'));let store=new ReviewedStore(join(root,'queue.db'));
 try{
  let routes=new PublisherRoutes(store);const b=routes.publish('remote','task','unit','worker',{threadId:'chat'});
  const envelope={v:1,binding:b,request_id:'question',delivery_id:remoteDelivery(b,'question'),payload:{kind:'question',problem:'problem',acceptance:'check'}};
  const messages=[{event_id:1,from_agent_id:'remote',body:JSON.stringify(envelope)}],sent:any[]=[];
  let failReply=false,sendCount=0,turns:any[]=[],busy=true;
  const inbox={get:async(cursor:number)=>({messages:messages.filter(m=>m.event_id>cursor),last_event_id:1}),send:async(source:string,body:string)=>{if(failReply)throw Error('offline');sent.push({source,body:JSON.parse(body)});return {ok:true};}};
  const app={read:async(thread:string)=>({thread:{id:thread,status:{type:busy?'active':'notLoaded'}},turns}),send:async(thread:string)=>{sendCount++;return {threadId:thread};}};
  let service=new RoutingService(store,routes,inbox,app,join(root,'receipts'));
  assert.equal((await service.tick()).threads[0].state,'waiting_chat_idle');assert.equal(sendCount,0);
  busy=false;assert.equal((await service.tick()).threads[0].state,'dispatched');assert.equal(sendCount,1);
  const job=store.rows()[0],episode=store.db.prepare('SELECT * FROM help_episodes').get() as any;
  assert.ok(!episode.prompt.includes('UNTRUSTED_REMOTE_DATA'));assert.ok(!episode.prompt.includes('ACTUAL create_goal'));
  const taskPath=/\[任務單\]\((.*)\)/.exec(episode.prompt)?.[1];assert.ok(taskPath);
  const task=JSON.parse(readFileSync(taskPath,'utf8'));assert.deepEqual(task.evidence,envelope.payload);
  assert.equal(task.responsePath,episode.path);assert.equal(task.objective,episode.objective);
  assert.ok(readFileSync(task.protocol,'utf8').includes('get_goal'));
  assert.equal(task.binding,undefined);assert.equal(task.receipt,undefined);assert.equal(task.remoteData,undefined);
  assert.ok(!JSON.stringify(task).includes(envelope.delivery_id));
  store.close();store=new ReviewedStore(join(root,'queue.db'));routes=new PublisherRoutes(store);service=new RoutingService(store,routes,inbox,app,join(root,'receipts'));
  assert.equal((await service.tick()).threads[0].state,'waiting_model_receipt');assert.equal(sendCount,1);
  const goal={threadId:'chat',objective:episode.objective,status:'active',createdAt:100,updatedAt:100};
  const receipt={started:{goal},after:{goal},problemResolved:false,text:JSON.stringify({decision:'propose',summary:'bounded answer',proposal:'check candidate'})};
  writeFileSync(episode.path,JSON.stringify(receipt));
  assert.equal((await service.tick()).threads[0].state,'waiting_completed_app_turn');
  turns=[{id:'actual-turn',status:'completed',items:[{type:'fileChange',changes:[{path:episode.path}]}]}];
  for(const conflict of [{threadId:'foreign'},{requestId:'foreign'},{episodeId:'foreign'},{objective:'foreign'},{turnId:'foreign'}]){
   writeFileSync(episode.path,JSON.stringify({...receipt,...conflict}));
   assert.equal((await service.tick()).threads[0].state,'attention');assert.equal(store.rows()[0].state,'running');
  }
  writeFileSync(episode.path,JSON.stringify(receipt));
  assert.equal((await service.tick()).threads[0].state,'model_completed');
  failReply=true;assert.equal((await service.tick()).threads[0].state,'attention');assert.equal(store.rows()[0].state,'reply_ready');
  failReply=false;assert.equal((await service.tick()).threads[0].state,'reply_sent');
  const answer=sent.find(x=>x.body.answer);assert.equal(answer.source,'remote');assert.equal(JSON.parse(answer.body.answer).kind,'awaiting_verification');
  assert.equal(store.rows()[0].state,'done');assert.equal(sendCount,1);
 }finally{store.close();rmSync(root,{recursive:true,force:true});}
});

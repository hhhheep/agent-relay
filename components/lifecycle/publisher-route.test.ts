import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Store,digest} from '../goal-routing-integration/store.ts';
import {NativeDispatch} from '../goal-routing-integration/native-dispatch.ts';
import {PublisherRoutes,remoteDelivery,executorCaller} from './publisher-route.ts';

const envelope=(b:any,id='q')=>({v:1,binding:b,request_id:id,delivery_id:remoteDelivery(b,id),payload:{kind:'question',problem:'same problem',acceptance:'same check'}});
test('overlapping adapters preserve original binding and share one durable native intent',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'overlap-')),path=join(dir,'state.sqlite');let first=new Store(path),second:Store|undefined;
 try{
  const routes=new PublisherRoutes(first),binding=routes.publish('source','task','unit','worker',{threadId:'original-chat'}),message=envelope(binding);
  routes.ingest('source',message);const job=routes.job(message.delivery_id);
  assert.throws(()=>new Store(path),/controller_owner_alive/);
  const next=new PublisherRoutes(first);assert.deepEqual(next.publish('source','task','unit','worker',{threadId:'original-chat'}),binding);
  assert.equal(first.db.prepare('SELECT host FROM publisher_route_hosts').get()?.host,'local');
  assert.throws(()=>next.publish('source','task','unit','worker',{threadId:'new-caller'}),/rebind/);
  let sends=0,release!:()=>void;const gate=new Promise<void>(r=>{release=r;});
  const old=new NativeDispatch(first,async thread=>{sends++;assert.equal(thread,'original-chat');await gate;return {threadId:thread};});
  const replacement=new NativeDispatch(first,async()=>{sends++;throw Error('duplicate');});
  const pending=old.dispatch(job,'one prompt');await assert.rejects(replacement.dispatch(job,'one prompt'),/uncertain_native/);release();await pending;
  assert.equal((await replacement.dispatch(job,'one prompt')).threadId,'original-chat');assert.equal(sends,1);
  assert.equal(next.job(message.delivery_id),job);assert.equal(first.rows().length,1);
 }finally{second?.close();first.close();rmSync(dir,{recursive:true,force:true});}
});
test('unconfirmed answers survive transport loss, long offline time and manager restart',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'answer-retry-'));let s=new Store(join(dir,'state.sqlite'));
 try{
  let p=new PublisherRoutes(s);const b=p.publish('worker','task','unit','worker',{threadId:'chat'}),e=envelope(b);p.ingest('worker',e);
  const result=JSON.stringify({kind:'awaiting_verification',request_id:e.delivery_id});
  s.db.prepare("UPDATE events SET result=?,state='done' WHERE id=?").run(result,p.job(e.delivery_id));
  let sent=0;const transport={send:async(source:string,body:string)=>{assert.equal(source,'worker');assert.equal(JSON.parse(body).answer,result);sent++;return {ok:true};}};
  assert.equal(await p.resendUnconfirmed(transport,100000),1);
  assert.equal(await p.resendUnconfirmed(transport,100001),0);
  s.close();s=new Store(join(dir,'state.sqlite'));p=new PublisherRoutes(s);
  assert.equal(await p.resendUnconfirmed(transport,100002),0);
  assert.equal(await p.resendUnconfirmed(transport,100000+40*86400000),1,'transport retention expiry does not lose durable answer');
  p.ingest('worker',{...e,kind:'help_answer_saved',answer_sha256:digest(result)});
  assert.equal(await p.resendUnconfirmed(transport,100000+80*86400000),0);assert.equal(sent,2);
  assert.equal(s.rows().length,1);
 }finally{s.close();rmSync(dir,{recursive:true,force:true});}
});
test('saved answer requires exact source, binding and locally recorded answer hash',()=>{
 const dir=mkdtempSync(join(tmpdir(),'saved-answer-'));const s=new Store(join(dir,'state.sqlite'));
 try{
  const p=new PublisherRoutes(s),b=p.publish('worker','task','unit','worker',{threadId:'chat'}),e=envelope(b);p.ingest('worker',e);
  const result=JSON.stringify({kind:'awaiting_verification',request_id:e.delivery_id});
  const ack={...e,kind:'help_answer_saved',answer_sha256:digest(result)};
  assert.throws(()=>p.ingest('worker',ack),/unverified_saved_answer/);
  s.db.prepare('UPDATE events SET result=? WHERE id=?').run(result,p.job(e.delivery_id));
  assert.throws(()=>p.ingest('other',ack),/untrusted/);
  assert.throws(()=>p.ingest('worker',{...ack,answer_sha256:'wrong'}),/unverified/);
  assert.equal(p.ingest('worker',ack).kind,'help_answer_confirmed');p.ingest('worker',ack);
  assert.equal(s.db.prepare('SELECT count(*) n FROM help_remote_answers').get()?.n,1);
 }finally{s.close();rmSync(dir,{recursive:true,force:true});}
});
test('32 questions with same text across four executions route to original publishers',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'publisher-'));let s=new Store(join(dir,'state.sqlite'));
 try{
  let p=new PublisherRoutes(s);const all:any[]=[];
  for(let i=0;i<4;i++){
   const b=p.publish('worker'+i,'task'+i,'unit','worker'+i,{threadId:'chat'+i%2});
   for(let n=0;n<8;n++){const e=envelope(b,'q'+n);p.ingest('worker'+i,e);p.ingest('worker'+i,e);all.push(e);}
  }
  assert.equal(s.rows().length,32);
  s.close();s=new Store(join(dir,'state.sqlite'));p=new PublisherRoutes(s);
  const sends:any[]=[];const native=new NativeDispatch(s,async(thread,prompt)=>{sends.push({thread,prompt});return {threadId:thread};});
  for(const e of all){
   assert.equal(p.ingest(e.binding.worker_id,e).durably_received,true);
   const job=p.job(e.delivery_id);await native.dispatch(job,e.delivery_id);
   native.handled(job,{deliveryId:job,threadId:e.binding.origin_chat_id,marker:e.delivery_id,observed:true});
   await assert.rejects(p.flush(native,e.delivery_id,{send:async()=>({})}),/not_answer/);
  }
  assert.equal(sends.length,32);
  sends.forEach((r,i)=>assert.equal(r.thread,all[i].binding.origin_chat_id));
 }finally{s.close();rmSync(dir,{recursive:true,force:true});}
});
test('publication and ingress fail closed on spoofing, conflicting retries and capacity',()=>{
 const dir=mkdtempSync(join(tmpdir(),'publisher-'));const s=new Store(join(dir,'state.sqlite'));
 try{
  const p=new PublisherRoutes(s),b=p.publish('source','task','unit','worker',{threadId:'original'}),e=envelope(b);
  assert.throws(()=>p.publish('source','task','unit','worker',{threadId:'wrong'}),/rebind/);
  assert.throws(()=>p.ingest('wrong',e),/untrusted/);
  assert.throws(()=>p.ingest('source',{...e,binding:{...b,origin_chat_id:'wrong'}}),/untrusted/);
  p.ingest('source',e);
  assert.throws(()=>p.ingest('source',{...e,payload:{...e.payload,problem:'changed'}}),/conflict/);
  for(let n=1;n<128;n++)p.ingest('source',envelope(b,'q'+n));
  assert.throws(()=>p.ingest('source',envelope(b,'overflow')),/capacity/);
  assert.equal(s.rows().length,128);
  assert.throws(()=>executorCaller({CODEX_THREAD_ID:'supplied-by-remote'}),/trusted/);
 }finally{s.close();rmSync(dir,{recursive:true,force:true});}
});
test('remote verification stays in original incident and returns bound review',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'publisher-'));const s=new Store(join(dir,'state.sqlite'));
 try{
  const p=new PublisherRoutes(s),b=p.publish('source','task','unit','worker',{threadId:'original'}),e=envelope(b);
  p.ingest('source',e);const first=p.job(e.delivery_id);
  s.mark(first,'running','test objective');s.ready(s.rows()[0],{text:'proposal'});
  const v={...envelope(b,'verify'),payload:{kind:'verification',proposal_id:e.delivery_id,answer_sha256:digest(s.rows()[0].result),pass:true,evidence:'actual remote check'}};
  assert.throws(()=>p.ingest('source',{...v,payload:{...v.payload,answer_sha256:'stale'}}),/verification_answer_mismatch/);
  p.ingest('source',v);const next=p.job(v.delivery_id),job=s.rows().find(r=>r.id===next);
  s.validatePhase(job);s.mark(next,'running','review objective');s.ready(job,{text:'reviewed evidence'});
  const native=new NativeDispatch(s,async()=>{throw Error('unexpected send');});
  let sent:any;await p.flush(native,v.delivery_id,{send:async(source,body)=>{sent={source,body:JSON.parse(body)};return {ok:true};}});
  assert.equal(sent.source,'source');assert.equal(sent.body.delivery_id,v.delivery_id);
  assert.equal(JSON.parse(sent.body.answer).kind,'closed');assert.equal(JSON.parse(sent.body.answer).incident,e.delivery_id);
  const other=p.publish('other','task2','unit','worker2',{threadId:'other-chat'});
  assert.throws(()=>p.ingest('other',{...envelope(other,'bad'),payload:v.payload}),/unbound_verification/);
 }finally{s.close();rmSync(dir,{recursive:true,force:true});}
});

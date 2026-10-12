import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {Store} from './store.ts';
import {NativeDispatch} from './native-dispatch.ts';
const setup=()=>{const path=join(mkdtempSync(join(tmpdir(),'native-dispatch-')),'queue.db');const store=new Store(path);store.bind('worker','original');store.bind('other','other-thread');return {store,path};};
const ingest=(s:Store,id:string,source='worker',event=1)=>{s.ingest([{from_agent_id:source,event_id:event,body:JSON.stringify({v:1,id,incident:id,kind:'request',payload:{}})}],event);return s.rows().at(-1);};
const ack=(job:any)=>({deliveryId:job.id,threadId:job.thread,marker:JSON.parse(job.body).id,observed:true});

test('native short Goal receipt, incident and outbox commit atomically',async()=>{
 const {store}=setup();const j=ingest(store,'goal-one'),turn={threadId:j.thread,turnId:'actual-turn',status:'completed'};
 const send=async(threadId:string)=>({threadId});const bridge=new NativeDispatch(store,send);
 await bridge.dispatch(j.id,'goal prompt','bounded objective');
 const goal={threadId:j.thread,objective:'bounded objective',createdAt:1,updatedAt:2};
 const receipt={threadId:j.thread,requestId:'goal-one',episodeId:j.id,objective:goal.objective,turnId:turn.turnId,started:{goal:{...goal,status:'active'}},completed:{goal:{...goal,status:'complete'}},text:'{"candidate":2}'};
 assert.throws(()=>bridge.goalHandled(j.id,{...receipt,completed:{goal:null}},turn),/explicit_app_completion_required/);
 const crash=new NativeDispatch(store,send,s=>{if(s==='before_native_goal_commit')throw Error('commit fault');});
 assert.throws(()=>crash.goalHandled(j.id,receipt,turn),/commit fault/);
 assert.equal(store.rows()[0].state,'running');assert.equal(store.db.prepare('SELECT count(*) n FROM incidents').get()!.n,0);
 assert.equal(store.db.prepare('SELECT state FROM native_dispatch WHERE job_id=?').get(j.id)!.state,'sent');
 bridge.goalHandled(j.id,receipt,turn);bridge.goalHandled(j.id,receipt,turn);
 assert.equal(JSON.parse(store.rows()[0].result).kind,'awaiting_verification');
 assert.equal(store.db.prepare('SELECT state FROM native_dispatch WHERE job_id=?').get(j.id)!.state,'handled');store.close();
});

test('native send persists identity, deduplicates after restart, and requires model acknowledgement',async()=>{
 let {store,path}=setup();const j=ingest(store,'one');let calls=0;
 const send=async(thread:string)=>{calls++;return {threadId:thread};};
 let bridge=new NativeDispatch(store,send);await bridge.dispatch(j.id,'prompt');
 await assert.rejects(bridge.flush(j.id,{}),/model_receipt_required/);
 store.close();store=new Store(path);bridge=new NativeDispatch(store,send);
 await bridge.dispatch(j.id,'prompt');assert.equal(calls,1);
 await assert.rejects(bridge.dispatch(j.id,'changed'),/dispatch_conflict/);
 assert.throws(()=>bridge.handled(j.id,{...ack(j),threadId:'wrong'}),/binding_mismatch/);
 bridge.handled(j.id,ack(j));bridge.handled(j.id,ack(j));
 let sent:any;await bridge.flush(j.id,{send:async(to:string,body:string)=>{sent={to,body};return {ok:true};}});
 assert.equal(sent.to,'worker');assert.equal(JSON.parse(sent.body).delivery_id,j.id);assert.equal(store.rows()[0].state,'done');store.close();
});
test('lost App acknowledgement never resends; correlated model receipt recovers after restart',async()=>{
 let {store,path}=setup();const j=ingest(store,'one');let calls=0;
 const send=async(thread:string)=>{calls++;return {threadId:thread};};
 await assert.rejects(new NativeDispatch(store,send,s=>{if(s==='after_native_accept')throw Error('lost_ack');}).dispatch(j.id,'prompt'),/lost_ack/);
 store.close();store=new Store(path);const bridge=new NativeDispatch(store,send);
 await assert.rejects(bridge.dispatch(j.id,'prompt'),/uncertain_native_dispatch/);assert.equal(calls,1);
 bridge.handled(j.id,ack(j));assert.equal(store.rows()[0].state,'reply_ready');store.close();
});
test('one pending native delivery per thread; another bound thread can progress',async()=>{
 const {store}=setup();const a=ingest(store,'a'),b=ingest(store,'b','worker',2),c=ingest(store,'c','other',3);
 const bridge=new NativeDispatch(store,async(thread:string)=>({threadId:thread}));
 await bridge.dispatch(a.id,'a');await assert.rejects(bridge.dispatch(b.id,'b'),/waiting_for_receipt/);
 await bridge.dispatch(c.id,'c');bridge.handled(a.id,ack(a));await bridge.dispatch(b.id,'b');store.close();
});
test('wrong App acceptance fails closed; remote outbox replay retains delivery identity',async()=>{
 const {store}=setup();const a=ingest(store,'a');
 await assert.rejects(new NativeDispatch(store,async()=>({threadId:'wrong'})).dispatch(a.id,'a'),/binding_mismatch/);
 let calls=0;const bridge=new NativeDispatch(store,async()=>{calls++;return {};});
 await assert.rejects(bridge.dispatch(a.id,'a'),/uncertain_native_dispatch/);assert.equal(calls,0);
 bridge.handled(a.id,ack(a));const bodies:string[]=[];const inbox={send:async(_:string,b:string)=>{bodies.push(b);return {};}};
 await assert.rejects(new NativeDispatch(store,async()=>({}),s=>{if(s==='after_remote_send')throw Error('lost_reply_ack');}).flush(a.id,inbox),/lost_reply_ack/);
 await bridge.flush(a.id,inbox);assert.equal(bodies.length,2);assert.equal(bodies[0],bodies[1]);store.close();
});

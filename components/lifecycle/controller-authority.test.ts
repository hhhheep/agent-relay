import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync,readFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';import {RoutingService} from './routing-service.ts';
import {initializeControllerAuthority,transferController,currentController,controllerScope,inspectControllerProgress,assertController} from './controller-authority.ts';
import {ResumeController} from './resume-controller.ts';
import {validateSessionStart} from './session-lifecycle.ts';

function fixture(){
 const root=mkdtempSync(join(tmpdir(),'controller-transfer-')),path=join(root,'queue.sqlite');
 const store=new ReviewedStore(path),routes=new PublisherRoutes(store),binding=routes.publish('worker','TASK','UNIT','remote-worker',{threadId:'old-chat'});
 initializeControllerAuthority(store.db);
 const grant={transferId:'transfer-1',taskId:'TASK',workUnitId:'UNIT',fromChat:'old-chat',toChat:'new-chat',expectedEpoch:0,authorizationRef:'fixture-human-grant'};
 const submit=()=>routes.ingest('worker',{v:1,binding,request_id:'question',delivery_id:remoteDelivery(binding,'question'),payload:{kind:'question',problem:'existing remote task',acceptance:'review results without relaunch'}});
 const inbox={get:async()=>({messages:[]}),send:async()=>({ok:true})};
 return {root,path,store,routes,binding,grant,submit,inbox,close:()=>{store.close();rmSync(root,{recursive:true,force:true});}};
}
test('new chat inspects pending while old OS manager owns lease; no reply or write needed',()=>{
 const f=fixture();try{
  f.submit();const before=f.store.rows(),owner=f.store.db.prepare("SELECT value FROM meta WHERE key='owner'").get();
  const progress=inspectControllerProgress(f.path,'TASK','UNIT');assert.equal(progress.readOnly,true);assert.equal(progress.relaunchPermitted,false);
  assert.equal(progress.events[0].id,before[0].id);assert.deepEqual(f.store.rows(),before);
  assert.deepEqual(f.store.db.prepare("SELECT value FROM meta WHERE key='owner'").get(),owner);
 }finally{f.close();}
});
test('offline old chat is not consulted; scope grant atomically transfers and retry is idempotent',()=>{
 const f=fixture();try{
  f.submit();const before=f.store.rows(),binding=JSON.stringify(f.binding);
  const receipt=transferController(f.store.db,f.grant,'new-chat',()=>true);
  assert.equal(receipt.epoch,1);assert.equal(currentController(f.store.db,controllerScope('TASK','UNIT')).chat,'new-chat');
  assert.deepEqual(transferController(f.store.db,{...f.grant},'new-chat',()=>true),receipt);
  assert.deepEqual(f.store.rows(),before);assert.equal(JSON.stringify(f.binding),binding);
  assert.throws(()=>assertController(f.store.db,receipt.scope,'old-chat',0),/stale/);
  assert.throws(()=>assertController(f.store.db,receipt.scope,'old-chat',1),/stale/);
  assertController(f.store.db,receipt.scope,'new-chat',1);
 }finally{f.close();}
});
test('unapproved target, competing epoch and transfer-id mutation fail without stealing owner',()=>{
 const f=fixture();try{
  assert.throws(()=>transferController(f.store.db,f.grant,'new-chat',()=>false),/not_authorized/);
  assert.throws(()=>transferController(f.store.db,f.grant,'foreign-chat',()=>true),/not_authorized/);
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  const other=new DatabaseSync(f.path);try{
   assert.throws(()=>transferController(other,{...f.grant,transferId:'competitor',toChat:'third-chat'},'third-chat',()=>true),/epoch_conflict/);
  }finally{other.close();}
  assert.throws(()=>transferController(f.store.db,{...f.grant,authorizationRef:'different'},'new-chat',()=>true),/id_conflict/);
  assert.equal(currentController(f.store.db,controllerScope('TASK','UNIT')).chat,'new-chat');
 }finally{f.close();}
});
test('crash inside transfer rolls back both owner and audit; reopened store retains committed owner',()=>{
 const f=fixture();try{
  assert.throws(()=>transferController(f.store.db,f.grant,'new-chat',()=>true,()=>{throw Error('crash');}),/crash/);
  assert.equal(currentController(f.store.db,controllerScope('TASK','UNIT')).epoch,0);
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM controller_transfers').get()!.n,0);
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  const read=new DatabaseSync(f.path,{readOnly:true});try{assert.equal(currentController(read,controllerScope('TASK','UNIT')).epoch,1);}finally{read.close();}
 }finally{f.close();}
});
test('queued question targets new controller and leaves original origin and event identity intact',async()=>{
 const f=fixture();const sends:any[]=[];try{
  f.submit();const original=f.store.rows()[0];
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  const app={controllerChat:()=> 'new-chat',read:async(thread:string)=>({thread:{id:thread,status:{type:'idle'}}}),send:async(thread:string,prompt:string)=>{sends.push({thread,prompt});return {threadId:thread};}};
  const service=new RoutingService(f.store,f.routes,f.inbox,app,join(f.root,'receipts'));
  assert.equal((await service.tick()).threads[0].state,'dispatched');assert.equal(sends[0].thread,'new-chat');
  const taskPath=/\[任務單\]\((.*)\)/.exec(sends[0].prompt)?.[1];assert.ok(taskPath);
  const task=JSON.parse(readFileSync(taskPath,'utf8'));assert.equal(task.schema,'harness-manager-task/v2');
  assert.equal(task.binding,undefined);assert.equal(task.receipt,undefined);
  assert.equal((f.store.db.prepare('SELECT target FROM controller_episode_targets WHERE job=?').get(original.id) as any).target,'new-chat');
  assert.equal(f.store.rows()[0].thread,'old-chat');assert.equal(f.store.rows()[0].id,original.id);
  assert.equal((f.store.db.prepare('SELECT thread FROM native_dispatch').get() as any).thread,'new-chat');
  assert.equal(inspectControllerProgress(f.path,'TASK','UNIT').authority.binding.origin_chat_id,'old-chat');
 }finally{f.close();}
});
test('stale manager never dispatches and cancelled old cached plan is archived before retargeting',async()=>{
 const f=fixture();let sends=0;try{
  f.submit();let actor='old-chat';
  const app={controllerChat:()=>actor,read:async(thread:string)=>({thread:{id:thread,status:{type:'idle'}}}),send:async(thread:string)=>{sends++;return {threadId:thread};}};
  const service=new RoutingService(f.store,f.routes,f.inbox,app,join(f.root,'receipts'));
  (service as any).episode(f.store.rows()[0]);
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  assert.equal((await service.tick()).threads[0].state,'attention');assert.equal(sends,0);assert.equal(f.store.rows()[0].state,'queued');
  actor='new-chat';assert.equal((await service.tick()).threads[0].state,'dispatched');assert.equal(sends,1);
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM controller_episode_history').get()!.n,1);
 }finally{f.close();}
});
test('late old-model receipt is retained for new-controller review without resending or closing',async()=>{
 const f=fixture();let sends=0,turns:any[]=[];try{
  f.submit();const app={controllerChat:()=> 'old-chat',read:async(thread:string)=>({thread:{id:thread,status:{type:'idle'}},turns}),send:async(thread:string)=>{sends++;return {threadId:thread};}};
  const service=new RoutingService(f.store,f.routes,f.inbox,app,join(f.root,'receipts'));
  await service.tick();const job=f.store.rows()[0],episode=f.store.db.prepare('SELECT * FROM help_episodes').get() as any;
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  writeFileSync(episode.path,JSON.stringify({text:'late unreviewed evidence'}));
  turns=[{id:'old-turn',status:'completed',items:[{type:'fileChange',changes:[{path:episode.path}]}]}];
  assert.equal((await service.tick()).threads[0].state,'handoff_receipt_requires_review');
  assert.equal(sends,1);assert.equal(f.store.rows()[0].state,'running');
  const snapshot=inspectControllerProgress(f.path,'TASK','UNIT');assert.equal(snapshot.lateReceipts.length,1);assert.equal(snapshot.events[0].id,job.id);
  assert.equal(snapshot.native[0].state,'sent');
 }finally{f.close();}
});
test('resume authority revoked after durable claim cannot submit a remote worker',async()=>{
 let submitted=0,valid=true;
 const controller=new ResumeController('unused','unused','unused',{recover:async()=>({}),submit:async()=>{submitted++;return {};}});
 controller.rpc=async(request:any)=>request.action==='inspect'?{state:'queued',task:'TASK',body:JSON.stringify({previous_command_id:'original'})}:request.action==='claim'?(valid=false,{state:'claimed',request:{task_id:'TASK'}}):{};
 await assert.rejects(controller.tick('resume',()=>{if(!valid)throw Error('controller_authority_stale');}),/stale/);assert.equal(submitted,0);
});
test('durable approved takeover admits fresh SessionStart without editing historical executor allowlist',()=>{
 const f=fixture();try{
  const config={workspace:f.root,authorizedExecutorChats:['old-chat'],manager:{stateRoot:f.root,url:'http://unused/mcp',name:'manager',team:'test',appServerPath:join(f.root,'server.mjs'),intervalMs:250,appLifecycle:{healthIntervalMs:1000,exitWhenAwaitingApp:true}}};
  const event={hook_event_name:'SessionStart',source:'resume',session_id:'new-chat',cwd:f.root};
  const env={CODEX_THREAD_ID:'new-chat',CODEX_APP_TOOLS_PIPE_PATH:'fixture-pipe',HARNESS_XATS_TOKEN:'fixture-token'};
  assert.throws(()=>validateSessionStart(config,event,env),/not_authorized/);
  transferController(f.store.db,f.grant,'new-chat',()=>true);
  assert.equal(validateSessionStart(config,event,env).state,'ready');assert.deepEqual(config.authorizedExecutorChats,['old-chat']);
  transferController(f.store.db,{...f.grant,transferId:'next',fromChat:'new-chat',toChat:'third-chat',expectedEpoch:1},'third-chat',()=>true);
  assert.throws(()=>validateSessionStart(config,event,env),/not_authorized/);
 }finally{f.close();}
});
test('new controller reviews original evidence despite unavailable old chat; uncertain original send stays untouched',async()=>{
 const f=fixture();let actor='old-chat',turns:any[]=[],sends=0;try{
  f.submit();const app={controllerChat:()=>actor,read:async(thread:string)=>{if(actor==='new-chat'&&thread==='old-chat')throw Error('old chat unavailable');return {thread:{id:thread,status:{type:'idle'}},turns};},send:async(thread:string)=>{sends++;return {threadId:thread};}};
  const service=new RoutingService(f.store,f.routes,f.inbox,app,join(f.root,'receipts'));
  await service.tick();const original=f.store.db.prepare('SELECT * FROM native_dispatch').get();
  transferController(f.store.db,f.grant,'new-chat',()=>true);actor='new-chat';
  const request=inspectControllerProgress(f.path,'TASK','UNIT').reviewRequests[0];
  const review={...request.template,evidenceRefs:['taskbook:sha256-fixture','result:sha256-fixture'],text:JSON.stringify({decision:'propose',summary:'independent review of existing result',proposal:'verification criteria for existing work'})};
  writeFileSync(request.path,JSON.stringify(review));
  assert.equal((await service.tick()).threads[0].state,'waiting_controller_review_turn');
  turns=[{id:'new-review-turn',status:'completed',items:[{type:'fileChange',changes:[{path:request.path}]}]}];
  assert.equal((await service.tick()).threads[0].state,'controller_review_completed');
  assert.deepEqual(f.store.db.prepare('SELECT * FROM native_dispatch').get(),original);assert.equal(sends,1);
  assert.equal((await service.tick()).threads[0].state,'reply_sent');assert.equal(f.store.rows()[0].state,'done');
  assert.equal(f.store.db.prepare('SELECT controller FROM controller_manual_reviews').get()!.controller,'new-chat');
 }finally{f.close();}
});
test('takeover during new-controller review rolls back closure instead of accepting stale decision',async()=>{
 const f=fixture();let actor='old-chat',reviewPath='';try{
  f.submit();const app={controllerChat:()=>actor,read:async(thread:string)=>{
   if(actor==='new-chat')transferController(f.store.db,{...f.grant,transferId:'next-review-owner',fromChat:'new-chat',toChat:'third-chat',expectedEpoch:1},'third-chat',()=>true);
   return {thread:{id:thread,status:{type:'idle'}},turns:reviewPath?[{id:'stale-turn',status:'completed',items:[{type:'fileChange',changes:[{path:reviewPath}]}]}]:[]};
  },send:async(thread:string)=>({threadId:thread})};
  const service=new RoutingService(f.store,f.routes,f.inbox,app,join(f.root,'receipts'));await service.tick();
  transferController(f.store.db,f.grant,'new-chat',()=>true);actor='new-chat';
  const request=inspectControllerProgress(f.path,'TASK','UNIT').reviewRequests[0];reviewPath=request.path;
  writeFileSync(reviewPath,JSON.stringify({...request.template,evidenceRefs:['taskbook','result'],text:JSON.stringify({decision:'human_required',summary:'stale decision'})}));
  assert.equal((await service.tick()).threads[0].state,'attention');assert.equal(f.store.rows()[0].state,'running');
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM controller_manual_reviews').get()!.n,0);
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM incidents').get()!.n,0);
 }finally{f.close();}
});

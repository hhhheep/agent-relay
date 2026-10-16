import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync,mkdirSync,readFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';
import {TaskPublisher} from './task-publisher.ts';import {HarnessPort,HarnessOperationError} from './harness-port.ts';
import {assertController,currentController,controllerScope,initializeControllerAuthority,transferController} from './controller-authority.ts';
import {runManager} from './manager-driver.ts';import {NativeAppPort} from './native-app-port.ts';
import {publishTask} from './publication-service.ts';

const sha=(value:string)=>createHash('sha256').update(value).digest('hex');
async function fixture(count=4){
 const root=mkdtempSync(join(tmpdir(),'fixed-worker-dispatch-')),store=new ReviewedStore(join(root,'queue.sqlite'));
 const routes=new PublisherRoutes(store);initializeControllerAuthority(store.db);
 const journal=new TaskPublisher(join(root,'publications.sqlite')),ids:string[]=[];let actor='chat-a';
 for(let i=0;i<count;i++){
  const item=await journal.prepare({task_id:'task-'+i,work_unit_id:'unit',worker_id:'route-worker',source:'source-'+i,
   worker_task:'approved-'+i,worker_phase:'prepare_code',timeout_seconds:30},
   {identity:()=>({threadId:'chat-a'}),read:async id=>({thread:{id}})});
  await journal.admit(item.id,routes,async binding=>({installed:true,binding}));ids.push(item.id);
 }
 const authorize=(binding:any)=>{
  const scope=controllerScope(binding.task_id,binding.work_unit_id),owner=currentController(store.db,scope);
  return ()=>assertController(store.db,scope,actor,owner.epoch);
 };
 return {root,store,routes,journal,ids,authorize,setActor:(id:string)=>{actor=id;},
  close:()=>{journal.close();store.close();rmSync(root,{recursive:true,force:true});}};
}

function busy(request:any){return new HarnessOperationError({response:{ok:false,task_id:request.task_id,work_unit_id:request.work_unit_id,
 worker_admission:{code:'worker_pool_busy',no_command_prepared:true},submission:{schema:'harness-submission/v1',outcome:'rejected',phase:'before_executor',
 worker_task_sha256:sha(request.worker_task),worker_phase:request.worker_phase,timeout_seconds:request.timeout_seconds}}});}

test('bounded dispatch reuses only proven capacity refusals; uncertain work is never submitted again',async()=>{
 const f=await fixture();const calls:string[]=[];let full=true;
 try{
  const submit=async(request:any)=>{
   calls.push(request.task_id);
   if(request.task_id==='task-0')throw Error('lost acknowledgment');
   if(request.task_id==='task-1'&&full)throw busy(request);
   return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'command-'+request.task_id,state:'running'};
  };
  const first=await f.journal.dispatchReady(3,f.authorize,submit);
  assert.equal(first.length,3);assert.equal(calls.length,3);
  assert.equal(first[0].state,'submission_uncertain');assert.equal(first[1].state,'waiting_worker');
  full=false;await f.journal.dispatchReady(3,f.authorize,submit);
  assert.equal(calls.filter(id=>id==='task-0').length,1);
  assert.equal(calls.filter(id=>id==='task-1').length,2);
  assert.equal(calls.filter(id=>id==='task-2').length,1);
  assert.equal(calls.filter(id=>id==='task-3').length,1);
  assert.equal(f.journal.inspect(f.ids[0]).state,'prepared');
 }finally{f.close();}
});

test('foreign controller is blocked before dispatch and successor continues only its transferred scope',async()=>{
 const f=await fixture(2);let sends=0;
 const submit=async(request:any)=>{sends++;return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original',state:'running'};};
 try{
  f.setActor('chat-b');
  assert.ok((await f.journal.dispatchReady(3,f.authorize,submit)).every(row=>row.state==='dispatch_requires_attention'));
  assert.equal(sends,0);
  transferController(f.store.db,{transferId:'scope-0',taskId:'task-0',workUnitId:'unit',fromChat:'chat-a',toChat:'chat-b',expectedEpoch:0,authorizationRef:'fixture-approved'},'chat-b',()=>true);
  await f.journal.dispatchReady(3,f.authorize,submit);
  assert.equal(sends,1);assert.equal(f.journal.inspect(f.ids[1]).state,'ready');
 }finally{f.close();}
});

test('capacity refusal retains original queue position ahead of later ready tasks',async()=>{
 const f=await fixture(2);const calls:string[]=[];let full=true;
 try{
  const submit=async(request:any)=>{calls.push(request.task_id);if(full)throw busy(request);return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original-'+request.task_id,state:'running'};};
  await f.journal.dispatchReady(1,f.authorize,submit);full=false;
  await f.journal.dispatchReady(1,f.authorize,submit);
  assert.deepEqual(calls,['task-0','task-0']);
  assert.equal(f.journal.inspect(f.ids[0]).state,'running');assert.equal(f.journal.inspect(f.ids[1]).state,'ready');
 }finally{f.close();}
});

test('manual rejection backlog cannot hide eligible work beyond the bounded scan',async()=>{
 const f=await fixture(130);const calls:string[]=[];
 try{
  for(const id of f.ids.slice(0,129))await assert.rejects(f.journal.submit(id,async request=>{
   const refusal=busy(request);refusal.evidence.response.worker_admission.code='worker_pool_context_changed';throw refusal;
  }));
  await f.journal.dispatchReady(1,f.authorize,async request=>{calls.push(request.task_id);return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original',state:'running'};});
  assert.deepEqual(calls,['task-129']);
  assert.ok(f.ids.slice(0,129).every(id=>f.journal.inspect(id).state==='rejected'));
 }finally{f.close();}
});

test('two dispatchers cannot submit the same admitted intent twice',async()=>{
 const f=await fixture(1);let sends=0;let release:()=>void=()=>{};
 const wait=new Promise<void>(resolve=>{release=resolve;});
 const submit=async(request:any)=>{sends++;await wait;return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original',state:'running'};};
 try{
  const first=f.journal.dispatchReady(1,f.authorize,submit);
  await f.journal.dispatchReady(1,f.authorize,submit);
  assert.equal(sends,1);release();await first;
 }finally{release();f.close();}
});

test('manual publication cannot bypass transferred epochs or revocation while awaiting registry ACK',async()=>{
 const f=await fixture(2);let actor='chat-a',starts=0,maintenance=0;
 const app={identity:()=>({threadId:actor}),read:async()=>{throw Error('old origin offline');}};
 const profile={python:'/runtime/python',runtimeRoot:'/runtime',workplace:'/workplace'};
 const input=(i:number)=>({task_id:'task-'+i,work_unit_id:'unit',worker_id:'route-worker',source:'source-'+i,worker_task:'approved-'+i,worker_phase:'prepare_code' as const,timeout_seconds:30});
 const transfer=(i:number)=>transferController(f.store.db,{transferId:'manual-'+i,taskId:'task-'+i,workUnitId:'unit',fromChat:'chat-a',toChat:'chat-b',expectedEpoch:0,authorizationRef:'fixture-approved'},'chat-b',()=>true);
 const harness={submit:async(request:any)=>{starts++;return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'original-'+request.task_id,state:'running'};},
  maintenance:async(request:any)=>{
   maintenance++;transfer(1);const binding=JSON.parse(f.journal.inspect(f.ids[1]).body).binding;
   return {task_id:request.task_id,work_unit_id:request.work_unit_id,command_id:'registry-original',state:'succeeded',exit_code:0,maintenance:{finished:true},stdout:{preview:JSON.stringify({installed:true,binding})}};
  },recover:async(task:string,command:string)=>({task_id:task,work_unit_id:'unit',command_id:command,state:'succeeded'})};
 try{
  transfer(0);
  await assert.rejects(publishTask(f.journal,f.routes,input(0),app,profile,harness,{existingId:f.ids[0]}),/authority_stale/);
  assert.equal(starts,0);assert.equal(maintenance,0);assert.equal(f.journal.inspect(f.ids[0]).state,'ready');
  f.journal.db.prepare("UPDATE publications SET state='queued',binding_receipt=NULL WHERE id=?").run(f.ids[1]);
  await assert.rejects(publishTask(f.journal,f.routes,input(1),app,profile,harness,{existingId:f.ids[1]}),/authority_stale/);
  assert.equal(starts,0);assert.equal(maintenance,1);assert.equal(f.journal.inspect(f.ids[1]).state,'ready');
  actor='chat-b';await publishTask(f.journal,f.routes,input(1),app,profile,harness,{existingId:f.ids[1]});
  assert.equal(starts,1);assert.equal(maintenance,1);
  actor='chat-a';const recovered=await publishTask(f.journal,f.routes,input(1),app,profile,harness,{existingId:f.ids[1]});
  assert.equal(recovered.command_id,'original-task-1');assert.equal(starts,1);assert.equal(recovered.state,'finished');
 }finally{f.close();}
});

test('pooled submission pins capability and fingerprint; old peer cannot silently start one-shot worker',async()=>{
 const root=mkdtempSync(join(tmpdir(),'pool-client-capability-')),config=join(root,'config.json');
 writeFileSync(config,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let supported=false;const calls:any[]=[];
 const client={connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{},...(supported?{worker_pool_fingerprint:{}}:{})}}}]}),
  callTool:async(r:any)=>{calls.push(r);return {structuredContent:{ok:true,task_id:r.arguments.task_id,work_unit_id:r.arguments.work_unit_id,command_id:'original',state:'running'}};}};
 const port=new HarnessPort(new Set(['task']),()=>client as any);
 try{
  await port.connect(config,{});assert.throws(()=>port.enableWorkerPool('a'.repeat(64)),/update_required/);assert.equal(calls.length,0);
  await port.close();supported=true;await port.connect(config,{});port.enableWorkerPool('a'.repeat(64));
  await port.submit({task_id:'task',work_unit_id:'unit',worker_task:'approved',access_mode:'writer'});
  assert.equal(calls.length,1);assert.equal(calls[0].arguments.worker_pool_fingerprint,'a'.repeat(64));
  assert.equal(calls[0].arguments.thread_id,undefined);assert.equal(calls[0].arguments.session_id,undefined);
 }finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

for(const supported of [true,false])test(`manager ${supported?'recovers completed work then assigns next':'refuses automatic dispatch to an old peer'}`,async()=>{
 const root=mkdtempSync(join(tmpdir(),'pooled-manager-')),stateRoot=join(root,'manager');mkdirSync(stateRoot);
 const journalPath=join(root,'pub.sqlite'),installed=join(root,'installed.json');
 writeFileSync(installed,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 const store=new ReviewedStore(join(stateRoot,'queue.sqlite')),routes=new PublisherRoutes(store),journal=new TaskPublisher(journalPath);
 const ids:string[]=[];let submitted=0,recovered=0;
 try{
  for(let i=0;i<2;i++){
   const p=await journal.prepare({task_id:'task-'+i,work_unit_id:'unit',worker_id:'route',source:'source-'+i,
    worker_task:'approved-'+i,worker_phase:'prepare_code',timeout_seconds:30},
    {identity:()=>({threadId:'chat'}),read:async id=>({thread:{id}})});
   await journal.admit(p.id,routes,async binding=>({installed:true,binding}));ids.push(p.id);
  }
 }finally{journal.close();store.close();}
 const config={stateRoot,url:'http://unused/mcp',name:'manager',team:'test',appServerPath:join(root,'app.mjs'),
  resume:{python:resolve(process.platform==='win32'?'../../.venv/Scripts/python.exe':'../../.venv/bin/python'),rpcScript:resolve('resume-rpc.py'),installedConfig:installed,
   publicationJournal:journalPath,workerProfile:{python:'/runtime/python',runtimeRoot:'/runtime',workplace:'/work'},
   autoDispatch:{poolFingerprint:'a'.repeat(64),releaseId:'candidate',maxPerTick:1,intervalMs:1000}}};
 const dependencies={connectInbox:async()=>({get:async()=>({messages:[],last_event_id:0}),send:async()=>({ok:true}),close:async()=>{},agentId:'fixture'}) as any,
  app:()=>new NativeAppPort((()=>({connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),
   callTool:async(r:any)=>{assert.equal(r.name,'read_thread');return {structuredContent:{thread:{id:r.arguments.threadId,hostId:'local'}}};}})) as any),
  harness:(tasks:Set<string>,releaseId?:string)=>new HarnessPort(tasks,(()=>({connect:async()=>{},close:async()=>{},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{},recover_worker:{},...(supported?{worker_pool_fingerprint:{}}:{})}}}]}),
   callTool:async(r:any)=>{
    if(r.name==='terminal_read')return {structuredContent:{server:{release_id:'candidate'}}};
    const p=r.arguments;
    if(p.command_id){recovered++;return {structuredContent:{task_id:p.task_id,work_unit_id:'unit',command_id:p.command_id,state:'succeeded'}};}
    assert.equal(p.worker_pool_fingerprint,'a'.repeat(64));submitted++;
    return {structuredContent:{task_id:p.task_id,work_unit_id:p.work_unit_id,command_id:'original-'+p.task_id,state:'running'}};
   }})) as any,releaseId)};
 try{
  const signal=new AbortController().signal,env={HARNESS_XATS_TOKEN:'fixture',CODEX_THREAD_ID:'chat',CODEX_APP_TOOLS_PIPE_PATH:'provided'};
  await runManager(config,env,signal,{once:true},dependencies);
  assert.equal(submitted,supported?1:0);
  if(supported){
   await runManager(config,env,signal,{once:true},dependencies);
   assert.equal(submitted,2);assert.equal(recovered,1);
   await runManager(config,env,signal,{once:true},dependencies);
   assert.equal(submitted,2);assert.equal(recovered,2);
  }
  const reopened=new TaskPublisher(journalPath);try{
   for(const id of ids)assert.equal(reopened.inspect(id).state,supported?'finished':'ready');
  }finally{reopened.close();}
 }finally{rmSync(root,{recursive:true,force:true});}
});

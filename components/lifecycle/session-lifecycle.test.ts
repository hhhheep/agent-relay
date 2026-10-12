import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readdirSync,readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {validateSessionStart,runSessionLifecycle} from './session-lifecycle.ts';
import {runManager} from './manager-driver.ts';import {NativeAppPort} from './native-app-port.ts';import {ReviewedStore} from './reviewed-store.ts';
import {probeSessionContext} from './session-context-probe.ts';
import {sessionAppContext,connectHookApp} from './session-app-context.ts';
const verifiedApp=()=>({connect:async()=>{},close:async()=>{}} as unknown as NativeAppPort);

test('hook context uses event caller, preserves pipe, and rejects invalid or conflicting identity',()=>{
 const f=fixture();try{
  const env={...f.env,CODEX_THREAD_ID:undefined};const ctx=sessionAppContext(f.root,f.config.authorizedExecutorChats,f.event,env);
  assert.equal(ctx.state,'ready');assert.equal(ctx.caller,f.event.session_id);assert.equal(ctx.env!.CODEX_APP_TOOLS_PIPE_PATH,env.CODEX_APP_TOOLS_PIPE_PATH);assert.equal(env.CODEX_THREAD_ID,undefined);
  assert.throws(()=>sessionAppContext(f.root,f.config.authorizedExecutorChats,{...f.event,session_id:''},env),/not_authorized/);
  assert.throws(()=>sessionAppContext(f.root,f.config.authorizedExecutorChats,f.event,{...env,CODEX_THREAD_ID:'other'}),/caller_mismatch/);
  assert.equal(sessionAppContext(f.root,f.config.authorizedExecutorChats,f.event,{...env,CODEX_APP_TOOLS_PIPE_PATH:' '}).reason,'app_pipe_not_provided');
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test('bootstrap retries transient failures, bounds attempts, and never retries identity failures',async()=>{
 let attempts=0,clock=0;const app={connect:async()=>{if(++attempts<3)throw Error('pipe_not_ready');},close:async()=>{}} as unknown as NativeAppPort;
 let result=await connectHookApp(app,'unused',{},undefined,{now:()=>clock,sleep:async ms=>{clock+=ms;}});assert.equal(result.pass,true);assert.equal(attempts,3);
 attempts=0;app.connect=async()=>{attempts++;throw Error('bootstrap_caller_mismatch');};result=await connectHookApp(app,'unused',{});assert.equal(result.reason,'bootstrap_caller_mismatch');assert.equal(attempts,1);
 attempts=0;app.connect=async()=>{attempts++;throw Error('pipe_not_ready');};result=await connectHookApp(app,'unused',{},undefined,{now:()=>clock,sleep:async ms=>{clock+=ms;}});assert.equal(result.pass,false);assert.equal(attempts,3);
 const control=new AbortController();control.abort();result=await connectHookApp(app,'unused',{},control.signal);assert.equal(result.reason,'bootstrap_cancelled');
});

test('manager is never started before hook App identity is verified',async()=>{
 const f=fixture();let runs=0;try{
  const app=()=>({connect:async()=>{throw Error('bootstrap_host_mismatch');},close:async()=>{}} as unknown as NativeAppPort);
  const result=await runSessionLifecycle(f.config,f.event,f.env,new AbortController().signal,{app,run:(async()=>{runs++;}) as any});
  assert.equal(result.state,'awaiting_app');assert.equal(result.reason,'bootstrap_host_mismatch');assert.equal(runs,0);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test('failed or cancelled bootstrap never asks a healthy owner to release',async()=>{
 const f=fixture();const control=new AbortController();let runs=0;
 try{
  mkdirSync(f.config.manager.stateRoot,{recursive:true});
  writeFileSync(join(f.config.manager.stateRoot,'status.json'),JSON.stringify({lifecycle:'active',ownerPid:123,managerGeneration:'old'}));
  const app=()=>({connect:async()=>{control.abort();},close:async()=>{}} as unknown as NativeAppPort);
  const result=await runSessionLifecycle(f.config,f.event,f.env,control.signal,{app,run:(async()=>{runs++;}) as any});
  assert.equal(result.state,'cancelled');assert.equal(runs,0);
  assert.equal(existsSync(join(f.config.manager.stateRoot,'handoff.json')),false);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

const fixture=()=>{
 const root=mkdtempSync(join(tmpdir(),'session-lifecycle-'));
 const config={workspace:root,authorizedExecutorChats:['new-real-caller'],ownerWaitMs:2000,manager:{stateRoot:join(root,'state'),url:'http://unused/mcp',name:'manager',team:'test',appServerPath:join(root,'server.mjs'),intervalMs:250,appLifecycle:{healthIntervalMs:1000,exitWhenAwaitingApp:true},appReconnect:{maxAttempts:2,delayMs:0}}};
 const event={hook_event_name:'SessionStart',source:'resume',session_id:'new-real-caller',cwd:root};
 const env={CODEX_THREAD_ID:'new-real-caller',CODEX_APP_TOOLS_PIPE_PATH:'fresh-provided-context',HARNESS_XATS_TOKEN:'test'};
 return {root,config,event,env};
};

test('invalid replacement configuration cannot release healthy owner',async()=>{
 const mutations=[{intervalMs:0},{name:''},{url:'file:///invalid'},{appReconnect:{maxAttempts:9}},{appLifecycle:{healthIntervalMs:10,exitWhenAwaitingApp:true}},
 {resume:{python:'relative',rpcScript:'relative',installedConfig:'relative'}}];
 for(const mutation of mutations){
  const f=fixture();let connects=0,runs=0;
  try{
   mkdirSync(f.config.manager.stateRoot,{recursive:true});
   const path=join(f.config.manager.stateRoot,'status.json');
   writeFileSync(path,JSON.stringify({lifecycle:'active',ownerPid:123,managerGeneration:'old'}));
   const before=readFileSync(path,'utf8');
   const app=()=>({connect:async()=>{connects++;},close:async()=>{}} as unknown as NativeAppPort);
   await assert.rejects(runSessionLifecycle({...f.config,manager:{...f.config.manager,...mutation}} as any,f.event,f.env,new AbortController().signal,{app,run:(async()=>{runs++;}) as any}),/invalid_/);
   assert.equal(connects,0);assert.equal(runs,0);assert.equal(existsSync(join(f.config.manager.stateRoot,'handoff.json')),false);
   assert.equal(readFileSync(path,'utf8'),before);
  }finally{rmSync(f.root,{recursive:true,force:true});}
 }
});
test('read-only hook probe verifies current caller and original target without sending a model turn',async()=>{
 const f=fixture();let sends=0;const app=new NativeAppPort((()=>({connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async r=>{if(r.name==='send_message_to_thread')sends++;return {structuredContent:{thread:{id:r.arguments.threadId,hostId:'local'}}};}})) as any);
 const config={workspace:f.root,authorizedExecutorChats:['new-real-caller'],appServerPath:f.config.manager.appServerPath,targets:['original-publisher'],receiptPath:join(f.root,'receipt.json')};
 try{const result=await probeSessionContext(config,f.event,{...f.env,CODEX_THREAD_ID:undefined},app);assert.equal(result.pass,true);assert.equal(result.callerSource,'session_start_event');assert.equal(result.contextPresence.threadId,false);assert.equal(result.targets[0].threadId,'original-publisher');assert.equal(sends,0);assert.equal((await probeSessionContext(config,f.event,{})).state,'awaiting_app');
  const history=join(f.root,'receipt-history'),receipts=readdirSync(history).map(name=>JSON.parse(readFileSync(join(history,name),'utf8')));assert.equal(receipts.length,2);assert.equal(receipts.filter(r=>r.pass).length,1);
 }
 finally{rmSync(f.root,{recursive:true,force:true});}
});
test('SessionStart ingress admits only authorized genuine startup/resume context',()=>{
 const f=fixture();try{
  assert.equal(validateSessionStart(f.config,f.event,f.env).state,'ready');
  const hookEnv={...f.env,CODEX_THREAD_ID:undefined};
  const admitted=validateSessionStart(f.config,f.event,hookEnv);assert.equal(admitted.state,'ready');assert.equal(admitted.env!.CODEX_THREAD_ID,f.event.session_id);assert.equal(hookEnv.CODEX_THREAD_ID,undefined);
  assert.throws(()=>validateSessionStart(f.config,f.event,{...f.env,CODEX_THREAD_ID:'different'}),/caller_mismatch/);
  assert.equal(validateSessionStart(f.config,{...f.event,source:'compact'},f.env).state,'ignored');
  assert.equal(validateSessionStart(f.config,f.event,{CODEX_THREAD_ID:'new-real-caller'}).state,'awaiting_app');
  assert.equal(validateSessionStart(f.config,f.event,{...f.env,HARNESS_XATS_TOKEN:undefined}).state,'awaiting_configuration');
  assert.throws(()=>validateSessionStart(f.config,{...f.event,session_id:'remote-supplied'},f.env),/not_authorized/);
  assert.throws(()=>validateSessionStart(f.config,{...f.event,cwd:tmpdir()},f.env),/workspace_mismatch/);
  assert.throws(()=>validateSessionStart({...f.config,authorizedExecutorChats:['other']},f.event,f.env),/not_authorized/);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
test('new lifecycle adapter waits for existing owner without changing context or lease',async()=>{
 const f=fixture();try{
  let count=0,clock=0;const result=await runSessionLifecycle(f.config,f.event,{...f.env,CODEX_THREAD_ID:undefined},new AbortController().signal,{app:verifiedApp,now:()=>clock,sleep:async ms=>{clock+=ms;},run:(async(config,env)=>{count++;assert.equal(config.stateRoot,f.config.manager.stateRoot);assert.equal(env.CODEX_THREAD_ID,'new-real-caller');if(count<3)throw Error('controller_owner_alive');}) as any});
  assert.equal(result.state,'stopped');assert.equal(count,3);assert.equal(clock,2000);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
test('owner wait is bounded, cancellation stops it, and unrelated errors are not retried',async()=>{
 const f=fixture();try{
  let clock=0,count=0;const run=(async()=>{count++;throw Error('controller_owner_alive');}) as any;
  const result=await runSessionLifecycle(f.config,f.event,f.env,new AbortController().signal,{app:verifiedApp,now:()=>clock,sleep:async ms=>{clock+=ms;},run});assert.equal(result.state,'awaiting_owner');assert.equal(count,3);
  const control=new AbortController();const stopped=await runSessionLifecycle(f.config,f.event,f.env,control.signal,{app:verifiedApp,sleep:async()=>{control.abort();},run});assert.equal(stopped.state,'cancelled');
  await assert.rejects(runSessionLifecycle(f.config,f.event,f.env,new AbortController().signal,{app:verifiedApp,run:(async()=>{throw Error('publication_profile_mismatch');}) as any}),/publication_profile_mismatch/);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
test('idle old manager detects dead App context and releases SQL owner after bounded attempts',async()=>{
 const f=fixture();let clients=0,reads=0,sends=0,clock=0;const statuses:any[]=[];
 const app=new NativeAppPort((()=>{const n=++clients;return {connect:async()=>{if(n>1)throw Error('old pipe closed');},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async r=>{if(r.name==='send_message_to_thread'){sends++;throw Error('unexpected send');}if(++reads>1)throw Error('old pipe closed');return {structuredContent:{thread:{id:r.arguments.threadId,hostId:'local'}}};}};}) as any);
 try{
  await runManager(f.config.manager,f.env,new AbortController().signal,{onStatus:s=>statuses.push(s)},{app:()=>app,connectInbox:(async()=>({agentId:'same-manager',get:async()=>({messages:[]}),send:async()=>({ok:true}),close:async()=>{}})) as any,now:()=>clock,sleep:async ms=>{clock+=ms;}});
  assert.equal(statuses.at(-1).lifecycle,'stopped');assert.equal(statuses.at(-1).appAvailability,'awaiting_app');assert.equal(clients,3);assert.equal(sends,0);
  const store=new ReviewedStore(join(f.config.manager.stateRoot,'queue.sqlite'));store.close();
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

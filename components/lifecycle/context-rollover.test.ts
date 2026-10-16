import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,dirname} from 'node:path';import {randomUUID} from 'node:crypto';import {spawnSync} from 'node:child_process';import {fileURLToPath} from 'node:url';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';import {TaskPublisher} from './task-publisher.ts';
import {NativeAppPort} from './native-app-port.ts';import {assertController,controllerScope,currentController} from './controller-authority.ts';
import {prepareContextRollover,createContextSuccessor,acceptContextSuccessor,readRollover,rolloverKey} from './context-rollover.ts';
import {validateSessionStart} from './session-lifecycle.ts';

async function fixture(){
 const root=mkdtempSync(join(tmpdir(),'context-rollover-')),old=randomUUID(),next=randomUUID(),foreign=randomUUID(),turn=randomUUID();
 const config={workspace:root,authorizedExecutorChats:[old],ownerWaitMs:500,manager:{stateRoot:root,url:'http://unused/mcp',name:'fixture',team:'fixture',appServerPath:join(root,'app.mjs'),appLifecycle:{healthIntervalMs:1000,exitWhenAwaitingApp:true},intervalMs:250},
  rollover:{authorizationRef:'user-approved-context-rollover',projectId:'fixture-project',cliPath:join(root,'session-lifecycle-cli.mjs'),configPath:join(root,'config.json')}};
 const env=(caller=old)=>({CODEX_THREAD_ID:caller,CODEX_APP_TOOLS_PIPE_PATH:'provided-by-host',HARNESS_XATS_TOKEN:'fixture'});
 const event={hook_event_name:'PreCompact',trigger:'auto',session_id:old,turn_id:turn,cwd:root};
 const journal=new TaskPublisher(join(root,'publications.sqlite')),store=new ReviewedStore(join(root,'queue.sqlite')),routes=new PublisherRoutes(store);
 const intent=await journal.prepare({task_id:'TASK',work_unit_id:'UNIT',worker_id:'worker-a',source:'source-a',worker_task:'Synthetic approved work',worker_phase:'prepare_code',timeout_seconds:30},{identity:()=>({threadId:old}),read:async id=>({thread:{id}})});
 await journal.admit(intent.id,routes,async binding=>({installed:true,binding}));
 await journal.submit(intent.id,async()=>({task_id:'TASK',work_unit_id:'UNIT',command_id:'original-command',state:'running'}));
 store.close();let creates=0,fail=false,wrong=false,readbackWrong=false;
 const app=()=>new NativeAppPort((()=>({connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'},{name:'create_thread'}]}),callTool:async r=>{
  if(r.name==='create_thread'){creates++;assert.equal(r.arguments.target.projectId,'fixture-project');assert.equal(r._meta['openai/threadId'],old);if(fail)throw Error('create ACK lost');return {structuredContent:{threadId:wrong?old:next,hostId:'local'}};}
  return {structuredContent:{thread:{id:readbackWrong&&r.arguments.threadId===next?foreign:r.arguments.threadId,hostId:'local',cwd:root}}};
 }})) as any);
 const inspect=()=>{const s=new ReviewedStore(join(root,'queue.sqlite'));try{return {checkpoint:readRollover(s.db),authority:currentController(s.db,controllerScope('TASK','UNIT'))};}finally{s.close();}};
 return {root,old,next,foreign,config,env,event,app,inspect,journal,intent,creates:()=>creates,setFail:()=>fail=true,setWrong:()=>wrong=true,setReadbackWrong:(value=true)=>readbackWrong=value,
  close:()=>{journal.close();rmSync(root,{recursive:true,force:true});}};
}

test('PreCompact checkpoint freezes old dispatch; exact App successor inherits original pending command and result',async()=>{
 const f=await fixture();try{
  const before=f.journal.inspect(f.intent.id),request=await prepareContextRollover(f.config,f.event,f.env(),f.app());assert.equal(request.state,'requested');
  const repeated=await prepareContextRollover(f.config,{...f.event,turn_id:randomUUID()},f.env(),f.app());assert.equal(repeated.id,request.id);
  const stale=new ReviewedStore(join(f.root,'queue.sqlite'));try{assert.throws(()=>assertController(stale.db,controllerScope('TASK','UNIT'),f.old,0),/rollover_pending/);}finally{stale.close();}
  assert.equal(validateSessionStart(f.config,{...f.event,hook_event_name:'SessionStart',source:'resume'},f.env()).reason,'context_rollover_pending');
  assert.deepEqual(await createContextSuccessor(f.config,request.id!,f.env(),f.app()),{state:'created',threadId:f.next});
  await assert.rejects(acceptContextSuccessor(f.config,request.id!,f.env(f.foreign),f.app()),/successor_mismatch/);
  assert.equal(f.inspect().authority.chat,f.old);
  const accepted=await acceptContextSuccessor(f.config,request.id!,f.env(f.next),f.app());assert.equal(accepted.receipts.length,1);
  assert.equal(f.inspect().authority.chat,f.next);assert.equal(f.inspect().authority.epoch,1);
  await acceptContextSuccessor(f.config,request.id!,f.env(f.next),f.app());assert.equal(f.inspect().authority.epoch,1);
  await createContextSuccessor(f.config,request.id!,f.env(),f.app());assert.equal(f.creates(),1);
  assert.deepEqual(f.journal.inspect(f.intent.id),before);
  const recovered:string[]=[];await f.journal.recoverPending(async()=>{throw Error('must recover known command');},async(task,command)=>{recovered.push(command);return {task_id:task,work_unit_id:'UNIT',command_id:command,state:'succeeded',result:{status:'success',research_acceptance:'not_assessed'}};});
  assert.deepEqual(recovered,['original-command']);assert.equal(f.journal.inspect(f.intent.id).state,'finished');
  const s=new ReviewedStore(join(f.root,'queue.sqlite'));try{assert.equal((s.db.prepare('SELECT count(*) n FROM incidents').get() as any).n,0);assert.throws(()=>assertController(s.db,controllerScope('TASK','UNIT'),f.old,0),/stale/);assertController(s.db,controllerScope('TASK','UNIT'),f.next,1);}finally{s.close();}
 }finally{f.close();}
});

test('lost create ACK is durable uncertainty across restart and never permits duplicate chat creation',async()=>{
 const f=await fixture();try{
  const request=await prepareContextRollover(f.config,f.event,f.env(),f.app());f.setFail();
  await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(),f.app()),/ACK lost/);
  assert.equal(f.inspect().checkpoint?.phase,'creating');
  await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(),f.app()),/creation_uncertain/);assert.equal(f.creates(),1);
  assert.equal(f.inspect().authority.chat,f.old);assert.equal(f.journal.inspect(f.intent.id).command_id,'original-command');
 }finally{f.close();}
});

test('wrong valid IDs, changed policies and foreign workspaces cannot create or take control',async()=>{
 const f=await fixture();try{
  assert.deepEqual(await prepareContextRollover(f.config,{...f.event,trigger:'manual'},f.env(),f.app()),{state:'ignored'});
  await assert.rejects(prepareContextRollover(f.config,{...f.event,session_id:f.foreign},f.env(),f.app()),/caller_mismatch/);
  await assert.rejects(prepareContextRollover(f.config,{...f.event,cwd:join(f.root,'foreign')},f.env(),f.app()),/workspace_mismatch/);
  await assert.rejects(prepareContextRollover(f.config,{...f.event,turn_id:'malformed'},f.env(),f.app()),/invalid_rollover_event/);
  const request=await prepareContextRollover(f.config,f.event,f.env(),f.app());
  await assert.rejects(createContextSuccessor({...f.config,rollover:{...f.config.rollover,projectId:'other'}},request.id!,f.env(),f.app()),/policy_changed/);
  await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(f.foreign),f.app()),/identity_mismatch/);assert.equal(f.creates(),0);
  f.setWrong();await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(),f.app()),/invalid_successor/);assert.equal(f.inspect().checkpoint?.phase,'creating');
 }finally{f.close();}
});

test('wrong App readback and added scopes stop transfer without adopting or relaunching work',async()=>{
 for(const change of ['readback','scope']){
  const f=await fixture();try{
   const request=await prepareContextRollover(f.config,f.event,f.env(),f.app());
   if(change==='readback'){
    f.setReadbackWrong();await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(),f.app()),/readback_mismatch/);
    assert.equal(f.inspect().checkpoint?.toChat,f.next);assert.equal(f.inspect().checkpoint?.phase,'verifying');
    f.setReadbackWrong(false);await createContextSuccessor(f.config,request.id!,f.env(),f.app());assert.equal(f.creates(),1);
   }else{
    await createContextSuccessor(f.config,request.id!,f.env(),f.app());
    const s=new ReviewedStore(join(f.root,'queue.sqlite'));try{new PublisherRoutes(s).publish('source-b','OTHER','UNIT','worker-b',{threadId:f.old});}finally{s.close();}
    await assert.rejects(acceptContextSuccessor(f.config,request.id!,f.env(f.next),f.app()),/scope_changed/);
   }
   assert.equal(f.inspect().authority.chat,f.old);assert.equal(f.journal.inspect(f.intent.id).command_id,'original-command');
  }finally{f.close();}
 }
});

test('concurrent creation resumes only the one persisted attempt, never a second chat',async()=>{
 const f=await fixture();try{
  const request=await prepareContextRollover(f.config,f.event,f.env(),f.app());
  const results=await Promise.allSettled([createContextSuccessor(f.config,request.id!,f.env(),f.app()),createContextSuccessor(f.config,request.id!,f.env(),f.app())]);
  assert.ok(results.some(r=>r.status==='fulfilled'));assert.equal(f.creates(),1);assert.equal(f.inspect().checkpoint?.toChat,f.next);
 }finally{f.close();}
});

test('corrupt checkpoint cannot unblock predecessor or silently create another successor',async()=>{
 const f=await fixture();try{
  const request=await prepareContextRollover(f.config,f.event,f.env(),f.app());
  const s=new ReviewedStore(join(f.root,'queue.sqlite'));try{s.db.prepare('UPDATE meta SET value=? WHERE key=?').run('{bad',rolloverKey);}finally{s.close();}
  await assert.rejects(createContextSuccessor(f.config,request.id!,f.env(),f.app()),/checkpoint_corrupt/);assert.equal(f.creates(),0);
 }finally{f.close();}
});

test('CLI probe and conflicting operation flags cannot accidentally create a handoff',async()=>{
 const f=await fixture();try{
  writeFileSync(f.config.rollover.configPath,JSON.stringify(f.config));
  for(const flags of [['--probe'],['--probe-app'],['--rollover-create','f'.repeat(64),'--rollover-successor','f'.repeat(64)]]){
   const result=spawnSync(process.execPath,['--import','tsx',fileURLToPath(new URL('./session-lifecycle-cli.ts',import.meta.url)),f.config.rollover.configPath,...flags],
    {cwd:dirname(fileURLToPath(import.meta.url)),input:JSON.stringify(f.event),encoding:'utf8',timeout:10000,env:{...process.env,...f.env()}});
   assert.notEqual(result.status,0);assert.match(result.stderr,/conflicting_rollover_operation/);
   const store=new ReviewedStore(join(f.root,'queue.sqlite'));try{assert.equal(readRollover(store.db),undefined);}finally{store.close();}
  }
 }finally{f.close();}
});

import test from 'node:test';import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';import {createServer} from 'node:http';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve,dirname} from 'node:path';import {fileURLToPath} from 'node:url';
import {TaskPublisher} from './task-publisher.ts';import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';
import {initializeControllerAuthority,transferControllersWhenQuiescent,assertController,controllerScope} from './controller-authority.ts';

const here=dirname(fileURLToPath(import.meta.url)),repo=resolve(here,'../..');
const python=join(repo,process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python');
function child(command:string,args:string[],input?:any){return new Promise<any>((yes,no)=>{
 const proc=spawn(command,args,{cwd:here,env:{...process.env,PYTHONPATH:repo},windowsHide:true,stdio:['pipe','pipe','pipe']});
 let stdout='',stderr='';const timer=setTimeout(()=>{proc.kill();no(Error('scenario_child_timeout'));},30000);
 proc.stdout.on('data',b=>{stdout+=b;});proc.stderr.on('data',b=>{stderr+=b;});proc.on('error',no);
 proc.on('exit',code=>{clearTimeout(timer);if(code!==0){no(Error('scenario_child_failed: '+stderr.slice(-3000)));return;}try{yes(JSON.parse(stdout));}catch{no(Error('invalid_scenario_child_output: '+stdout.slice(-500)));}});
 proc.stdin.end(input===undefined?'':JSON.stringify(input));
});}

test('three independent manager processes retain ten original assignments across handoff, ACK loss, disconnect, late result and restart', {timeout:180000},async()=>{
 const root=mkdtempSync(join(tmpdir(),'worker-handoff-scenario-')),remote=join(root,'remote');
 const rpc=(action:any)=>child(python,[join(repo,'tests/fixtures/worker_pool_scenario.py')],{root:remote,action});
 const config=await rpc({op:'configuration'}),directions:any[]=[];
 let droppedAck=false,disconnectRecovery=false,recoveriesBlocked=0;
 const calls:any[]=[],bridgeFailures:string[]=[];
 const server=createServer(async(req,res)=>{
  try{
   let body='';for await(const chunk of req)body+=chunk;
   const args=JSON.parse(body);calls.push(args);
   if(disconnectRecovery&&args.task_id==='TASK-1'&&args.command_id){disconnectRecovery=false;recoveriesBlocked++;res.writeHead(503).end();return;}
   const result=await rpc({op:'terminal_exec',arguments:args});
   if(args.task_id==='TASK-0'&&args.worker_task&&!args.recover_worker&&!droppedAck){droppedAck=true;req.socket.destroy();return;}
   res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));
  }catch(error){bridgeFailures.push(String(error));res.writeHead(500).end(String(error));}
 });
 await new Promise<void>(yes=>server.listen(0,'127.0.0.1',yes));
 const endpoint='http://127.0.0.1:'+(server.address() as any).port;
 const inspect=async()=>{const records=await rpc({op:'inspect'}),active=records.filter((r:any)=>!r.result);
  assert.ok(active.length<=3);assert.equal(new Set(active.map((r:any)=>r.request.worker_assignment.worker_id)).size,active.length);
  assert.equal(new Set(active.map((r:any)=>r.request.worker_assignment.scope_id)).size,active.length);return records;};
 const journals=()=>directions.flatMap(d=>{const journal=new TaskPublisher(d.journal);try{return d.ids.map((id:string)=>journal.inspect(id));}finally{journal.close();}});
 const tick=(d:any,caller=d.caller)=>child(process.execPath,['--import','tsx',join(repo,'tests/fixtures/worker_manager_scenario.mts'),d.config,caller,endpoint]);
 try{
  for(let direction=0;direction<3;direction++){
   const stateRoot=join(root,'direction-'+direction);mkdirSync(stateRoot);
   const journalPath=join(stateRoot,'publications.sqlite'),store=new ReviewedStore(join(stateRoot,'queue.sqlite')),routes=new PublisherRoutes(store),journal=new TaskPublisher(journalPath);
   const caller='chat-'+direction,ids:string[]=[],bindings:any[]=[];initializeControllerAuthority(store.db);
   try{for(let i=direction;i<10;i+=3){
    const item=await journal.prepare({task_id:'TASK-'+i,work_unit_id:'unit',worker_id:'publisher-'+direction,source:'source-'+i,
     worker_task:'Do only this approved synthetic assignment',worker_phase:'prepare_code',timeout_seconds:30},
     {identity:()=>({threadId:caller}),read:async id=>({thread:{id}})});
    await journal.admit(item.id,routes,async binding=>({installed:true,binding}));ids.push(item.id);bindings.push(item.binding);
   }}finally{journal.close();store.close();}
   const installed=join(stateRoot,'installed.json');writeFileSync(installed,JSON.stringify({mcpServers:{solution_agent_harness:{command:'synthetic',args:[],cwd:root}}}));
   const path=join(stateRoot,'manager.json');writeFileSync(path,JSON.stringify({stateRoot,name:'direction-'+direction,team:'synthetic',url:'http://unused/mcp',appServerPath:join(root,'synthetic-app.mjs'),
    resume:{python,rpcScript:join(here,'resume-rpc.py'),installedConfig:installed,publicationJournal:journalPath,
     workerProfile:{python:'/runtime/python',runtimeRoot:'/runtime',workplace:'/workplace'},
     autoDispatch:{poolFingerprint:config.fingerprint,releaseId:config.release,maxPerTick:1,intervalMs:1000}}}));
   directions.push({stateRoot,journal:journalPath,config:path,caller,ids,bindings});
  }
  const first=await Promise.all(directions.map(d=>tick(d)));
  assert.equal(new Set(first.map(r=>r.pid)).size,3);
  let records=await inspect();assert.equal(records.length,3);assert.ok(records.every(r=>r.leases===1));
  assert.equal(journals().filter(r=>r.state==='prepared'&&!r.command_id).length,1);
  const original=new Map(records.map(r=>[r.request.task_id,r.request.command_id]));
  const restarted=await tick(directions[0]);assert.notEqual(restarted.pid,first[0].pid);
  assert.equal((await inspect()).length,3);assert.equal(journals().find(r=>r.command_id===original.get('TASK-0'))?.state,'running');
  // Transfer only alpha after its old OS process exits. Remote jobs remain intact.
  const alpha=directions[0],grants=alpha.bindings.map((binding:any)=>({transferId:'handoff-'+binding.task_id,taskId:binding.task_id,workUnitId:binding.work_unit_id,
   fromChat:alpha.caller,toChat:'chat-alpha-next',expectedEpoch:0,authorizationRef:'synthetic-approved-scope'}));
  await transferControllersWhenQuiescent(alpha.stateRoot,grants,'chat-alpha-next',grant=>grants.some((g:any)=>JSON.stringify(g)===JSON.stringify(grant)));
  alpha.caller='chat-alpha-next';
  const before=records.map(r=>r.request.command_id).sort();await tick(alpha,'chat-0');
  assert.deepEqual((await inspect()).map(r=>r.request.command_id).sort(),before);
  const completed=new Set<string>(),threads=new Map<string,string>();
  for(const record of records){const result=await rpc({op:'complete',command_id:record.request.command_id});completed.add(record.request.command_id);threads.set(record.request.worker_assignment.scope_id,result.worker_session.thread_id);}
  disconnectRecovery=true;
  await Promise.all(directions.map(d=>tick(d)));
  assert.equal(recoveriesBlocked,1);records=await inspect();assert.equal(records.length,6);
  const pendingAlpha=records.find(r=>r.request.task_id==='TASK-3');assert.ok(pendingAlpha);
  assert.deepEqual(await rpc({op:'wrong_thread',command_id:pendingAlpha.request.command_id,foreign_thread:threads.get('beta')}),{rejected_before_codex:true});
  // Result that arrived during the disconnected recovery is consumed on restart.
  await tick(directions[1]);assert.equal((await inspect()).length,6);
  for(let round=0;round<5;round++){
   records=await inspect();
   for(const record of records.filter(r=>!completed.has(r.request.command_id))){
    const result=await rpc({op:'complete',command_id:record.request.command_id});completed.add(record.request.command_id);
    assert.equal(result.worker_session.thread_id,threads.get(record.request.worker_assignment.scope_id));
   }
   await Promise.all(directions.map(d=>tick(d)));
   if(journals().every(r=>r.state==='finished'))break;
  }
  records=await inspect();const rows=journals();
  assert.equal(records.length,10);assert.equal(completed.size,10);assert.equal(new Set(rows.map(r=>r.command_id)).size,10);
  assert.ok(rows.every(r=>r.state==='finished'));assert.ok(records.every(r=>r.leases===0&&r.active_unit===null&&r.authoritative===0));
  assert.equal(new Set(records.map(r=>r.request.worker_assignment.worker_id)).size,3);
  assert.equal(new Set(records.map(r=>r.output)).size,10);
  assert.ok(records.every(r=>r.result.task_id===r.request.task_id&&r.result.work_unit_id===r.request.work_unit_id));
  for(const row of rows){const receipt=JSON.parse(row.result);assert.equal(receipt.work_unit.research_acceptance,'not_assessed');assert.ok(receipt.artifacts.worker_result);}
  for(const [task,command] of original)assert.equal(records.find(r=>r.request.task_id===task).request.command_id,command);
  assert.equal(calls.filter(r=>r.task_id==='TASK-0'&&r.worker_task&&!r.recover_worker).length,1);
  // Existing review authority is separate from execution completion. A synthetic
  // review explicitly records a proposal and verification per original command.
  for(const d of directions){
   const store=new ReviewedStore(join(d.stateRoot,'queue.sqlite'));try{
    assert.equal(store.db.prepare('SELECT count(*) n FROM incidents').get()!.n,0);
    for(const binding of d.bindings){
     const scope=controllerScope(binding.task_id,binding.work_unit_id);assertController(store.db,scope,d.caller,d===alpha?1:0);
     const row=rows.find(r=>JSON.parse(r.body).binding.task_id===binding.task_id),receipt=JSON.parse(row.result);
     const ingest=(id:string,kind:string,payload:any)=>store.ingest([{event_id:store.rows().length+1,from_agent_id:scope,body:JSON.stringify({v:1,id,incident:receipt.command_id,kind,payload})}],store.rows().length+1);
     ingest('review-'+binding.task_id,'request',{command:receipt.command_id,artifact:receipt.artifacts.worker_result});
     const job=store.rows().at(-1);
     assert.throws(()=>store.ready(job,{text:JSON.stringify({decision:'accept',summary:'execution alone'})}),/invalid_manager_decision/);
     store.ready(job,{text:JSON.stringify({decision:'propose',summary:'review scoped synthetic output',proposal:{command:receipt.command_id,artifact:receipt.artifacts.worker_result}})});
     ingest('verify-'+binding.task_id,'verification',{proposal_id:'review-'+binding.task_id,pass:true,evidence:receipt.artifacts.worker_result});
     store.ready(store.rows().at(-1),{text:JSON.stringify({decision:'accept',summary:'explicit synthetic evidence review'})},()=>assertController(store.db,scope,d.caller,d===alpha?1:0));
    }
    assert.equal(store.db.prepare("SELECT count(*) n FROM incidents WHERE state='closed'").get()!.n,d.ids.length);
   }finally{store.close();}
  }
  assert.deepEqual(bridgeFailures,[]);
 }finally{await new Promise<void>(yes=>server.close(()=>yes()));rmSync(root,{recursive:true,force:true});}
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {HarnessPort} from './harness-port.ts';
import {runManager} from './manager-driver.ts';
import {ResumeScheduler} from './resume-scheduler.ts';

test('a pending MCP connect reserves the attempt against a second connect',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-concurrent-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});let created=0;
 const port=new HarnessPort(new Set(['task']),()=>{
  const id=++created;
  const client:any={connect:async()=>{if(id===1)await gate;},close:async()=>{client.onclose?.();},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]})};
  return client;
 });
 const first=port.connect(path,{});
 try{
  await assert.rejects(port.connect(path,{}),/harness_already_connected/);
  assert.equal(created,1);
  release();await first;assert.equal(port.connected,true);
 }finally{release();await first.catch(()=>{});await port.close();rmSync(root,{recursive:true,force:true});}
});

test('close revokes a pending MCP initialization before it can publish readiness',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-close-pending-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});let closed=0;
 const client:any={connect:async()=>{await gate;},close:async()=>{closed++;client.onclose?.();},
  listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]})};
 const port=new HarnessPort(new Set(['task']),()=>client);
 const pending=port.connect(path,{});
 try{
  await port.close();release();
  await assert.rejects(pending,/harness_reconnect_required/);
  assert.equal(port.connected,false);assert.ok(closed>=1);
 }finally{release();await pending.catch(()=>{});await port.close();rmSync(root,{recursive:true,force:true});}
});

test('a revoked old initialization cannot overwrite a fresh client or its capabilities',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-replaced-pending-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});let created=0;const calls:any[]=[];
 const port=new HarnessPort(new Set(['task']),()=>{
  const id=++created;
  const client:any={connect:async()=>{if(id===1)await gate;},close:async()=>{client.onclose?.();},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{},...(id===2?{finish_maintenance:{},recover_worker:{}}:{})}}}]}),
   callTool:async(r:any)=>{calls.push({id,request:r});return {structuredContent:{ok:true,task_id:'task',work_unit_id:'unit',command_id:'original',state:'succeeded'}};}};
  return client;
 });
 const first=port.connect(path,{});
 try{
  await port.close();await port.connect(path,{});
  release();await assert.rejects(first,/harness_reconnect_required/);
  assert.equal(port.connected,true);assert.equal(calls.length,0);
  assert.equal((await port.maintenance({task_id:'task',work_unit_id:'unit',command:'synthetic',access_mode:'writer'})).command_id,'original');
  assert.equal(calls.length,1);assert.equal(calls[0].id,2);
 }finally{release();await first.catch(()=>{});await port.close();rmSync(root,{recursive:true,force:true});}
});

test('EOF invalidates the old MCP client and never replays a command during recovery',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-eof-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 const calls:any[]=[];let generation=0;const clients:any[]=[];
 const port=new HarnessPort(new Set(['task']),()=>{
  const client:any={connect:async()=>{},close:async()=>{client.onclose?.();},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]}),
   callTool:async(request:any)=>{calls.push(request);return {structuredContent:{ok:true,task_id:'task',command_id:'original',state:'succeeded'}};}};
  generation++;clients.push(client);return client;
 });
 try{
  await port.connect(path,{});
  clients[0].onclose?.();
  await assert.rejects(port.recover('task','original'),/harness_reconnect_required/);
  assert.equal(calls.length,0);
  assert.equal(port.connected,false);
  // Explicit owner reconnect rebuilds a fresh client using the same declared config.
  await port.connect(path,{});
  assert.equal(generation,2);
  assert.equal((await port.recover('task','original')).command_id,'original');
  assert.equal(calls.length,1);
  assert.equal(calls[0].arguments.command_id,'original');
  assert.equal(calls[0].arguments.command,undefined);
  assert.equal(calls[0].arguments.worker_task,undefined);
  // A late old-generation close cannot invalidate the new session.
  clients[0].onclose?.();assert.equal(port.connected,true);
 }finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

test('a close during MCP initialization cannot publish a connected client',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-init-eof-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 const client:any={connect:async()=>{client.onclose?.();},close:async()=>{},
  listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]})};
 const port=new HarnessPort(new Set(['task']),()=>client);
 try{await assert.rejects(port.connect(path,{}),/harness_reconnect_required/);assert.equal(port.connected,false);}
 finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

test('explicit reconnect still rejects a changed expected runtime before command recovery',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-reconnect-identity-')),path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let release='expected',commands=0;const clients:any[]=[];
 const port=new HarnessPort(new Set(['task']),()=>{
  const client:any={connect:async()=>{},close:async()=>{client.onclose?.();},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]}),
   callTool:async(r:any)=>{if(r.name!=='terminal_read')commands++;return {structuredContent:{server:{release_id:release}}};}};
  clients.push(client);return client;
 },'expected');
 try{
  await port.connect(path,{});clients[0].onclose();release='wrong';
  await assert.rejects(port.connect(path,{}),/candidate_runtime_identity_mismatch/);
  assert.equal(port.connected,false);assert.equal(commands,0);
 }finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

test('Manager stops reporting resume ready and stops scheduler calls after Harness disconnect',async t=>{
 const root=mkdtempSync(join(tmpdir(),'manager-harness-eof-')),control=new AbortController();
 const states:any[]=[];let port:any,ticks=0;
 t.mock.method(HarnessPort.prototype,'connect',async function(this:any){port=this;this.client={close:async()=>{}};});
 t.mock.method(ResumeScheduler.prototype,'tick',async()=>{ticks++;return [];});
 const config={stateRoot:join(root,'state'),url:'http://unused/mcp',name:'manager',team:'synthetic',appServerPath:join(root,'app.mjs'),intervalMs:250,
  resume:{python:join(root,'python'),rpcScript:join(root,'rpc.py'),installedConfig:join(root,'config.json')}};
 const app:any={connect:async()=>{},close:async()=>{},identity:()=>({threadId:'original-chat'}),canReconnect:()=>false,diagnostics:()=>({})};
 try{
  await runManager(config,{HARNESS_XATS_TOKEN:'synthetic'},control.signal,{onStatus:s=>{
   if(s.lifecycle!=='active')return;states.push(s);
   if(states.length===1)port.client=undefined;else control.abort();
  }},{app:()=>app,connectInbox:(async()=>({agentId:'synthetic',get:async()=>({messages:[]}),send:async()=>({ok:true}),close:async()=>{}})) as any,sleep:async()=>{}});
  assert.equal(states[0].resumeState,'ready');
  assert.equal(states[1].resumeState,'harness_reconnect_required');
  assert.equal(ticks,1);
  assert.equal(states[1].pending,0);
 }finally{rmSync(root,{recursive:true,force:true});}
});


test('managed startup allowance reaches the existing SDK handshake without opening another client',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-admission-timeout-')),path=join(root,'config.json');
 let clients=0;const timeouts:any[]=[];
 const port=new HarnessPort(new Set(['task']),()=>{
  clients++;
  return {connect:async(_transport:any,options:any)=>{timeouts.push(options?.timeout);},close:async()=>{},
   listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]})} as any;
 });
 try{
  const config={command:'unused',args:[],cwd:root,startup_timeout_sec:90};
  writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:config}}));
  await port.connect(path,{});assert.deepEqual(timeouts,[90000]);assert.equal(clients,1);await port.close();
  for(const value of [0,-1,'90',3000000]){
   writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{...config,startup_timeout_sec:value}}}));
   await assert.rejects(port.connect(path,{}),/invalid_harness_startup_timeout/);
  }
  assert.equal(clients,1);
 }finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

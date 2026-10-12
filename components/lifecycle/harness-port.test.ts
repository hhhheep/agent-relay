import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {HarnessPort} from './harness-port.ts';
test('Harness retains entire refusal envelope and clears stale failure before next call',async()=>{
 const root=mkdtempSync(join(tmpdir(),'full-refusal-'));const path=join(root,'config.json');writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 let fail=true;const response={ok:false,task_id:'task',work_unit_id:'unit',error:{code:'conflict',details:{nested:'keep all'}},server:{release_id:'candidate'},submission:{outcome:'rejected'}};
 const client={connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]}),callTool:async()=>fail?{structuredContent:response,isError:true}:{structuredContent:{ok:true,task_id:'task',command_id:'original',state:'succeeded'}}};
 const port=new HarnessPort(new Set(['task']),()=>client as any);
 try{await port.connect(path,{});await assert.rejects(port.recover('task','original'),/conflict/);assert.deepEqual(port.lastFailure.response,response);assert.deepEqual(port.lastFailure.envelope.structuredContent,response);fail=false;await port.recover('task','original');assert.equal(port.lastFailure,undefined);}
 finally{await port.close();rmSync(root,{recursive:true,force:true});}
});
test('installed Harness adapter enforces task allowlist and command receipt identity',async()=>{
 const root=mkdtempSync(join(tmpdir(),'harness-port-'));const path=join(root,'config.json');
 writeFileSync(path,JSON.stringify({mcpServers:{solution_agent_harness:{command:'unused',args:[],cwd:root}}}));
 const calls:any[]=[];let wrong=false;
 const client={connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{}}}}]}),callTool:async(request:any)=>{calls.push(request);return {structuredContent:{ok:true,task_id:request.arguments.task_id,command_id:wrong?'wrong':request.arguments.command_id,state:'succeeded'}};}};
 const port=new HarnessPort(new Set(['task']),()=>client as any);
 try{
  await port.connect(path,{});assert.equal((await port.recover('task','original')).command_id,'original');
  await assert.rejects(port.recover('other','original'),/not_authorized/);assert.equal(calls.length,1);
  wrong=true;await assert.rejects(port.recover('task','original'),/identity_mismatch/);
  assert.throws(()=>port.submit({task_id:'task',command:'codex exec'}),/invalid_worker/);
 }finally{await port.close();rmSync(root,{recursive:true,force:true});}
});

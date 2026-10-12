import test from 'node:test';import assert from 'node:assert/strict';
import {NativeAppPort} from './native-app-port.ts';

test('closing during handshake cancels it and prevents late connection adoption',async()=>{
 let release!:()=>void,closed=0;const gate=new Promise<void>(r=>{release=r;});
 const port=new NativeAppPort((()=>({connect:async()=>gate,close:async()=>{closed++;},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async r=>({structuredContent:{thread:{id:r.arguments.threadId}}})})) as any);
 const pending=port.connect('/installed/server.mjs',{CODEX_THREAD_ID:'caller',CODEX_APP_TOOLS_PIPE_PATH:'provided'});
 const rejected=assert.rejects(pending,/cancelled/);await port.close();release();await rejected;
 assert.equal(closed,1);assert.equal(port.canReconnect(),false);assert.throws(()=>port.identity(),/rebootstrap/);
});
test('same genuine context reconnect is serialized and never retries an uncertain send',async()=>{
 let clients=0,failedSend=false;const calls:any[]=[];
 const factory=()=>{clients++;return {connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async r=>{
  calls.push(r);if(r.name==='send_message_to_thread'){failedSend=true;throw Error('ack lost after acceptance');}
  return {structuredContent:{thread:{id:r.arguments.threadId,hostId:'local'}}};
 }};};
 const env={CODEX_THREAD_ID:'genuine-caller',CODEX_APP_TOOLS_PIPE_PATH:'same-provided-context'};
 const port=new NativeAppPort(factory as any);await port.connect('/installed/server.mjs',env);
 await assert.rejects(port.send('original-publisher','one prompt'),/ack lost/);assert.ok(failedSend);assert.equal(port.canReconnect(),true);
 env.CODEX_APP_TOOLS_PIPE_PATH='external-mutation-must-not-be-adopted';
 await Promise.all([port.reconnect(),port.reconnect()]);assert.equal(clients,2);assert.equal(port.diagnostics().generation,2);
 assert.equal((await port.read('original-publisher')).thread.id,'original-publisher');
 assert.equal(calls.filter(r=>r.name==='send_message_to_thread').length,1);
 assert.ok(calls.every(r=>r._meta['openai/threadId']==='genuine-caller'));await port.close();
 await assert.rejects(port.reconnect(),/new_trusted/);
});
test('identity drift terminates same-context retries until a genuinely new bootstrap',async()=>{
 let corrupt=false;let clients=0;const factory=()=>{clients++;return {connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async r=>({structuredContent:{thread:{id:corrupt?'wrong-caller':r.arguments.threadId}}})};};
 const port=new NativeAppPort(factory as any),env={CODEX_THREAD_ID:'caller',CODEX_APP_TOOLS_PIPE_PATH:'provided'};
 await port.connect('/installed/server.mjs',env);
 // A transport failure, not an explicit App error, permits a fresh handshake.
 (port as any).client.callTool=async()=>{throw Error('transport closed');};await assert.rejects(port.read('target'));
 corrupt=true;await assert.rejects(port.reconnect(),/caller_mismatch/);assert.equal(port.canReconnect(),false);
 await assert.rejects(port.reconnect(),/new_trusted/);assert.equal(clients,2);
 corrupt=false;await port.connect('/installed/server.mjs',{CODEX_THREAD_ID:'new-real-caller',CODEX_APP_TOOLS_PIPE_PATH:'new-provided'});
 assert.equal((await port.read('original-target')).thread.id,'original-target');assert.equal(port.identity().threadId,'new-real-caller');await port.close();
});
test('broken App pipe requires trusted rebootstrap, not guessed reconnect',async()=>{
 let fail=false,closed=0;const calls:any[]=[];
 const fake=()=>({connect:async()=>{},close:async()=>{closed++;},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async(request:any)=>{
  calls.push(request);if(fail)throw Error('pipe closed');
  return {structuredContent:request.name==='read_thread'?{thread:{id:request.arguments.threadId}}:{threadId:request.arguments.threadId}};
 }});
 const port=new NativeAppPort(fake as any);
 await assert.rejects(port.connect('/installed/server.mjs',{CODEX_THREAD_ID:'caller'}),/trusted/);
 await port.connect('/installed/server.mjs',{CODEX_THREAD_ID:'caller',CODEX_APP_TOOLS_PIPE_PATH:'provided-pipe'});
 await port.send('bound-target','prompt');assert.equal(calls.at(-1)._meta['openai/threadId'],'caller');
 fail=true;await assert.rejects(port.read('bound-target'),/pipe closed/);
 await assert.rejects(port.send('bound-target','do not resend'),/rebootstrap/);assert.equal(closed,1);
 fail=false;await port.connect('/installed/server.mjs',{CODEX_THREAD_ID:'caller',CODEX_APP_TOOLS_PIPE_PATH:'new-provided-pipe'});
 assert.equal((await port.read('bound-target')).thread.id,'bound-target');await port.close();
});

import test from 'node:test';import assert from 'node:assert/strict';import {createServer} from 'node:http';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {notifyAcceptance} from './session-acceptance-notify.ts';
test('temporary manager handoff is loopback-only, caller-bound, bounded and does not save pipe',async()=>{
 const root=mkdtempSync(join(tmpdir(),'a3-notify-')),path=join(root,'descriptor.json'),nonce='a'.repeat(64);let received:any;
 const broker=createServer(async(req,res)=>{assert.equal(req.headers.authorization,'Bearer '+nonce);let body='';for await(const chunk of req)body+=chunk;received=JSON.parse(body);res.writeHead(202);res.end();});
 await new Promise<void>(yes=>broker.listen(0,'127.0.0.1',yes));
 const descriptor={schema:'a3-restart-supervisor/v1',workspace:root,caller:'caller',port:(broker.address() as any).port,nonce,expiresAt:Date.now()+10000};
 const event={hook_event_name:'SessionStart',source:'resume',session_id:'caller',cwd:root};
 try{
  writeFileSync(path,JSON.stringify(descriptor));assert.equal((await notifyAcceptance(root,path,event,'genuine-in-memory-pipe')).state,'submitted');assert.equal(received.pipe,'genuine-in-memory-pipe');
  await assert.rejects(notifyAcceptance(root,path,{...event,session_id:'other'},'pipe'),/invalid_acceptance_descriptor/);
  writeFileSync(path,JSON.stringify({...descriptor,authorizedExecutorChats:['caller','original-manager']}));assert.equal((await notifyAcceptance(root,path,{...event,session_id:'original-manager'},'second-genuine-pipe')).state,'submitted');assert.equal(received.event.session_id,'original-manager');
  writeFileSync(path,JSON.stringify({...descriptor,expiresAt:0}));await assert.rejects(notifyAcceptance(root,path,event,'pipe'),/invalid_acceptance_descriptor/);
  await assert.rejects(notifyAcceptance(root,join(root,'..','untrusted.json'),event,'pipe'),/outside_workspace/);
 }finally{await new Promise<void>(yes=>broker.close(()=>yes()));rmSync(root,{recursive:true,force:true});}
});

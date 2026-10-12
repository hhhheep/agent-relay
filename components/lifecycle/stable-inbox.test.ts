import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';import {randomBytes} from 'node:crypto';
import {startServer} from '../xats-routing-lab/upstream/src/daemon/server.ts';
import {connectStableInbox} from './stable-inbox.ts';
import {Store} from '../goal-routing-integration/store.ts';
import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {InboxPump} from './inbox-pump.ts';

test('changed tunnel endpoint reconnects same ID; foreign service cannot migrate durable identity',async()=>{
 const root=mkdtempSync(join(tmpdir(),'moved-inbox-')),token=randomBytes(32).toString('hex');
 let server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});let inbox:any,foreign:any;
 const path=join(root,'worker.sqlite');
 try{
  const originalURL=`http://127.0.0.1:${server.port}/mcp`;
  inbox=await connectStableInbox(path,{url:originalURL,token,name:'worker',team:'same'});const original=inbox.agentId;await inbox.close();inbox=null;
  await server.app.close();server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});
  inbox=await connectStableInbox(path,{url:`http://127.0.0.1:${server.port}/mcp`,endpointIdentity:'declared-service',token,name:'worker',team:'same'});
  assert.equal(inbox.agentId,original);await inbox.close();inbox=null;
  foreign=await startServer({dbPath:join(root,'foreign.sqlite'),host:'127.0.0.1',port:0,token});
  await assert.rejects(connectStableInbox(path,{url:`http://127.0.0.1:${foreign.port}/mcp`,endpointIdentity:'declared-service',token,name:'worker',team:'same'}));
  inbox=await connectStableInbox(path,{url:`http://127.0.0.1:${server.port}/mcp`,endpointIdentity:'declared-service',token,name:'worker',team:'same'});assert.equal(inbox.agentId,original);
 }finally{await inbox?.close();await foreign?.app.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});

test('actual daemon restart preserves bridge identities and messages sent while manager is offline',async()=>{
 const root=mkdtempSync(join(tmpdir(),'stable-inbox-')),token=randomBytes(32).toString('hex');
 let server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});
 const port=server.port,url=`http://127.0.0.1:${port}/mcp`;let manager:any,worker:any;
 const connect=(name:string)=>connectStableInbox(join(root,name+'.sqlite'),{url,token,name,team:'isolated-restart'});
 try{
  manager=await connect('manager');worker=await connect('worker');
  const managerId=manager.agentId,workerId=worker.agentId;
  await manager.close();manager=null;
  await worker.send(managerId,'offline-question');await worker.close();worker=null;
  await server.app.close();server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port,token});
  manager=await connect('manager');worker=await connect('worker');
  assert.equal(manager.agentId,managerId);assert.equal(worker.agentId,workerId);
  const box=await manager.get(0);assert.equal(box.messages.length,1);
  assert.equal(box.messages[0].from_agent_id,workerId);assert.equal(box.messages[0].body,'offline-question');
  await manager.send(workerId,'restored-reply');assert.equal((await worker.get(0)).messages[0].body,'restored-reply');
  await assert.rejects(connectStableInbox(join(root,'manager.sqlite'),{url,token,name:'other',team:'isolated-restart'}),/binding_changed/);
 }finally{await manager?.close();await worker?.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});

test('120 queued questions span inbox pages and retain publisher binding across reconnect',async()=>{
 const root=mkdtempSync(join(tmpdir(),'inbox-pages-')),token=randomBytes(32).toString('hex');
 const server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});
 const url=`http://127.0.0.1:${server.port}/mcp`;let manager:any;const clients:any[]=[];let store=new Store(join(root,'queue.sqlite'));
 const connect=(name:string)=>connectStableInbox(join(root,name+'.sqlite'),{url,token,name,team:'isolated-pages'});
 try{
  manager=await connect('manager');const managerId=manager.agentId;
  const routes=new PublisherRoutes(store);
  for(let source=0;source<2;source++){
   const worker=await connect('worker'+source);clients.push(worker);
   const binding=routes.publish(worker.agentId,'task'+source,'unit','worker'+source,{threadId:'chat'+source});
   for(let n=0;n<60;n++)await worker.send(managerId,JSON.stringify({v:1,binding,request_id:'q'+n,delivery_id:remoteDelivery(binding,'q'+n),payload:{kind:'question',problem:'same text',acceptance:'verify original source'}}));
  }
  let pump=new InboxPump(store,routes,manager);
  assert.equal((await pump.ingest()).count,100);assert.equal(store.rows().length,100);
  const cursor=pump.cursor();await manager.close();manager=null;store.close();store=new Store(join(root,'queue.sqlite'));
  manager=await connect('manager');assert.equal(manager.agentId,managerId);
  pump=new InboxPump(store,new PublisherRoutes(store),manager);assert.equal(pump.cursor(),cursor);
  assert.equal((await pump.ingest()).count,20);assert.equal(store.rows().length,120);
  assert.equal((await pump.ingest()).count,0);await pump.flush();
  for(let source=0;source<2;source++){
   const messages=(await clients[source].get(0)).messages;assert.equal(messages.length,60);
   assert.ok(messages.every((m:any)=>JSON.parse(m.body).origin_chat_id==='chat'+source));
  }
 }finally{await manager?.close();for(const client of clients)await client.close();store.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});

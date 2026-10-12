import test from 'node:test';import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,readFileSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';import {randomBytes} from 'node:crypto';
import {startServer} from '../xats-routing-lab/upstream/src/daemon/server.ts';
import {connectStableInbox} from './stable-inbox.ts';import {runManager} from './manager-driver.ts';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {NativeAppPort} from './native-app-port.ts';

test('remote inbox outage preserves queued work and reconnects the same durable identity after server restart',async()=>{
 const root=mkdtempSync(join(tmpdir(),'manager-remote-outage-')),token=randomBytes(32).toString('hex');
 const dbPath=join(root,'xats.sqlite');let server=await startServer({dbPath,host:'127.0.0.1',port:0,token}),closed=false;
 const port=server.port;
 const config={stateRoot:join(root,'manager'),url:`http://127.0.0.1:${port}/mcp`,name:'manager',team:'outage-test',appServerPath:join(root,'unused.mjs')};
 const env={HARNESS_XATS_TOKEN:token},signal=new AbortController().signal;
 const status=()=>JSON.parse(readFileSync(join(config.stateRoot,'status.json'),'utf8'));
 try{
  await runManager(config,env,signal,{once:true});const identity=status().agentId;
  let store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));
  const routes=new PublisherRoutes(store),binding=routes.publish('remote-worker','task','unit','worker',{threadId:'original-chat'});
  const envelope={v:1,binding,request_id:'same-question',delivery_id:remoteDelivery(binding,'same-question'),payload:{kind:'question',problem:'blocked',acceptance:'proof'}};
  routes.ingest('remote-worker',envelope);const original=store.rows()[0];store.close();
  await server.app.close();closed=true;
  await runManager(config,env,signal,{once:true});
  assert.equal(status().pending,1);assert.equal(status().connectionFailures,1);
  assert.equal(status().appAvailability,'awaiting_app');
  store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));
  assert.equal(store.rows().length,1);assert.equal(store.rows()[0].state,'queued');
  assert.equal(store.rows()[0].id,original.id);assert.equal(store.rows()[0].thread,'original-chat');store.close();
  server=await startServer({dbPath,host:'127.0.0.1',port,token});closed=false;
  await runManager(config,env,signal,{once:true});
  assert.equal(status().agentId,identity);assert.equal(status().connectionFailures,0);assert.equal(status().pending,1);
  store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));
  new PublisherRoutes(store).ingest('remote-worker',envelope);
  assert.equal(store.rows().length,1);assert.equal(store.rows()[0].id,original.id);
  assert.equal(store.rows()[0].state,'queued');store.close();
 }finally{if(!closed)await server.app.close();rmSync(root,{recursive:true,force:true});}
});

for(const recover of [true,false])test(`same-context outage ${recover?'recovers':'exhausts bounded retry'} while retaining original queued work`,async()=>{
 const root=mkdtempSync(join(tmpdir(),'manager-a2-')),stateRoot=join(root,'manager');
 const config={stateRoot,url:'http://unused/mcp',name:'manager',team:'test',appServerPath:join(root,'server.mjs'),intervalMs:250,appReconnect:{maxAttempts:2,delayMs:0}};
 mkdirSync(stateRoot);const db=join(stateRoot,'queue.sqlite');let store=new ReviewedStore(db);
 const routes=new PublisherRoutes(store),binding=routes.publish('source','task','unit','worker',{threadId:'original-chat'});
 routes.ingest('source',{v:1,binding,request_id:'q',delivery_id:remoteDelivery(binding,'q'),payload:{kind:'question',problem:'blocked',acceptance:'proof'}});store.close();
 let clients=0,sends=0,clock=0;const states:any[]=[];const control=new AbortController();
 const app=new NativeAppPort((()=>{const n=++clients;return {connect:async()=>{if(n>1&&!recover)throw Error('offline');},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async(r:any)=>{
  if(r.name==='send_message_to_thread'){sends++;throw Error('unexpected send');}
  if(n===1&&r.arguments.threadId==='original-chat')throw Error('transport lost');
  return {structuredContent:{thread:{id:r.arguments.threadId,hostId:'local',status:{type:'running'}}}};
 }};}) as any);
 try{
  await runManager(config,{HARNESS_XATS_TOKEN:'test',CODEX_THREAD_ID:'genuine-caller',CODEX_APP_TOOLS_PIPE_PATH:'provided'},control.signal,{onStatus:s=>{if(s.lifecycle==='active'){states.push(s);if(s.appReconnect.reason===(recover?'same_context_verified':'same_context_retry_budget_exhausted'))control.abort();}}},{app:()=>app,connectInbox:(async()=>({agentId:'manager',get:async()=>({messages:[]}),send:async()=>({ok:true}),close:async()=>{}})) as any,now:()=>clock,sleep:async ms=>{clock+=ms;}});
  assert.equal(states[0].appAvailability,'reconnect_pending');assert.equal(states.at(-1).appAvailability,recover?'online':'awaiting_app');
  assert.equal(clients,recover?2:3);assert.equal(sends,0);assert.equal(states.at(-1).pending,1);
  store=new ReviewedStore(db);assert.equal(store.rows()[0].thread,'original-chat');assert.equal(store.rows()[0].state,'queued');store.close();
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('driver without App context continues durable intake and recovers same identity on restart',async()=>{
 const root=mkdtempSync(join(tmpdir(),'manager-driver-')),token=randomBytes(32).toString('hex');
 const server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});let worker:any;
 const config={stateRoot:join(root,'manager'),url:`http://127.0.0.1:${server.port}/mcp`,name:'manager',team:'driver-test',appServerPath:join(root,'unused-app-server.mjs')};
 const env={HARNESS_XATS_TOKEN:token};const signal=new AbortController().signal;
 const status=()=>JSON.parse(readFileSync(join(config.stateRoot,'status.json'),'utf8'));
 try{
  await runManager(config,env,signal,{once:true});const initial=status();assert.equal(initial.state,'app_rebootstrap_required');assert.equal(initial.lifecycle,'stopped');assert.equal(initial.transport,'disconnected');
  worker=await connectStableInbox(join(root,'worker.sqlite'),{url:config.url,token,name:'worker',team:config.team});
  let store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));
  const binding=new PublisherRoutes(store).publish(worker.agentId,'task','unit','worker',{threadId:'original-chat'});store.close();
  const message={v:1,binding,request_id:'question',delivery_id:remoteDelivery(binding,'question'),payload:{kind:'question',problem:'blocked',acceptance:'real check'}};
  await worker.send(initial.agentId,JSON.stringify(message));
  await runManager(config,env,signal,{once:true});const next=status();
  assert.equal(next.agentId,initial.agentId);assert.equal(next.state,'app_rebootstrap_required');assert.equal(next.pending,1);
  const receipt=JSON.parse((await worker.get(0)).messages[0].body);assert.equal(receipt.kind,'help_accepted');assert.equal(receipt.origin_chat_id,'original-chat');
  await runManager(config,env,signal,{once:true});assert.equal(status().pending,1);
  store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));assert.equal(store.rows()[0].state,'queued');store.close();
  const controller=new AbortController();let ticks=0;
  await runManager(config,env,controller.signal,{onStatus:s=>{if(s.lifecycle==='active'){ticks++;controller.abort();}}});
  assert.equal(ticks,1);assert.equal(status().lifecycle,'stopped');
  store=new ReviewedStore(join(config.stateRoot,'queue.sqlite'));assert.equal(store.rows().length,1);store.close();
  assert.ok(!readFileSync(join(config.stateRoot,'status.json'),'utf8').includes(token));
 }finally{await worker?.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomBytes} from 'node:crypto';
import {startServer} from '../xats-routing-lab/upstream/src/daemon/server.ts';
import {connectInbox} from '../goal-routing-integration/xats.ts';
import {Store} from '../goal-routing-integration/store.ts';
import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';
import {InboxPump} from './inbox-pump.ts';

test('real XATS ingress replay after crash and lost acknowledgment retains one job',async()=>{
 const root=mkdtempSync(join(tmpdir(),'help-pump-')),token=randomBytes(32).toString('hex');
 const server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});
 const clients:any[]=[];let s=new Store(join(root,'queue.sqlite'));
 try{
  const url=`http://127.0.0.1:${server.port}/mcp`;
  const manager=await connectInbox(url,token,'manager','test');clients.push(manager);
  const worker=await connectInbox(url,token,'worker','test');clients.push(worker);
  const agents=(await manager.call('list_agents',{})).agents;
  const mid=agents.find((a:any)=>a.name==='manager').agent_id,wid=agents.find((a:any)=>a.name==='worker').agent_id;
  let routes=new PublisherRoutes(s);const binding=routes.publish(wid,'task','unit','worker',{threadId:'original-chat'});
  const payload={v:1,binding,request_id:'question',delivery_id:remoteDelivery(binding,'question'),payload:{kind:'question',problem:'blocked',acceptance:'actual check'}};
  await worker.send(mid,JSON.stringify(payload));
  let pump=new InboxPump(s,routes,manager,stage=>{if(stage==='after_ingress')throw Error('injected process stop');});
  await assert.rejects(pump.ingest(),/process stop/);assert.equal(s.rows().length,1);assert.equal(pump.cursor(),0);
  s.close();s=new Store(join(root,'queue.sqlite'));routes=new PublisherRoutes(s);
  pump=new InboxPump(s,routes,manager,stage=>{if(stage==='after_ack_send')throw Error('lost acknowledgment');});
  await pump.ingest();await assert.rejects(pump.flush(),/lost acknowledgment/);
  s.close();s=new Store(join(root,'queue.sqlite'));routes=new PublisherRoutes(s);pump=new InboxPump(s,routes,manager);
  await pump.flush();await worker.send(mid,JSON.stringify(payload));await pump.ingest();await pump.flush();
  assert.equal(s.rows().length,1);
  const answers=(await worker.get(0)).messages.map((m:any)=>JSON.parse(m.body));
  assert.ok(answers.length>=2);assert.ok(answers.every((a:any)=>a.kind==='help_accepted'&&a.delivery_id===payload.delivery_id&&a.origin_chat_id==='original-chat'));
 }finally{s.close();for(const c of clients)await c.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});

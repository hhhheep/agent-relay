import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes,remoteDelivery} from './publisher-route.ts';import {RoutingService} from './routing-service.ts';
import {digest} from '../goal-routing-integration/store.ts';import {acceptAppProgress} from '../goal-routing-integration/app-receipt.ts';

test('one problem Goal spans proposal and failed verification; completes only after independent acceptance',async()=>{
 const root=mkdtempSync(join(tmpdir(),'problem-goal-'));const store=new ReviewedStore(join(root,'queue.db'));let turns:any[]=[];const objectives:string[]=[];
 try{
  const routes=new PublisherRoutes(store),binding=routes.publish('worker','task','unit','worker',{threadId:'chat'});
  const inbox={get:async()=>({messages:[],last_event_id:0}),send:async()=>({})};
  const app={read:async(id:string)=>({thread:{id,status:{type:'idle'}},turns}),send:async(id:string)=>({threadId:id})};
  const service=new RoutingService(store,routes,inbox,app,join(root,'receipts'));
  const question=remoteDelivery(binding,'q');
  async function review(request:string,payload:any,decision:any,resolved:boolean){
   const delivery=remoteDelivery(binding,request);routes.ingest('worker',{v:1,binding,request_id:request,delivery_id:delivery,payload});
   assert.equal((await service.tick()).threads[0].state,'dispatched');
   const job=store.rows().find(r=>JSON.parse(String(r.body)).id===delivery)!;
   const episode=store.db.prepare('SELECT * FROM help_episodes WHERE job=?').get(job.id) as any;objectives.push(episode.objective);
   const goal={threadId:'chat',objective:episode.objective,status:'active',createdAt:100,updatedAt:100};
   const receipt={threadId:'chat',requestId:delivery,episodeId:job.id,objective:episode.objective,started:{goal},problemResolved:resolved,
    ...(resolved?{completed:{goal:{...goal,status:'complete',updatedAt:101}}}:{}),after:{goal:resolved?{...goal,status:'complete',updatedAt:101}:goal},text:JSON.stringify(decision)};
   if(payload.kind==='question'){
    const premature={...receipt,problemResolved:true,completed:{goal:{...goal,status:'complete',updatedAt:101}},after:{goal:{...goal,status:'complete',updatedAt:101}}};
    writeFileSync(episode.path,JSON.stringify(premature));turns.push({id:'premature',status:'completed',items:[{type:'fileChange',changes:[{path:episode.path}]}]});
    assert.equal((await service.tick()).threads[0].state,'attention');
    assert.equal(store.db.prepare('SELECT COUNT(*) n FROM incidents').get()?.n,0);
   }
   writeFileSync(episode.path,JSON.stringify(receipt));turns.push({id:request,status:'completed',items:[{type:'fileChange',changes:[{path:episode.path}]}]});
   assert.equal((await service.tick()).threads[0].state,'model_completed');await service.tick();
   return JSON.parse(String(store.db.prepare('SELECT result FROM events WHERE id=?').get(job.id)?.result));
  }
  const proposed=await review('q',{kind:'question',problem:'problem',acceptance:'actual validation'}, {decision:'propose',summary:'prepare actual checks',proposal:'check evidence'},false);
  const answerSha=digest(JSON.stringify(proposed));
  const failed=await review('v1',{kind:'verification',pass:false,proposal_id:question,answer_sha256:answerSha,evidence:'missing receipt'}, {decision:'reject',summary:'receipt still missing'},false);
  assert.equal(failed.kind,'retry_allowed');
  const accepted=await review('v2',{kind:'verification',pass:true,proposal_id:question,answer_sha256:answerSha,evidence:'actual receipt validated'}, {decision:'accept',summary:'independent checks passed'},true);
  assert.equal(accepted.kind,'closed');assert.equal(new Set(objectives).size,1);
 }finally{store.close();rmSync(root,{recursive:true,force:true});}
});

test('continuing receipt rejects paused, foreign and replaced Goals and never invents completion',()=>{
 const expected={threadId:'chat',requestId:'request',episodeId:'episode',objective:'Harness help problem incident'};
 const goal={threadId:'chat',objective:expected.objective,status:'active',createdAt:100,updatedAt:100};
 const receipt={...expected,turnId:'turn',started:{goal},after:{goal},problemResolved:false,text:'result'};
 const turn={threadId:'chat',turnId:'turn',status:'completed'};
 assert.equal(acceptAppProgress(expected,receipt,turn).problemResolved,false);
 for(const changed of [{...goal,status:'paused'},{...goal,threadId:'foreign'},{...goal,createdAt:101,updatedAt:101}]){
  assert.throws(()=>acceptAppProgress(expected,{...receipt,after:{goal:changed}},turn));
 }
 assert.throws(()=>acceptAppProgress(expected,{...receipt,completed:{goal:{...goal,status:'complete'}}},turn));
});

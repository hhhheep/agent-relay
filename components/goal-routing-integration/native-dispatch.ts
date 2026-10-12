import {Store,digest} from './store.ts';
import {acceptAppCompletion,acceptAppProgress,type AppTurn} from './app-receipt.ts';

// Durable boundary for native App sends. App accepts no idempotency key, so an
// ambiguous send is never automatically repeated. A model receipt can reconcile
// it; otherwise intervention is required. This class does not mutate Goals.
export class NativeDispatch {
 private live=new Set<string>();
 constructor(readonly store:Store,readonly send:(thread:string,prompt:string)=>Promise<any>,readonly fault:(stage:string)=>void=()=>{},readonly target:(job:any)=>string=job=>job.thread,readonly guard:()=>void=()=>{}){
  store.db.exec(`CREATE TABLE IF NOT EXISTS native_dispatch(job_id TEXT PRIMARY KEY,thread TEXT NOT NULL,prompt_hash TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
  CREATE UNIQUE INDEX IF NOT EXISTS one_native_pending_per_thread ON native_dispatch(thread) WHERE state IN ('prepared','sent');`);
 }
 async dispatch(id:string,prompt:string,objective?:string){
  if(this.live.has(id))throw Error('native_dispatch_in_progress');
  this.live.add(id);
  try{
   const hash=digest(prompt);
   const intent=this.store.tx(()=>{
    let job=this.store.db.prepare('SELECT * FROM events WHERE id=?').get(id) as any;
    if(!job)throw Error('unknown_native_job');
    const row=this.store.db.prepare('SELECT * FROM native_dispatch WHERE job_id=?').get(id) as any;
    job={...job,thread:row?.thread??this.target(job)};
    if(row){
     if(row.thread!==job.thread||row.prompt_hash!==hash)throw Error('native_dispatch_conflict');
     if(objective!==undefined&&job.objective!==objective)throw Error('native_objective_conflict');
     if(row.state==='prepared')throw Error('uncertain_native_dispatch_requires_receipt');
     return {cached:true,receipt:JSON.parse(row.receipt)};
    }
    if(job.state!=='queued')throw Error('native_job_not_queued');
    this.guard();
    const pending=this.store.db.prepare("SELECT job_id FROM native_dispatch WHERE thread=? AND state IN ('prepared','sent')").get(job.thread);
    if(pending)throw Error('native_thread_waiting_for_receipt');
    this.store.db.prepare("INSERT INTO native_dispatch VALUES (?,?,?,'prepared',NULL)").run(id,job.thread,hash);
    if(objective!==undefined)this.store.validatePhase(job);
    this.store.mark(id,'running',objective);
    return {cached:false,thread:job.thread};
   });
   if(intent.cached)return intent.receipt;
   this.fault('before_native_send');
   this.guard();
   const receipt=await this.send(intent.thread,prompt);
   this.fault('after_native_accept');
   if(receipt?.threadId!==intent.thread)throw Error('native_accept_binding_mismatch');
   this.store.db.prepare("UPDATE native_dispatch SET state='sent',receipt=? WHERE job_id=? AND state='prepared'").run(JSON.stringify(receipt),id);
   return receipt;
  }finally{this.live.delete(id);}
 }
 goalHandled(id:string,receipt:any,turn:AppTurn){
  let job=this.store.db.prepare('SELECT * FROM events WHERE id=?').get(id) as any;
  const row=this.store.db.prepare('SELECT * FROM native_dispatch WHERE job_id=?').get(id) as any;
  if(!job||!row||!job.objective)throw Error('unknown_native_goal_job');
  job={...job,thread:row.thread};
  const input=JSON.parse(job.body);
  const expected={threadId:job.thread,requestId:input.id,episodeId:id,objective:job.objective};
  const evidence=receipt.problemResolved===false?acceptAppProgress(expected,receipt,turn):acceptAppCompletion(expected,receipt,turn);
  const encoded=JSON.stringify(receipt);
  if(row.state==='handled'){
   if(row.receipt!==encoded)throw Error('native_goal_receipt_conflict');
   return evidence;
  }
  this.store.ready(job,evidence,()=>{this.store.db.prepare("UPDATE native_dispatch SET state='handled',receipt=? WHERE job_id=?").run(encoded,id);this.fault('before_native_goal_commit');});
  return evidence;
 }
 handled(id:string,ack:any){return this.store.tx(()=>{
  let job=this.store.db.prepare('SELECT * FROM events WHERE id=?').get(id) as any;
  const row=this.store.db.prepare('SELECT * FROM native_dispatch WHERE job_id=?').get(id) as any;
  if(!job||!row)throw Error('unknown_native_job');
  job={...job,thread:row.thread};
  const input=JSON.parse(job.body);
  if(ack?.deliveryId!==id||ack.threadId!==job.thread||ack.marker!==input.id||ack.observed!==true)throw Error('native_model_receipt_binding_mismatch');
  const encoded=JSON.stringify(ack);
  if(row.state==='handled'){
   if(row.receipt!==encoded)throw Error('native_model_receipt_conflict');
   return;
  }
  this.store.db.prepare("UPDATE native_dispatch SET state='handled',receipt=? WHERE job_id=?").run(encoded,id);
  const result={v:1,delivery_id:id,request_id:input.id,incident:input.incident,kind:'model_received',target:job.thread,receipt:ack};
  this.store.db.prepare("UPDATE events SET state='reply_ready',result=? WHERE id=?").run(JSON.stringify(result),id);
 });}
 async flush(id:string,inbox:any){
  const job=this.store.db.prepare('SELECT * FROM events WHERE id=?').get(id) as any;
  if(!job||job.state==='done')return;
  if(job.state!=='reply_ready')throw Error('native_model_receipt_required');
  const receipt=await inbox.send(job.source,job.result);
  this.fault('after_remote_send');
  this.store.db.prepare("UPDATE events SET state='done',sent=? WHERE id=?").run(JSON.stringify(receipt),id);
 }
}

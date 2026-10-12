import {Store,digest} from '../goal-routing-integration/store.ts';
import {NativeDispatch} from '../goal-routing-integration/native-dispatch.ts';

export type Binding={task_id:string,work_unit_id:string,worker_id:string,origin_chat_id:string};
const canonical=(v:any):string=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);
export const remoteDelivery=(b:Binding,id:string)=>digest(canonical([b,id]));

// Local trusted publisher owns registration. Network envelopes cannot create or
// alter a route. source is the transport-authenticated XATS sender, never payload.
export class PublisherRoutes {
 constructor(readonly store:Store){store.db.exec(`
 CREATE TABLE IF NOT EXISTS publisher_routes(scope TEXT PRIMARY KEY,source TEXT NOT NULL,binding TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS help_ingress(delivery TEXT PRIMARY KEY,job TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,body TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS help_remote_answers(delivery TEXT PRIMARY KEY,request_id TEXT NOT NULL,answer_sha256 TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS help_answer_attempts(delivery TEXT PRIMARY KEY,attempted INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS publisher_route_hosts(scope TEXT PRIMARY KEY,host TEXT NOT NULL CHECK(host='local'));
 INSERT OR IGNORE INTO publisher_route_hosts SELECT scope,'local' FROM publisher_routes;
 `);}
 publish(source:string,task:string,unit:string,worker:string,caller:{threadId:string}){
  const b:Binding={task_id:task,work_unit_id:unit,worker_id:worker,origin_chat_id:caller.threadId};
  if([source,...Object.values(b)].some(x=>typeof x!=='string'||!x.trim()||x.length>256))throw Error('invalid_publish_binding');
  const scope=digest(canonical([task,unit]));
  return this.store.tx(()=>{
   const encoded=canonical(b),old=this.store.db.prepare('SELECT * FROM publisher_routes WHERE scope=?').get(scope) as any;
   if(old&&(old.source!==source||old.binding!==encoded))throw Error('publication_rebind_rejected');
   this.store.db.prepare('INSERT OR IGNORE INTO publisher_routes VALUES (?,?,?)').run(scope,source,encoded);
   this.store.db.prepare("INSERT OR IGNORE INTO publisher_route_hosts VALUES (?,'local')").run(scope);
   this.store.bind(scope,b.origin_chat_id);
   return b;
  });
 }
 ingest(source:string,envelope:any){
  const b=envelope?.binding;
  if(!b||envelope.v!==1||typeof envelope.request_id!=='string'||!envelope.request_id||envelope.request_id.length>128)throw Error('invalid_help_envelope');
  const scope=digest(canonical([b.task_id,b.work_unit_id]));
  const registered=this.store.db.prepare('SELECT * FROM publisher_routes WHERE scope=?').get(scope) as any;
  if(!registered||registered.source!==source||registered.binding!==canonical(b))throw Error('untrusted_help_source');
  if(envelope.delivery_id!==remoteDelivery(b,envelope.request_id))throw Error('delivery_binding_mismatch');
  if(envelope.kind==='help_answer_saved'){
   const saved=this.store.db.prepare('SELECT e.result FROM help_ingress h JOIN events e ON e.id=h.job WHERE h.delivery=? AND h.scope=?').get(envelope.delivery_id,scope) as any;
   if(!saved?.result||digest(saved.result)!==envelope.answer_sha256)throw Error('unverified_saved_answer');
   this.store.db.prepare('INSERT OR IGNORE INTO help_remote_answers VALUES (?,?,?)').run(envelope.delivery_id,envelope.request_id,envelope.answer_sha256);
   return {kind:'help_answer_confirmed',delivery_id:envelope.delivery_id,binding:b,answer_sha256:envelope.answer_sha256};
  }
  const p=envelope.payload;
  if(!p||!['question','verification','worker_incomplete','no_progress_observed'].includes(p.kind))throw Error('invalid_help_payload');
  if(p.kind==='question'&&(!p.problem||!p.acceptance))throw Error('missing_question_fields');
  if(p.payload_artifact){const ref=p.payload_artifact;
   if(ref.task_id!==b.task_id||!/^sha256:[a-f0-9]{64}$/.test(ref.artifact_id)||ref.artifact_id!=='sha256:'+ref.sha256||!Number.isSafeInteger(ref.byte_size)||ref.byte_size<=0||ref.byte_size>70000)throw Error('invalid_help_payload');}
  // Existing Store/NativeDispatch request contract. Larger evidence belongs in artifacts.
  if(p.kind==='verification'){
   if(typeof p.pass!=='boolean'||typeof p.evidence!=='string'||!p.evidence.trim())throw Error('invalid_verification');
   const proposal=this.store.db.prepare('SELECT h.scope,e.result FROM help_ingress h JOIN events e ON e.id=h.job WHERE h.delivery=?').get(p.proposal_id) as any;
   if(!proposal||proposal.scope!==scope)throw Error('unbound_verification_proposal');
   if(!proposal.result||JSON.parse(proposal.result).kind!=='awaiting_verification'||digest(proposal.result)!==p.answer_sha256)throw Error('verification_answer_mismatch');
  }
  const body=JSON.stringify({v:1,id:envelope.delivery_id,incident:p.kind==='verification'?p.proposal_id:envelope.delivery_id,kind:p.kind==='verification'?'verification':'request',payload:p});
  if(body.length>2400)throw Error('help_payload_requires_artifact_reference');
  const id=digest(scope+'\0'+envelope.delivery_id);
  return this.store.tx(()=>{
   const old=this.store.db.prepare('SELECT * FROM help_ingress WHERE delivery=?').get(envelope.delivery_id) as any;
   if(old&&old.body!==body)throw Error('help_content_conflict');
   if(!old){
    const n=Number((this.store.db.prepare("SELECT count(*) n FROM events WHERE state NOT IN ('done','human')").get() as any).n);
    if(n>=128)throw Error('capacity_retry');
    this.store.db.prepare('INSERT INTO events(id,source,thread,body) VALUES (?,?,?,?)').run(id,scope,b.origin_chat_id,body);
    this.store.db.prepare('INSERT INTO help_ingress VALUES (?,?,?,?)').run(envelope.delivery_id,id,scope,body);
   }
   return {delivery_id:envelope.delivery_id,origin_chat_id:b.origin_chat_id,durably_received:true};
  });
 }
 job(delivery:string){return (this.store.db.prepare('SELECT job FROM help_ingress WHERE delivery=?').get(delivery) as any)?.job;}
 async resendUnconfirmed(transport:{send:(source:string,body:string)=>Promise<any>},now=Date.now()){
  const rows=this.store.db.prepare(`SELECT h.delivery,e.result,r.source,r.binding FROM help_ingress h
   JOIN events e ON e.id=h.job JOIN publisher_routes r ON r.scope=h.scope
   LEFT JOIN help_remote_answers a ON a.delivery=h.delivery
   LEFT JOIN help_answer_attempts t ON t.delivery=h.delivery
   WHERE e.state='done' AND e.result IS NOT NULL AND a.delivery IS NULL
    AND (t.attempted IS NULL OR t.attempted<=?) ORDER BY coalesce(t.attempted,0),e.seq LIMIT 128`).all(now-30000) as any[];
  let failure:unknown;
  for(const row of rows){
   try{
   if(JSON.parse(row.result).kind==='model_received')throw Error('model_receipt_is_not_answer');
   // Commit attempt before network I/O. A crash delays retry; only an exact
   // remote persistence receipt ends retries, not a transport send acknowledgment.
   this.store.db.prepare('INSERT INTO help_answer_attempts VALUES (?,?) ON CONFLICT(delivery) DO UPDATE SET attempted=excluded.attempted').run(row.delivery,now);
   await transport.send(row.source,JSON.stringify({binding:JSON.parse(row.binding),delivery_id:row.delivery,answer:row.result}));
   }catch(error){failure??=error;}
  }
  if(failure)throw failure;
  return rows.length;
 }
 async flush(native:NativeDispatch,delivery:string,transport:{send:(source:string,body:string)=>Promise<any>}){
  const row=this.store.db.prepare('SELECT * FROM help_ingress WHERE delivery=?').get(delivery) as any;
  if(!row)throw Error('unknown_help_delivery');
  const route=this.store.db.prepare('SELECT * FROM publisher_routes WHERE scope=?').get(row.scope) as any;
  return native.flush(row.job,{send:async(scope:string,result:string)=>{
   if(scope!==row.scope)throw Error('reply_scope_mismatch');
   const parsed=JSON.parse(result);
   // A model-received acknowledgment is NOT a decision/answer for the worker.
   if(parsed.kind==='model_received')throw Error('model_receipt_is_not_answer');
   this.store.db.prepare('INSERT INTO help_answer_attempts VALUES (?,?) ON CONFLICT(delivery) DO UPDATE SET attempted=excluded.attempted').run(delivery,Date.now());
   return transport.send(route.source,JSON.stringify({binding:JSON.parse(route.binding),delivery_id:delivery,answer:result}));
  }});
 }
}

export function executorCaller(env:Record<string,string|undefined>){
 const threadId=env.CODEX_THREAD_ID;
 if(!threadId||!env.CODEX_APP_TOOLS_PIPE_PATH)throw Error('missing_trusted_app_context');
 return {threadId};
}

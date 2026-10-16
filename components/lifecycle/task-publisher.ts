import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {PublisherRoutes} from './publisher-route.ts';
import {posix} from 'node:path';
import {HarnessOperationError,rejectedBeforeLaunch} from './harness-port.ts';

const hash=(value:string)=>createHash('sha256').update(value).digest('hex');
export type TaskSubmission={task_id:string,work_unit_id:string,worker_id:string,source:string,worker_task:string,worker_phase:'prepare_code'|'execute_run'|'full',timeout_seconds:number};
export type MarkdownSnapshot={version:string,manifest:Record<string,string>};
export type WorkerProfile={python:string,runtimeRoot:string,workplace:string};
const sameSnapshot=(a:any,b:MarkdownSnapshot)=>a?.version===b.version&&a.manifest&&Object.keys(a.manifest).length===Object.keys(b.manifest).length&&Object.entries(b.manifest).every(([k,v])=>a.manifest[k]===v);

// Separate publication journal allows chats to enqueue while RoutingService owns
// its Store. Callers get identity from the bootstrapped official App port, never
// from task Markdown or a supplied destination field.
export class TaskPublisher {
 readonly db:DatabaseSync;
 constructor(path:string){
  this.db=new DatabaseSync(path);this.db.exec(`PRAGMA busy_timeout=5000;PRAGMA synchronous=FULL;
   CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',binding_receipt TEXT,command_id TEXT,result TEXT);
   CREATE TABLE IF NOT EXISTS publication_attempts(attempt INTEGER PRIMARY KEY AUTOINCREMENT,publication_id TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);`);
  this.db.exec('CREATE TABLE IF NOT EXISTS publication_operations(publication_id TEXT NOT NULL,kind TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,command_id TEXT,receipt TEXT,PRIMARY KEY(publication_id,kind));');
 }
 close(){this.db.close();}
 async dispatchReady(limit:number,authorize:(binding:any)=>()=>void,submit:(request:any)=>Promise<any>){
  if(!Number.isInteger(limit)||limit<1||limit>64)throw Error('invalid_publication_dispatch_limit');
  // Keep retryable capacity refusals in their original position, while manual
  // rejection rows cannot fill the bounded scan and hide eligible work.
  const rows=this.db.prepare(`SELECT id,body,state,result FROM publications
   WHERE state='ready' OR (state='rejected' AND CASE WHEN json_valid(result) THEN
    json_extract(result,'$.response.worker_admission.code')='worker_pool_busy'
    AND json_type(result,'$.response.worker_admission.no_command_prepared')='true' ELSE 0 END)
   ORDER BY rowid LIMIT 128`).all() as any[];
  const results:any[]=[];let attempts=0;
  for(const row of rows){
   try{
    const body=JSON.parse(row.body);
    if(row.state==='rejected'){
     const response=JSON.parse(row.result??'null')?.response;
     if(response?.worker_admission?.code!=='worker_pool_busy'||response.worker_admission.no_command_prepared!==true)continue;
    }
    if(attempts>=limit)break;
    const guard=authorize(body.binding);guard();
    if(row.state==='rejected')this.recoverRejected(row.id);
    attempts++;
    const result=await this.submit(row.id,request=>{guard();return submit(request);});
    results.push({id:row.id,state:result.state,command_id:result.command_id});
   }catch(error){
    const saved=this.inspect(row.id),response=JSON.parse(saved.result??'null')?.response;
    const waiting=saved.state==='rejected'&&response?.worker_admission?.code==='worker_pool_busy';
    results.push({id:row.id,state:waiting?'waiting_worker':saved.state==='prepared'?'submission_uncertain':'dispatch_requires_attention'});
   }
  }
  return results;
 }
 async recoverPending(recover:(request:any)=>Promise<any>,recoverKnown?:(task:string,command:string)=>Promise<any>){
  const rows=this.db.prepare("SELECT id,body,command_id FROM publications WHERE state IN ('prepared','running') ORDER BY id LIMIT 128").all() as any[];
  const results=[];
  for(const row of rows){
   try{
    const request=JSON.parse(row.body).request;
    if(row.command_id&&!recoverKnown)continue;
    const receipt=row.command_id?await recoverKnown!(request.task_id,row.command_id):await recover(request);
    if(receipt.state==='submission_uncertain'&&receipt.lookup_only===true&&receipt.command_id===null&&receipt.task_id===request.task_id&&receipt.work_unit_id===request.work_unit_id){results.push({id:row.id,state:'submission_uncertain'});continue;}
    const restored=this.reconcile(row.id,receipt);results.push({id:row.id,state:restored.state,command_id:restored.command_id});
   }catch{results.push({id:row.id,state:'recovery_requires_attention'});}
  }
  return results;
 }
 resumeContexts(profile:WorkerProfile,captureVerification=false){
  if(Object.keys(profile).sort().join(',')!=='python,runtimeRoot,workplace'||Object.values(profile).some(p=>typeof p!=='string'||!p.startsWith('/')||p.includes('\\')||p.includes('\n')||posix.normalize(p)!==p))throw Error('invalid_declared_worker_profile');
  this.db.exec('CREATE TABLE IF NOT EXISTS publication_profile(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);');
  const encoded=JSON.stringify({python:profile.python,runtimeRoot:profile.runtimeRoot,workplace:profile.workplace,...(captureVerification?{captureVerification:true}:{})});
  this.db.prepare('INSERT OR IGNORE INTO publication_profile VALUES (1,?)').run(encoded);
  if((this.db.prepare('SELECT body FROM publication_profile WHERE id=1').get() as any).body!==encoded)throw Error('publication_profile_migration_required');
  return (this.db.prepare("SELECT * FROM publications WHERE command_id IS NOT NULL AND state IN ('running','finished')").all() as any[]).map(row=>{
   const {binding}=JSON.parse(row.body),receipt=JSON.parse(row.result);
   if(receipt.task_id!==binding.task_id||receipt.work_unit_id!==binding.work_unit_id||receipt.command_id!==row.command_id)throw Error('publication_receipt_corrupt');
   // Python worker key uses json.dumps(sort_keys=True, ensure_ascii=False),
   // including its default spaces. Task text never provides these paths.
   const key=hash('{"task_id": '+JSON.stringify(binding.task_id)+', "work_unit_id": '+JSON.stringify(binding.work_unit_id)+'}');
   return {binding,context:{python_path:profile.python,help_script:posix.join(profile.runtimeRoot,'solution_agent/harness/help_queue.py'),mailbox:posix.join(profile.workplace,'.solution-agent/help-mailboxes',key,'queue.sqlite'),previous_command_id:row.command_id,...(captureVerification?{verification_runner:posix.join(profile.runtimeRoot,'solution_agent/harness/verification_runner.py'),verification_cwd:profile.workplace}:{})}};
  });
 }
 async prepare(input:TaskSubmission,app:{identity:()=>{threadId:string},read:(thread:string)=>Promise<any>},markdown?:MarkdownSnapshot){
  if(Object.keys(input).sort().join(',')!==['task_id','work_unit_id','worker_id','source','worker_task','worker_phase','timeout_seconds'].sort().join(','))throw Error('unexpected_publication_fields');
  if([input.task_id,input.work_unit_id,input.worker_id,input.source].some(x=>typeof x!=='string'||!x.trim()||x.length>128))throw Error('invalid_task_identity');
  if(!['prepare_code','execute_run','full'].includes(input.worker_phase)||!input.worker_task.trim()||Buffer.byteLength(input.worker_task)>65536||!Number.isFinite(input.timeout_seconds)||input.timeout_seconds<=0)throw Error('invalid_task_submission');
  if(markdown){
   if(!/^[a-f0-9]{64}$/.test(markdown.version)||!markdown.manifest||!Object.keys(markdown.manifest).length||Object.keys(markdown.manifest).length>128||Object.entries(markdown.manifest).some(([k,v])=>!k.endsWith('.md')||k.startsWith('/')||k.includes('\\')||k.includes(':')||k.split('/').some(p=>!p||p==='.'||p==='..')||!/^[a-f0-9]{64}$/.test(v)))throw Error('invalid_markdown_snapshot');
   markdown={version:markdown.version,manifest:Object.fromEntries(Object.entries(markdown.manifest).sort(([a],[b])=>a<b?-1:a>b?1:0))};
  }
  const {threadId}=app.identity();const snapshot=await app.read(threadId);
  if(snapshot.thread?.id!==threadId)throw Error('publisher_chat_readback_mismatch');
  if(snapshot.thread.hostId&&snapshot.thread.hostId!=='local')throw Error('publisher_local_app_host_required');
  const binding={task_id:input.task_id,work_unit_id:input.work_unit_id,worker_id:input.worker_id,origin_chat_id:threadId};
  const id=hash(JSON.stringify([input.task_id,input.work_unit_id]));
  const body=JSON.stringify({binding,source:input.source,...(markdown?{markdown}:{}),request:{task_id:input.task_id,work_unit_id:input.work_unit_id,worker_task:input.worker_task,worker_phase:input.worker_phase,timeout_seconds:input.timeout_seconds,access_mode:'writer',wait_ms:1000,max_preview_bytes:2048}});
  this.db.exec('BEGIN IMMEDIATE');
  try{
   const old=this.db.prepare('SELECT body FROM publications WHERE id=?').get(id) as any;
   if(old&&old.body!==body)throw Error('publication_conflict');
   this.db.prepare('INSERT OR IGNORE INTO publications(id,body) VALUES (?,?)').run(id,body);
   this.db.exec('COMMIT');return {id,binding};
  }catch(error){this.db.exec('ROLLBACK');throw error;}
 }
 inspect(id:string){const row=this.db.prepare('SELECT * FROM publications WHERE id=?').get(id) as any;if(!row)throw Error('unknown_publication');return row;}
 attempts(id:string){this.inspect(id);return this.db.prepare('SELECT * FROM publication_attempts WHERE publication_id=? ORDER BY attempt').all(id);}
 async operation(id:string,kind:string,request:any,port:{start:(request:any)=>Promise<any>,recover:(command:string)=>Promise<any>}){
  this.inspect(id);const encoded=JSON.stringify(request);
  let row=this.db.prepare('SELECT * FROM publication_operations WHERE publication_id=? AND kind=?').get(id,kind) as any;
  if(row&&row.request!==encoded)throw Error('publication_operation_conflict');
  if(row?.state==='succeeded'){
   const saved=JSON.parse(row.receipt);
   if(saved.exit_code!==0)throw Error('publication_operation_requires_attention');
   if(saved.maintenance?.finished===true)return saved;
   // OS command succeeded but capsule finalization may have conflicted.
   // Recover that exact command; never start its side effect again.
  }
  if(row&& !row.command_id)throw Error('publication_operation_submission_uncertain');
  if(row&&['failed','cancelled','lost'].includes(row.state))throw Error('publication_operation_requires_attention');
  if(!row){
   this.db.prepare("INSERT INTO publication_operations(publication_id,kind,request,state) VALUES (?,?,?,'prepared')").run(id,kind,encoded);
   row={};
  }
  let receipt:any;
  try{receipt=row.command_id?await port.recover(row.command_id):await port.start(request);}
  catch(error){
   const evidence=error instanceof HarnessOperationError?error.evidence:{message:String(error)};
   const data=evidence.response;
   // Keep a returned original ID even if later finalization failed.
   const command=data?.task_id===request.task_id&&data?.work_unit_id===request.work_unit_id?data.command_id:null;
   this.db.prepare('UPDATE publication_operations SET receipt=?,command_id=COALESCE(command_id,?) WHERE publication_id=? AND kind=?').run(JSON.stringify(evidence),command??null,id,kind);
   if(evidence.code!=='task_capsule_conflict'||!command||(row.command_id&&command!==row.command_id)||data.state!=='succeeded'||data.exit_code!==0)throw error;
   // One bounded retry of finalization only, after durable ID capture.
   // If still conflicting, this item stays recoverable without blocking peers.
   receipt=await port.recover(command);
  }
  if(receipt?.task_id!==request.task_id||receipt.work_unit_id!==request.work_unit_id||!receipt.command_id||
     !['prepared','running','succeeded','failed','cancelled','lost'].includes(receipt.state)||(row.command_id&&receipt.command_id!==row.command_id))throw Error('publication_operation_receipt_mismatch');
  this.db.prepare('UPDATE publication_operations SET state=?,command_id=?,receipt=? WHERE publication_id=? AND kind=?').run(receipt.state,receipt.command_id,JSON.stringify(receipt),id,kind);
  if(receipt.state!=='succeeded'||receipt.exit_code!==0||receipt.maintenance?.finished!==true)throw Error('publication_operation_wait_or_attention');
  return receipt;
 }
 recoverRejected(id:string){
  const row=this.inspect(id);
  if(row.state!=='rejected'||row.command_id||!rejectedBeforeLaunch(JSON.parse(row.body).request,JSON.parse(row.result)))throw Error('authoritative_rejection_required');
  this.db.prepare("UPDATE publications SET state='ready' WHERE id=? AND state='rejected' AND command_id IS NULL").run(id);
  return this.inspect(id);
 }
 async recoverUnstarted(id:string,inspectAuthority:(body:any)=>Promise<any>){
  const row=this.inspect(id),body=JSON.parse(row.body);
  if(row.state!=='prepared'||row.command_id)throw Error('legacy_unstarted_recovery_not_applicable');
  const receipt=await inspectAuthority(body),proof=receipt.proof;
  if(receipt.state!=='succeeded'||receipt.exit_code!==0||!receipt.command_id||receipt.task_id!==body.binding.task_id||
   proof?.schema!=='harness-unstarted/v1'||JSON.stringify(proof.binding)!==JSON.stringify(body.binding)||
   proof.worker_task_sha256!==hash(body.request.worker_task)||proof.worker_phase!==body.request.worker_phase||
   !Number.isInteger(proof.capsule_version)||proof.active_work_unit!==null||proof.writer_leases!==0||
   proof.worker_control_exists!==false||proof.worker_output_exists!==false||!Array.isArray(proof.matching_jobs)||proof.matching_jobs.length||
   !Array.isArray(proof.inspected_roots)||proof.inspected_roots.length!==2)throw Error('authoritative_absence_not_confirmed');
  this.db.exec('BEGIN IMMEDIATE');
  try{
   const changed=this.db.prepare("UPDATE publications SET state='ready',result=? WHERE id=? AND state='prepared' AND command_id IS NULL").run(JSON.stringify(receipt),id);
   if(Number(changed.changes)!==1)throw Error('publication_recovery_race');
   this.db.prepare("INSERT INTO publication_attempts(publication_id,state,receipt) VALUES (?,'unstarted_recovered',?)").run(id,JSON.stringify(receipt));this.db.exec('COMMIT');
  }catch(error){this.db.exec('ROLLBACK');throw error;}
  return this.inspect(id);
 }
 async admit(id:string,routes:PublisherRoutes,installBinding:(binding:any,markdown?:MarkdownSnapshot)=>Promise<any>,syncMarkdown?:(snapshot:MarkdownSnapshot)=>Promise<any>){
  const row=this.inspect(id),body=JSON.parse(row.body);
  if(['ready','prepared','running','finished'].includes(row.state))return row;
  if(body.markdown){
   if(!syncMarkdown)throw Error('markdown_sync_required');
   const synced=await syncMarkdown(body.markdown);
   if(synced?.ok!==true||!sameSnapshot(synced,body.markdown))throw Error('markdown_sync_not_confirmed');
  }
  routes.publish(body.source,body.binding.task_id,body.binding.work_unit_id,body.binding.worker_id,{threadId:body.binding.origin_chat_id});
  // Registry installation is idempotent and must prove the exact remote binding.
  // It does not launch a worker; replay after a lost receipt is safe.
  const receipt=await installBinding(body.binding,body.markdown);
  const same=receipt?.binding&&Object.keys(body.binding).every(k=>receipt.binding[k]===body.binding[k])&&Object.keys(receipt.binding).length===Object.keys(body.binding).length;
  if(receipt?.installed!==true||!same)throw Error('remote_binding_not_confirmed');
  if(body.markdown&&!sameSnapshot(receipt.markdown,body.markdown))throw Error('remote_markdown_binding_not_confirmed');
  this.db.prepare("UPDATE publications SET state='ready',binding_receipt=? WHERE id=? AND state='queued'").run(JSON.stringify(receipt),id);
  return this.inspect(id);
 }
 async submit(id:string,submit:(request:any)=>Promise<any>){
  const row=this.inspect(id);if(['running','finished'].includes(row.state))return row;
  if(row.state!=='ready')throw Error('publication_not_ready_or_submission_uncertain');
  this.db.exec('BEGIN IMMEDIATE');let attempt:number;
  try{
   const claimed=this.db.prepare("UPDATE publications SET state='prepared',result=NULL WHERE id=? AND state='ready'").run(id);
   if(Number(claimed.changes)!==1)throw Error('publication_already_claimed');
   attempt=Number(this.db.prepare("INSERT INTO publication_attempts(publication_id,state) VALUES (?,'prepared')").run(id).lastInsertRowid);
   this.db.exec('COMMIT');
  }catch(error){this.db.exec('ROLLBACK');throw error;}
  const request=JSON.parse(row.body).request;
  try{
   const receipt=await submit(request);
   const result=this.reconcile(id,receipt);
   this.db.prepare('UPDATE publication_attempts SET state=?,receipt=? WHERE attempt=?').run(result.state,JSON.stringify(receipt),attempt);
   return result;
  }catch(error){
   const evidence=error instanceof HarnessOperationError?error.evidence:{code:'transport_or_receipt_unknown',message:String(error)};
   const state=error instanceof HarnessOperationError&&rejectedBeforeLaunch(request,evidence)?'rejected':'prepared';
   const response=error instanceof HarnessOperationError?evidence.response:undefined;
   const command=response?.task_id===request.task_id&&response?.work_unit_id===request.work_unit_id&&
    typeof response.command_id==='string'&&/^[0-9a-f]{32}$/.test(response.command_id)?response.command_id:null;
   this.db.exec('BEGIN IMMEDIATE');
   try{
    this.db.prepare("UPDATE publications SET state=?,result=?,command_id=COALESCE(command_id,?) WHERE id=? AND state='prepared' AND command_id IS NULL").run(state,JSON.stringify(evidence),command,id);
    this.db.prepare('UPDATE publication_attempts SET state=?,receipt=? WHERE attempt=?').run(state,JSON.stringify(evidence),attempt);
    this.db.exec('COMMIT');
   }catch(saveError){this.db.exec('ROLLBACK');throw saveError;}
   throw error;
  }
 }
 reconcile(id:string,receipt:any){
  const row=this.inspect(id),body=JSON.parse(row.body);
  if(!['prepared','running','finished'].includes(row.state))throw Error('publication_not_submitted');
  if(!receipt?.command_id||receipt.task_id!==body.binding.task_id||receipt.work_unit_id!==body.binding.work_unit_id)throw Error('submission_receipt_mismatch');
  if(row.command_id&&row.command_id!==receipt.command_id)throw Error('submission_command_conflict');
  if(!['prepared','running','succeeded','failed','cancelled'].includes(receipt.state))throw Error('unconfirmed_submission_state');
  const state=['succeeded','failed','cancelled'].includes(receipt.state)?'finished':'running';
  if(row.state==='finished'&&state!=='finished')throw Error('stale_submission_receipt');
  this.db.prepare('UPDATE publications SET state=?,command_id=?,result=? WHERE id=?').run(state,receipt.command_id,JSON.stringify(receipt),id);
  return this.inspect(id);
 }
}

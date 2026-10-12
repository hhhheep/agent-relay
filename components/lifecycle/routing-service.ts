import {mkdirSync,existsSync,readFileSync,statSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {Store,digest} from '../goal-routing-integration/store.ts';
import {NativeDispatch} from '../goal-routing-integration/native-dispatch.ts';
import {PublisherRoutes} from './publisher-route.ts';
import {InboxPump} from './inbox-pump.ts';
import {ReviewedStore} from './reviewed-store.ts';
import {initializeControllerAuthority,currentController,assertController} from './controller-authority.ts';
import {managerProtocol,bindManagerReceipt} from './manager-task-protocol.ts';

export type AppPort={read:(thread:string)=>Promise<any>;send:(thread:string,prompt:string)=>Promise<any>;controllerChat?:()=>string};

// One bounded deterministic tick; an OS-owned driver may schedule ticks. No
// model polling, hidden Goal, foreign-Goal takeover or automatic resend on doubt.
export class RoutingService {
 readonly native:NativeDispatch;readonly pump:InboxPump;readonly root:string;
 private ticking=false;
 private dispatchAuthority?:{scope:string,epoch:number};
 constructor(readonly store:Store,readonly routes:PublisherRoutes,readonly inbox:any,readonly app:AppPort,receiptRoot:string){
  if(!(store instanceof ReviewedStore))throw Error('manager_review_policy_required');
  this.root=resolve(receiptRoot);mkdirSync(this.root,{recursive:true});
  store.db.exec('CREATE TABLE IF NOT EXISTS help_episodes(job TEXT PRIMARY KEY,path TEXT NOT NULL,objective TEXT NOT NULL,prompt TEXT NOT NULL);');
  store.db.exec('CREATE TABLE IF NOT EXISTS help_episode_protocol(job TEXT PRIMARY KEY,version INTEGER NOT NULL);');
  store.tx(()=>{
   const old=store.db.prepare("SELECT value FROM meta WHERE key='help_receipt_root'").get();
   if(old&&old.value!==this.root)throw Error('receipt_root_migration_required');
   store.db.prepare("INSERT OR IGNORE INTO meta VALUES ('help_receipt_root',?)").run(this.root);
  });
  initializeControllerAuthority(store.db);
  store.db.exec('CREATE TABLE IF NOT EXISTS controller_late_receipts(job TEXT PRIMARY KEY,thread TEXT NOT NULL,receipt TEXT NOT NULL,turn TEXT NOT NULL);');
  store.db.exec('CREATE TABLE IF NOT EXISTS controller_episode_targets(job TEXT PRIMARY KEY,target TEXT NOT NULL);CREATE TABLE IF NOT EXISTS controller_episode_history(job TEXT NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job,hash));');
  store.db.exec('CREATE TABLE IF NOT EXISTS controller_manual_reviews(job TEXT PRIMARY KEY,controller TEXT NOT NULL,epoch INTEGER NOT NULL,receipt TEXT NOT NULL,turn TEXT NOT NULL);');
  this.native=new NativeDispatch(store,(thread,prompt)=>app.send(thread,prompt),()=>{},job=>{
   const route=store.db.prepare('SELECT scope FROM publisher_routes WHERE scope=?').get(job.source);
   return route?currentController(store.db,job.source).chat:job.thread;
  },()=>{if(this.dispatchAuthority)assertController(store.db,this.dispatchAuthority.scope,app.controllerChat?.(),this.dispatchAuthority.epoch);});
  this.pump=new InboxPump(store,routes,inbox);
 }
 private episode(job:any){
  let episode=this.store.db.prepare('SELECT * FROM help_episodes WHERE job=?').get(job.id) as any;
  if(episode){
   const recorded=this.store.db.prepare('SELECT target FROM controller_episode_targets WHERE job=?').get(job.id) as any;
   const original=this.store.db.prepare('SELECT thread FROM events WHERE id=?').get(job.id) as any;
   if((recorded?.target??original.thread)===job.thread)return episode;
   if(this.store.db.prepare('SELECT job_id FROM native_dispatch WHERE job_id=?').get(job.id))throw Error('uncertain_episode_cannot_retarget');
   this.store.tx(()=>{
    this.store.db.prepare('INSERT OR IGNORE INTO controller_episode_history VALUES (?,?,?)').run(job.id,episode.prompt,JSON.stringify(episode));
    this.store.db.prepare('DELETE FROM help_episodes WHERE job=?').run(job.id);
    this.store.db.prepare('DELETE FROM help_episode_protocol WHERE job=?').run(job.id);
    this.store.db.prepare('DELETE FROM controller_episode_targets WHERE job=?').run(job.id);
   });
  }
  const input=JSON.parse(job.body),path=resolve(this.root,job.id+'.json');
  const problemGoal=input.kind==='verification'||input.payload.kind==='question';
  const objective=problemGoal?'Harness help problem '+digest(job.source+'\0'+input.incident):'Harness manager episode '+job.id;
  const route=this.store.db.prepare('SELECT binding FROM publisher_routes WHERE scope=?').get(job.source) as any;
  const binding=route?JSON.parse(route.binding):undefined;
  const line=/-([GTU])-/.exec(binding?.task_id??'')?.[1];
  const label=line?line+' 線':'遠端任務';
  const summary=input.kind==='verification'
   ?input.payload.pass===false?'遠端回報驗證未通過，請獨立核對證據，指出還缺哪些檢查。':'遠端提交驗證結果，請獨立核對後決定是否接受。'
   :input.payload.kind==='worker_incomplete'?'worker 回報尚未完成，請確認卡點與接續條件。':'worker 有一個問題需要你判斷，請核對後給出具體建議。';
  const taskPath=resolve(this.root,job.id+'.'+digest(job.thread).slice(0,8)+'.task.json');
  const protocolPath=resolve(this.root,'manager-protocol-'+digest(managerProtocol).slice(0,16)+'.txt');
  if(existsSync(protocolPath)){if(readFileSync(protocolPath,'utf8')!==managerProtocol)throw Error('manager_protocol_content_conflict');}
  else writeFileSync(protocolPath,managerProtocol,{encoding:'utf8',flag:'wx'});
  const {proposal_id,answer_sha256,...evidence}=input.payload;
  const task={schema:'harness-manager-task/v2',protocol:protocolPath,responsePath:path,objective,
   goalPolicy:problemGoal?'one_goal_per_problem':'bounded_notification_review',
   problem:summary,evidence,
   completion:input.kind==='verification'?'獨立核對原問題驗收條件；缺證據則保留 Goal 並指出缺項。':input.payload.acceptance??'核實狀態，提供有證據的接續條件。'};
  const taskText=JSON.stringify(task,null,2);
  if(existsSync(taskPath)){if(readFileSync(taskPath,'utf8')!==taskText)throw Error('manager_task_content_conflict');}
  else writeFileSync(taskPath,taskText,{encoding:'utf8',flag:'wx'});
  const reviewedPrompt=`${label}：${summary}\n請讀取[任務單](${taskPath})，按其中的管理流程處理並保存回覆。細節與回執欄位已放在任務單，不必貼回聊天。`;
  this.store.tx(()=>{
   this.store.db.prepare('INSERT INTO help_episodes VALUES (?,?,?,?)').run(job.id,path,objective,reviewedPrompt);
   this.store.db.prepare('INSERT INTO help_episode_protocol VALUES (?,2)').run(job.id);
   this.store.db.prepare('INSERT INTO controller_episode_targets VALUES (?,?)').run(job.id,job.thread);
  });
  return {path,objective,prompt:reviewedPrompt};
 }
 private async process(thread:string){
  const job=this.store.next(thread);if(!job)return {thread,state:'idle'};
  const route=this.store.db.prepare('SELECT scope FROM publisher_routes WHERE scope=?').get(job.source);
  const authority=route?currentController(this.store.db,job.source):undefined;
  const requireCurrent=()=>{if(authority)assertController(this.store.db,authority.scope,this.app.controllerChat?.(),authority.epoch);};
  if(job.state==='reply_ready'){
   requireCurrent();
   const row=this.store.db.prepare('SELECT delivery FROM help_ingress WHERE job=?').get(job.id) as any;
   if(!row)throw Error('missing_help_ingress');
   await this.routes.flush(this.native,row.delivery,this.inbox);return {thread,state:'reply_sent'};
  }
  const intent=this.store.db.prepare('SELECT thread FROM native_dispatch WHERE job_id=?').get(job.id) as any;
  const target=intent?.thread??authority?.chat??thread;
  const effectiveJob={...job,thread:target};
  if(job.state==='running'&&authority?.transferred&&target!==authority.chat){
   const path=resolve(this.root,job.id+'.controller-review.json');
   if(existsSync(path)){
    requireCurrent();if(statSync(path).size>64000)throw Error('controller_review_too_large');
    let review:any;try{review=JSON.parse(readFileSync(path,'utf8'));}catch{return {thread,state:'waiting_controller_review_write'};}
    if(review.jobId!==job.id||review.controllerChat!==authority.chat||review.epoch!==authority.epoch||review.taskId!==authority.binding.task_id||review.workUnitId!==authority.binding.work_unit_id)throw Error('controller_review_binding_mismatch');
    if(!Array.isArray(review.evidenceRefs)||!review.evidenceRefs.length||review.evidenceRefs.length>16||review.evidenceRefs.some((ref:any)=>typeof ref!=='string'||!ref.trim()||ref.length>2048))throw Error('controller_review_evidence_required');
    const current=await this.app.read(authority.chat);
    if(current.thread?.id!==authority.chat)throw Error('app_read_thread_mismatch');
    const normalize=(s:string)=>s.replaceAll('\\','/').toLowerCase();
    const turn=current.turns?.find((t:any)=>t.status==='completed'&&t.items?.some((i:any)=>i.type==='fileChange'&&i.changes?.some((c:any)=>typeof c.path==='string'&&normalize(c.path)===normalize(path))));
    if(!turn)return {thread,state:'waiting_controller_review_turn'};
    const observed=JSON.stringify({threadId:authority.chat,turnId:turn.id,status:turn.status});
    const reviewed={text:review.text,controllerReview:true,controllerChat:authority.chat,epoch:authority.epoch,turnId:turn.id,evidenceRefs:review.evidenceRefs};
    this.store.ready(job,reviewed,()=>{
     requireCurrent();this.store.db.prepare('INSERT INTO controller_manual_reviews VALUES (?,?,?,?,?)').run(job.id,authority.chat,authority.epoch,JSON.stringify(review),observed);
    });
    return {thread,state:'controller_review_completed'};
   }
  }
  const snapshot=await this.app.read(target);
  if(snapshot.thread?.id!==target)throw Error('app_read_thread_mismatch');
  if(job.state==='queued'){
   if(!['idle','notLoaded'].includes(snapshot.thread.status?.type))return {thread,state:'waiting_chat_idle'};
   requireCurrent();
   const episode=this.episode(effectiveJob);
   this.dispatchAuthority=authority?{scope:authority.scope,epoch:authority.epoch}:undefined;
   try{await this.native.dispatch(job.id,episode.prompt,episode.objective);}finally{this.dispatchAuthority=undefined;}
   return {thread,state:'dispatched'};
  }
  if(job.state==='running'){
   const episode=this.store.db.prepare('SELECT * FROM help_episodes WHERE job=?').get(job.id) as any;
   if(!episode)throw Error('missing_episode_plan');
   if(!existsSync(episode.path))return {thread,state:'waiting_model_receipt'};
   if(statSync(episode.path).size>256000)throw Error('model_receipt_too_large');
   let receipt:any;try{receipt=JSON.parse(readFileSync(episode.path,'utf8'));}catch{return {thread,state:'waiting_receipt_write'};}
   const normalize=(s:string)=>s.replaceAll('\\','/').toLowerCase();
   const turn=snapshot.turns?.find((t:any)=>t.status==='completed'&&t.items?.some((i:any)=>i.type==='fileChange'&&i.changes?.some((c:any)=>typeof c.path==='string'&&normalize(c.path)===normalize(episode.path))));
   if(!turn)return {thread,state:'waiting_completed_app_turn'};
   if(authority?.transferred&&target!==authority.chat){
    const encoded=JSON.stringify(receipt),observed=JSON.stringify({threadId:target,turnId:turn.id,status:turn.status});
    const old=this.store.db.prepare('SELECT receipt,turn FROM controller_late_receipts WHERE job=?').get(job.id) as any;
    if(old&&(old.receipt!==encoded||old.turn!==observed))throw Error('late_controller_receipt_conflict');
    this.store.db.prepare('INSERT OR IGNORE INTO controller_late_receipts VALUES (?,?,?,?)').run(job.id,target,encoded,observed);
    return {thread,state:'handoff_receipt_requires_review'};
   }
   requireCurrent();
   if(receipt.error)throw Error('model_requires_attention:'+String(receipt.error));
   const compact=this.store.db.prepare('SELECT version FROM help_episode_protocol WHERE job=?').get(job.id)?.version===2;
   const bound=bindManagerReceipt(receipt,{threadId:target,requestId:JSON.parse(job.body).id,episodeId:job.id,objective:episode.objective,turnId:turn.id},compact);
   this.native.goalHandled(job.id,bound,{threadId:target,turnId:turn.id,status:turn.status});
   return {thread,state:'model_completed'};
  }
  return {thread,state:job.state};
 }
 async tick(){
  if(this.ticking)throw Error('routing_tick_in_progress');this.ticking=true;
  const report:any={transport:[],threads:[]};
  try{
   try{report.ingest=await this.pump.ingest();}catch(error){report.transport.push({phase:'ingest',error:String(error)});}
   try{report.acks=await this.pump.flush();}catch(error){report.transport.push({phase:'ack',error:String(error)});}
   try{report.answers=await this.routes.resendUnconfirmed(this.inbox);}catch(error){report.transport.push({phase:'answer_retry',error:String(error)});}
   const threads=[...new Set(this.store.rows().filter(r=>!['done','human'].includes(r.state)).map(r=>String(r.thread)))];
   for(const thread of threads){
    try{report.threads.push(await this.process(thread));}
    catch(error){report.threads.push({thread,state:'attention',error:String(error)});}
   }
   return report;
  }finally{this.ticking=false;}
 }
}

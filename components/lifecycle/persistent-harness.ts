import {DatabaseSync} from 'node:sqlite';
import {readFileSync,realpathSync} from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import {basename,dirname} from 'node:path';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {McpError,ErrorCode} from '@modelcontextprotocol/sdk/types.js';
import {NativeAppPort} from './native-app-port.ts';
import {currentController,controllerScope} from './controller-authority.ts';
import {PersistentSubmissions,type SubmissionRecord} from './persistent-submissions.ts';

export type PersistentBinding={chat:string,epoch:number,taskId:string,workUnitId:string};
export type PersistentScope={workspace:string,queuePath:string,binding:PersistentBinding,terminalIds:string[],jobIds?:string[],recoveryRefs?:{kind:'command-request'|'stage-focus',path:string,sha256:string}[]};
export type PersistentAuthorityStore={workspace:string,queuePath:string,terminalIds:string[]};
export type PersistentConfig={workspace:string,queuePath:string,appServerPath:string,workplaceIdentityPath:string,submissionJournalPath?:string,
 authorityStores?:PersistentAuthorityStore[],additionalScopes?:PersistentScope[],binding:PersistentBinding,terminalIds:string[],toolMode?:'read-only'|'full',jobIds?:string[],recoveryReleaseIds?:string[],recoveryRefs?:{kind:'command-request'|'stage-focus',path:string,sha256:string}[],peer:{protocol_version:string,release_id:string,source_root_sha256:string,mcp_schema_sha256:string}};
const sha=(v:string|Buffer)=>createHash('sha256').update(v).digest('hex');
const canonicalPath=(p:string)=>realpathSync.native(p);
const canonicalTools=['stage_execute','stage_status','stage_collect','terminal_open','terminal_exec','terminal_send','terminal_read','terminal_close','artifact_inspect','artifact_store','artifact_apply'];
const processWorkplaceOwners=new Map<string,string>();

// Read the existing authority; never initialize, transfer, repair or migrate it here.
export function readPersistentAuthority(config:PersistentConfig):PersistentBinding{
 const b=config.binding,scope=controllerScope(b.taskId,b.workUnitId);
 const db=new DatabaseSync(config.queuePath,{readOnly:true});
 try{
  db.exec('BEGIN');
  const has=(name:string)=>!!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name);
  if(!has('controller_authority'))throw Error('persistent_authority_schema_missing');
  const current=currentController(db,scope),binding=current.binding;
  if(binding.task_id!==b.taskId||binding.work_unit_id!==b.workUnitId)throw Error('persistent_scope_mismatch');
  if(typeof current.chat!=='string'||!current.chat||!Number.isSafeInteger(current.epoch)||current.epoch<0)throw Error('persistent_authority_invalid');
  if(has('meta')){
   const row=db.prepare("SELECT value FROM meta WHERE key='context_rollover'").get() as any;
   if(row){const value=JSON.parse(row.value);if(value?.schema!=='context-rollover/v1'||!['requested','creating','verifying','created','transferred'].includes(value.phase))throw Error('persistent_rollover_unverified');
    if(value.phase!=='transferred')throw Error('persistent_rollover_pending');}
  }
  return {...b,chat:current.chat,epoch:current.epoch};
 }finally{db.close();}
}
export function assertPersistentAuthority(config:PersistentConfig,caller:string){
 const current=readPersistentAuthority(config),b=config.binding;
 if(caller!==b.chat||caller!==current.chat||b.epoch!==current.epoch)throw Error('persistent_authority_stale');
 return current;
}

function assertTransferChain(config:PersistentConfig,from:PersistentBinding,to:PersistentBinding){
 if(from.taskId!==to.taskId||from.workUnitId!==to.workUnitId||to.epoch<from.epoch||to.epoch-from.epoch>128)throw Error('persistent_rebind_scope_rejected');
 if(to.epoch===from.epoch){if(to.chat!==from.chat)throw Error('persistent_rebind_grant_missing');return;}
 const db=new DatabaseSync(config.queuePath,{readOnly:true});
 try{
  if(!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='controller_transfers'").get())throw Error('persistent_rebind_grant_missing');
  const scope=controllerScope(from.taskId,from.workUnitId);
  const rows=db.prepare('SELECT id,grant_json,receipt FROM controller_transfers WHERE scope=?').all(scope) as any[];
  let chat=from.chat;
  for(let epoch=from.epoch+1;epoch<=to.epoch;epoch++){
   const candidates=rows.map(r=>({id:r.id,g:JSON.parse(r.grant_json),r:JSON.parse(r.receipt)})).filter(v=>v.r.epoch===epoch);
   if(candidates.length!==1)throw Error('persistent_rebind_grant_missing_or_ambiguous');
   const {id,g,r}=candidates[0];
   if(r.schema!=='controller-transfer/v1'||r.scope!==scope||g.taskId!==from.taskId||g.workUnitId!==from.workUnitId||
      g.expectedEpoch!==epoch-1||g.fromChat!==chat||r.previousChat!==chat||r.currentChat!==g.toChat||
      typeof g.toChat!=='string'||!g.toChat||g.toChat===chat||g.transferId!==id||r.transferId!==id||
      typeof g.authorizationRef!=='string'||!g.authorizationRef.trim()||r.authorizationRef!==g.authorizationRef)throw Error('persistent_rebind_grant_invalid');
   chat=g.toChat;
  }
  if(chat!==to.chat)throw Error('persistent_rebind_grant_invalid');
 }finally{db.close();}
}

export async function verifyPersistentCaller(config:PersistentConfig,env:NodeJS.ProcessEnv,app=new NativeAppPort()){
 try{
  await app.connect(config.appServerPath,env);
  const caller=app.identity().threadId,reply=await app.read(caller);
  if(reply.thread?.id!==caller||reply.thread.hostId!=='local'||canonicalPath(reply.thread.cwd)!==canonicalPath(config.workspace))throw Error('persistent_app_context_mismatch');
  assertPersistentAuthority(config,caller);
  return app;
 }catch(error){await app.close();throw error;}
}

// This owns only one backend session. Callers must explicitly start it; attach never starts it.
export class PersistentHarnessOwner {
 private readonly config:PersistentConfig;
 private backend:any;private state:'idle'|'starting'|'online'|'offline'|'closed'='idle';
 private tools:any[]=[];private attachments=new Map<string,{id:string,app:NativeAppPort,env:NodeJS.ProcessEnv,suspended?:boolean,reopening?:Promise<void>}>();private attaching=false;private attachingCount=0;private pending=0;
 private callTail:Promise<void>=Promise.resolve();
 private workplace:string;readonly generation=randomUUID();private starts=0;
 private jobs=new Set<string>();
 private recoveredCommands=new Set<string>();
 private activeBinding:PersistentBinding;
 private scopePolicies:PersistentHarnessOwner[]=[];
 private terminalBindings=new Map<string,{chat:string,taskId:string,scope:string}>();
 private submissions?:PersistentSubmissions;
 private transitioning=false;private lastDisconnectReason='none';
 constructor(config:PersistentConfig,private dependencies:{backend?:()=>any,app?:()=>NativeAppPort,runtimeSha256?:string}={}){
  this.config=JSON.parse(JSON.stringify(config));
  if(this.config.submissionJournalPath!==undefined)this.submissions=new PersistentSubmissions(this.config.submissionJournalPath);
  this.activeBinding=structuredClone(this.config.binding);
  const c=this.config,b=c.binding;
  if(c.toolMode!==undefined&&!['read-only','full'].includes(c.toolMode))throw Error('invalid_persistent_tool_mode');
  if(c.jobIds!==undefined&&(!Array.isArray(c.jobIds)||c.jobIds.some(id=>typeof id!=='string'||!id.trim())))throw Error('invalid_persistent_jobs');
  if(c.recoveryReleaseIds!==undefined&&(!Array.isArray(c.recoveryReleaseIds)||c.recoveryReleaseIds.length>64||new Set(c.recoveryReleaseIds).size!==c.recoveryReleaseIds.length||c.recoveryReleaseIds.some(id=>typeof id!=='string'||!id.trim()||id.length>256)))throw Error('invalid_persistent_recovery_releases');
  this.jobs=new Set(c.jobIds??[]);
  if(!b?.chat||!b.taskId||!b.workUnitId||!Number.isSafeInteger(b.epoch)||b.epoch<0||!c.terminalIds?.length||new Set(c.terminalIds).size!==c.terminalIds.length||c.terminalIds.some(t=>typeof t!=='string'||!t.trim()))throw Error('invalid_persistent_binding');
  if(c.peer?.protocol_version!=='0.6'||!c.peer.release_id||!['source_root_sha256','mcp_schema_sha256'].every(k=>/^[a-f0-9]{64}$/.test((c.peer as any)[k])))throw Error('invalid_persistent_peer');
  const identity=readFileSync(c.workplaceIdentityPath);
  if(!/^solution-agent-workplace-v1:[a-f0-9]{32}\n$/.test(identity.toString('utf8')))throw Error('invalid_persistent_workplace');
  this.workplace=sha(identity);
  this.validateRecoveryRefs();
  if(c.additionalScopes!==undefined&&(!Array.isArray(c.additionalScopes)||c.additionalScopes.length>128))throw Error('persistent_scopes_invalid');
  const seen=new Set([controllerScope(b.taskId,b.workUnitId)]);
  for(const scope of c.additionalScopes??[]){
   if(!scope||Object.keys(scope).some(k=>!['workspace','queuePath','binding','terminalIds','jobIds','recoveryRefs'].includes(k)))throw Error('persistent_scope_fields_rejected');
   if(typeof scope.workspace!=='string'||!scope.workspace||typeof scope.queuePath!=='string'||!scope.queuePath)throw Error('persistent_scopes_invalid');
   const id=controllerScope(scope.binding?.taskId,scope.binding?.workUnitId);
   if(seen.has(id))throw Error('persistent_scope_duplicate');seen.add(id);
   // Reuse the same policy checks, but never start another backend for a scope.
   this.scopePolicies.push(new PersistentHarnessOwner({...c,...scope,jobIds:scope.jobIds,recoveryRefs:scope.recoveryRefs,additionalScopes:undefined,authorityStores:undefined},dependencies));
  }
  if(c.authorityStores!==undefined&&(!Array.isArray(c.authorityStores)||c.authorityStores.length>16||c.authorityStores.some(store=>!store||Object.keys(store).some(k=>!['workspace','queuePath','terminalIds'].includes(k))||typeof store.workspace!=='string'||!store.workspace||typeof store.queuePath!=='string'||!store.queuePath||!Array.isArray(store.terminalIds)||!store.terminalIds.length||store.terminalIds.some(id=>typeof id!=='string'||!id.trim()))))throw Error('persistent_authority_stores_invalid');
 }
 private validateRecoveryRefs(){
  const refs=this.config.recoveryRefs??[];
  if(!Array.isArray(refs)||refs.length>1024)throw Error('persistent_recovery_refs_invalid');
  for(const ref of refs){
   if(!ref||!['command-request','stage-focus'].includes(ref.kind)||typeof ref.path!=='string'||!/^[a-f0-9]{64}$/.test(ref.sha256))throw Error('persistent_recovery_refs_invalid');
   const bytes=readFileSync(ref.path);
   if(bytes.length>1048576||sha(bytes)!==ref.sha256)throw Error('persistent_recovery_receipt_mismatch');
   const data=JSON.parse(bytes.toString('utf8')),b=this.config.binding,id=basename(dirname(canonicalPath(ref.path)));
   const releases=new Set([this.config.peer.release_id,...this.config.recoveryReleaseIds??[]]);
   if(!/^[a-zA-Z0-9_-]{1,256}$/.test(id))throw Error('persistent_recovery_id_invalid');
   if(ref.kind==='command-request'){
    if(basename(ref.path)!=='request.json'||data.schema_version!==2||data.command_id!==id||data.task_id!==b.taskId||data.work_unit_id!==b.workUnitId||!releases.has(data.runtime_release_id)||!/^[a-f0-9]{64}$/.test(data.command_sha256))throw Error('persistent_recovery_command_scope_rejected');
    this.recoveredCommands.add(id);
   }else{
    if(basename(ref.path)!=='focus_runtime.json'||data.schema_version!==1||data.focus_runtime_integrated!==true||data.capsule_id!==b.taskId||data.work_unit_id!==b.workUnitId||!releases.has(data.release_id))throw Error('persistent_recovery_job_scope_rejected');
    this.jobs.add(id);
   }
  }
 }
 status(){return {state:this.state,ownerPid:process.pid,generation:this.generation,configurationSha256:sha(JSON.stringify(this.config)),runtimeSha256:this.dependencies.runtimeSha256??null,backendStarts:this.starts,connection:{transitioning:this.transitioning,lastDisconnectReason:this.lastDisconnectReason},attached:this.attachments.size>0,attachments:this.attachments.size,pending:this.pending,activeBinding:structuredClone(this.activeBinding),activeScopes:this.policies().map(p=>structuredClone(p.activeBinding)),recovery:{commandIds:[...this.recoveredCommands].sort(),jobIds:[...this.jobs].sort()}};}
 private multiScope(){return !!(this.config.additionalScopes?.length||this.config.authorityStores?.length);}
 private policies(){return [this,...this.scopePolicies];}
 private effectiveConfig():PersistentConfig{return {...this.config,binding:structuredClone(this.activeBinding)};}
 async rebind(env:NodeJS.ProcessEnv,expected:{generation:string,epoch:number,scope?:string}){
  if(this.state!=='online')throw Error('persistent_service_not_online');
  // Idle frontend leases are not work. Keep them alive; every subsequent call
  // rechecks the committed controller. Active/queued work must drain first.
  if(this.attaching||this.attachingCount||this.pending)throw Error('persistent_frontend_busy');
  const policy=expected?.scope?this.policies().find(p=>controllerScope(p.activeBinding.taskId,p.activeBinding.workUnitId)===expected.scope):this;
  if(!policy)throw Error('persistent_rebind_scope_rejected');
  if(expected?.generation!==this.generation||expected.epoch!==policy.activeBinding.epoch)throw Error('persistent_rebind_conflict');
  const previous=structuredClone(policy.activeBinding),target=readPersistentAuthority(policy.config);
  assertTransferChain(policy.config,previous,target);
  this.attaching=true;let app:NativeAppPort|undefined;
  try{
   app=await verifyPersistentCaller({...policy.config,binding:target},env,this.dependencies.app?.());
   const latest=readPersistentAuthority(policy.config);
   if(this.state!=='online'||JSON.stringify(latest)!==JSON.stringify(target)||JSON.stringify(policy.activeBinding)!==JSON.stringify(previous))throw Error('persistent_rebind_conflict');
   assertTransferChain(policy.config,previous,target);
   policy.activeBinding=target;
   for(const binding of this.terminalBindings.values())if(binding.chat===previous.chat&&binding.scope===controllerScope(previous.taskId,previous.workUnitId))binding.chat=target.chat;
   return {schema:'persistent-owner-rebind/v1',changed:previous.chat!==target.chat||previous.epoch!==target.epoch,generation:this.generation,previousBinding:previous,activeBinding:structuredClone(target),backendStarts:this.starts};
  }finally{try{await app?.close();}finally{this.attaching=false;}}
 }
 // Schema discovery grants no authority and cannot create a backend session.
 catalog(){
  if(!this.tools.length||this.state==='closed')throw Error('persistent_service_not_online');
  return {tools:structuredClone(this.config.toolMode==='full'?this.tools:this.tools.filter(t=>t.name==='terminal_read'))};
 }
 private verifyPeer(result:any,probe=false){
  // MCP validation/refusal errors may omit structured output. Preserve the exact error;
  // the already verified connection is not a reason to open a replacement transport.
  if(result.isError&&!probe&&!result.structuredContent?.server)return;
  if(result.isError&&probe)throw Error('persistent_peer_probe_failed');
  const data=result.structuredContent??JSON.parse(result.content.find((c:any)=>c.type==='text').text);
  const peer=data?.server;
  for(const [key,value] of Object.entries(this.config.peer))if(peer?.[key]!==value)throw Error('persistent_peer_identity_mismatch:'+key);
  if(peer?.workplace_sha256!==this.workplace)throw Error('persistent_peer_workplace_mismatch');
 }
 async startManual(transport:any){
  if(this.state!=='idle')throw Error('persistent_explicit_new_owner_required');
  if(processWorkplaceOwners.has(this.workplace))throw Error('persistent_workplace_busy');
  processWorkplaceOwners.set(this.workplace,this.generation);
  return this.connectBackend(transport);
 }
 assertReconnectReady(){
  if(this.transitioning||this.state!=='offline')throw Error('persistent_reconnect_requires_offline');
  if(this.pending||this.attaching||this.attachingCount)throw Error('persistent_request_pending');
  if(processWorkplaceOwners.get(this.workplace)!==this.generation)throw Error('persistent_workplace_owner_lost');
 }
 async reconnectManual(factory:()=>any,verifyClosed:()=>Promise<void>){
  this.assertReconnectReady();this.transitioning=true;
  try{
   await this.backend?.close();
   // Native App verification helpers share the owner's OS containment tree.
   // Close only these owned helpers, keeping the frontend leases and IDs. The
   // next call bootstraps from its original trusted in-memory context and
   // rechecks current controller authority; it cannot reuse a stale grant.
   for(const attachment of this.attachments.values()){attachment.suspended=true;await attachment.app.close();}
   // Stdio close alone does not prove that its SSH descendants exited.
   await verifyClosed();
   return await this.connectBackend(factory());
  }finally{this.transitioning=false;}
 }
 private async connectBackend(transport:any){
  this.state='starting';this.starts++;
  const backend=this.dependencies.backend?.()??new Client({name:'harness-persistent-owner',version:'1'},{capabilities:{}});this.backend=backend;
  backend.onclose=()=>{if(this.backend===backend&&['online','starting'].includes(this.state)){this.state='offline';this.lastDisconnectReason='transport_closed';}};
  try{
   await backend.connect(transport);
   const listing=await backend.listTools();
   if(JSON.stringify(listing.tools.map((t:any)=>t.name).sort())!==JSON.stringify([...canonicalTools].sort()))throw Error('persistent_tool_surface_mismatch');
   if(this.tools.length&&sha(JSON.stringify(listing.tools))!==sha(JSON.stringify(this.tools)))throw Error('persistent_reconnect_catalog_changed');
   if(this.submissions){const properties=listing.tools.find((t:any)=>t.name==='terminal_exec')?.inputSchema?.properties;if(!properties?.submission_id||!properties?.recover_submission||!properties?.submission_cursor)throw Error('persistent_submission_runtime_required');}
   this.verifyPeer(await backend.callTool({name:'terminal_read',arguments:{terminal_id:this.config.terminalIds[0],max_output_bytes:1}}),true);
   if(this.state!=='starting')throw Error('persistent_backend_lost_during_start');
   this.tools=listing.tools;this.state='online';return this.status();
  }catch(error){this.state='offline';this.lastDisconnectReason='connection_validation_failed';await backend.close();throw error;}
 }
 async attach(env:NodeJS.ProcessEnv){
  if(this.state!=='online')throw Error('persistent_service_not_online');
  if(this.attaching)throw Error('persistent_frontend_busy');
  if(this.attachments.size+this.attachingCount>=64)throw Error('persistent_frontend_limit');
  this.attachingCount++;let app:NativeAppPort|undefined;
  try{
   if(!this.scopePolicies.length&&!this.config.authorityStores?.length)app=await verifyPersistentCaller(this.effectiveConfig(),env,this.dependencies.app?.());
   else{
    app=this.dependencies.app?.()??new NativeAppPort();await app.connect(this.config.appServerPath,env);
    const caller=app.identity().threadId,data=await app.read(caller);
    this.authorizedPolicies(caller,data);
   }
   if(this.state!=='online')throw Error('persistent_service_not_online');
   if(this.state!=='online')throw Error('persistent_service_not_online');
   const id=randomUUID();this.attachments.set(id,{id,app,env:{...env}});return {id,generation:this.generation};
  }catch(error){await app?.close();throw error;}finally{this.attachingCount--;}
 }
 private authorizedPolicies(caller:string,data:any){
  if(data.thread?.id!==caller||data.thread.hostId!=='local')throw Error('persistent_app_context_mismatch');
  const cwd=canonicalPath(data.thread.cwd);
  this.refreshRegisteredScopes(caller,cwd);
  const policies=this.policies().filter(p=>canonicalPath(p.config.workspace)===cwd&&p.activeBinding.chat===caller);
  if(!policies.length)throw Error('persistent_app_context_mismatch');
  const authorized=policies.filter(p=>{try{assertPersistentAuthority(p.effectiveConfig(),caller);return true;}catch{return false;}});
  if(!authorized.length)throw Error('persistent_authority_stale');
  return authorized;
 }
 private refreshRegisteredScopes(caller:string,cwd:string){
  for(const store of this.config.authorityStores??[]){
   if(canonicalPath(store.workspace)!==cwd)continue;
   const db=new DatabaseSync(store.queuePath,{readOnly:true});
   try{
    db.exec('BEGIN');
    const rows=db.prepare('SELECT scope,binding FROM publisher_routes LIMIT 1025').all() as any[];
    if(rows.length>1024)throw Error('persistent_authority_store_too_large');
    for(const row of rows){
     const binding=JSON.parse(row.binding);
     if(typeof binding.task_id!=='string'||!binding.task_id||typeof binding.work_unit_id!=='string'||!binding.work_unit_id||typeof binding.origin_chat_id!=='string'||!binding.origin_chat_id||row.scope!==controllerScope(binding.task_id,binding.work_unit_id))throw Error('persistent_registered_scope_invalid');
     const current=currentController(db,row.scope);
     if(current.chat!==caller)continue;
     const known=this.policies().find(p=>p.config.queuePath===store.queuePath&&controllerScope(p.activeBinding.taskId,p.activeBinding.workUnitId)===row.scope);
     if(known)continue; // A loaded binding changes only through explicit rebind.
     if(this.scopePolicies.length>=256)throw Error('persistent_authority_store_too_large');
     const initial={chat:binding.origin_chat_id,epoch:0,taskId:binding.task_id,workUnitId:binding.work_unit_id};
     const target={...initial,chat:current.chat,epoch:current.epoch};
     const config={...this.config,...store,binding:target,additionalScopes:undefined,authorityStores:undefined,jobIds:undefined,recoveryRefs:undefined};
     assertTransferChain(config,initial,target);
     // Registration remains owned by the existing trusted publisher. Discovery is
     // read-only and never creates grants, queues, commands or another transport.
     this.scopePolicies.push(new PersistentHarnessOwner(config,this.dependencies));
    }
   }finally{db.close();}
  }
 }
 private async authorize(id:string){
  const attachment=this.attachments.get(id);
  if(!attachment)throw Error('persistent_attachment_stale');
  if(this.state!=='online')throw Error('persistent_service_not_online');
  if(attachment.suspended){
   if(!attachment.reopening)attachment.reopening=(async()=>{await attachment.app.connect(this.config.appServerPath,attachment.env);attachment.suspended=false;})();
   try{await attachment.reopening;}finally{attachment.reopening=undefined;}
  }
  const caller=attachment.app.identity().threadId;
  const data=await attachment.app.read(caller);
  const policies=this.authorizedPolicies(caller,data);
  if(this.attachments.get(id)!==attachment||this.state!=='online')throw Error('persistent_attachment_stale');
  return policies;
 }
 async listTools(id:string){await this.authorize(id);return this.catalog();}
 private checkRequest(request:any){
  this.validateRecoveryRefs();
  const a=request?.arguments??{},name=request?.name,b=this.activeBinding;
  if(!canonicalTools.includes(name)||!a||typeof a!=='object'||Array.isArray(a))throw Error('persistent_tool_policy_rejected');
  if(this.config.toolMode!=='full'){
   if(name!=='terminal_read'||Object.keys(a).some(k=>!['terminal_id','after_cursor','max_output_bytes'].includes(k)))throw Error('persistent_read_policy_rejected');
  }else{
   if(name==='stage_execute'){
    if(a.task_capsule_id!==b.taskId||a.work_unit_id!==b.workUnitId||a.release_id!==this.config.peer.release_id)throw Error('persistent_task_scope_rejected');
   }else if(['stage_status','stage_collect'].includes(name)){
    if(!this.jobs.has(a.job_id))throw Error('persistent_job_scope_rejected');
   }else if(['terminal_exec','terminal_open','artifact_store','artifact_inspect','artifact_apply'].includes(name)){
    if(a.task_id!==b.taskId)throw Error('persistent_task_scope_rejected');
    if(name==='terminal_exec'&&['cancel','finish_maintenance','recover_worker','recover_submission'].some(k=>a[k]!==undefined&&typeof a[k]!=='boolean'))throw Error('persistent_command_flags_rejected');
    if(name==='terminal_exec'&&a.submission_id!==undefined&&a.submission_id!==null)throw Error('persistent_program_submission_rejected');
    if(name==='terminal_exec'&&a.command&&(a.cancel||a.recover_worker||a.recover_submission))throw Error('persistent_command_combination_rejected');
    if(name==='terminal_exec'&&(a.worker_task||a.command)&&a.work_unit_id!==b.workUnitId)throw Error('persistent_work_unit_rejected');
   }
  }
  if(name.startsWith('terminal_')&&name!=='terminal_exec'&&!this.config.terminalIds.includes(a.terminal_id))throw Error('persistent_read_policy_rejected');
  if(name==='terminal_read'&&(!Number.isSafeInteger(a.after_cursor??0)||(a.after_cursor??0)<0||!Number.isSafeInteger(a.max_output_bytes??8192)||(a.max_output_bytes??8192)<1||(a.max_output_bytes??8192)>8192))throw Error('persistent_read_policy_rejected');
 }
 async callTool(id:string,request:any){
  if(this.state!=='online')throw Error('persistent_service_not_online');
  if(this.attaching)throw Error('persistent_frontend_busy');
  if(!this.attachments.has(id))throw Error('persistent_attachment_stale');
  if(this.pending>=64)throw Error('persistent_request_queue_full');
  let submission:{record:SubmissionRecord}|undefined;
  const args=request?.arguments??{};
  if(this.submissions&&request?.name==='terminal_exec'&&typeof args.command==='string'&&!args.command_id&&!args.worker_task){
   const caller=this.attachments.get(id)!.app.identity().threadId;
   const candidate=this.policies().find(p=>p.activeBinding.chat===caller&&p.activeBinding.taskId===args.task_id&&p.activeBinding.workUnitId===args.work_unit_id);
   if(!candidate)throw Error('persistent_task_scope_rejected');
   candidate.checkRequest(request);assertPersistentAuthority(candidate.effectiveConfig(),caller);
   submission={record:this.submissions.begin(candidate.activeBinding,this.generation,args)};
  }
  const previous=this.callTail;let release!:()=>void;
  this.callTail=new Promise<void>(resolve=>{release=resolve;});this.pending++;
  try{await previous;return await this.callAuthorized(id,request,submission);}
  catch(error){if(submission&&this.submissions&&['queued','sent'].includes(submission.record.phase))submission.record=this.submissions.transition(submission.record,submission.record.phase==='queued'?'denied':'unknown');throw error;}
  finally{this.pending--;release();}
 }
 private async callBackend(request:any){
  try{
   const result=await this.backend.callTool(request,undefined,{timeout:60000});this.verifyPeer(result);return result;
  }catch(error){
   if(error instanceof McpError&&![ErrorCode.ConnectionClosed,ErrorCode.RequestTimeout].includes(error.code))throw error;
   this.state='offline';this.lastDisconnectReason='transport_call_failed';await this.backend.close();throw error;
  }
 }
 private async callAuthorized(id:string,request:any,submission?:{record:SubmissionRecord}){
  // Authorization and original command/lease semantics remain in the backend.
  // First authenticate the actual App caller, then resolve only its authorized scope.
  const policies=await this.authorize(id);let policy:PersistentHarnessOwner|undefined,lastError:unknown;
  for(const candidate of policies){try{candidate.checkRequest(request);if(this.multiScope()&&['terminal_send','terminal_close'].includes(request.name)){const terminal=this.terminalBindings.get(request.arguments?.terminal_id);if(terminal?.scope!==controllerScope(candidate.activeBinding.taskId,candidate.activeBinding.workUnitId))throw Error('persistent_terminal_scope_rejected');}policy=candidate;break;}catch(error){lastError=error;}}
  if(!policy)throw lastError??Error('persistent_task_scope_rejected');
  const a=request?.arguments??{};
  if(request.name==='terminal_exec'&&a.recover_submission===true)return this.recoverSubmissions(id,a,policies);
  if(this.multiScope()&&request.name==='terminal_open'){
   const binding=this.terminalBindings.get(a.terminal_id);
   if(binding&&(binding.chat!==policy.activeBinding.chat||binding.scope!==controllerScope(policy.activeBinding.taskId,policy.activeBinding.workUnitId)))throw Error('persistent_terminal_scope_rejected');
  }
  if(this.multiScope()&&['terminal_send','terminal_close'].includes(request.name)){
   const binding=this.terminalBindings.get(a.terminal_id);
   if(!binding||!policies.some(p=>p.activeBinding.chat===binding.chat&&controllerScope(p.activeBinding.taskId,p.activeBinding.workUnitId)===binding.scope))throw Error('persistent_terminal_scope_rejected');
  }
  let argumentsToForward={...a};
  if(request.name==='terminal_exec'&&a.command_id&&(a.cancel===true||a.finish_maintenance===true)){
   if(a.command||a.worker_task||a.recover_worker)throw Error('persistent_command_mutation_rejected');
   // Resolve the saved backend receipt, never a model-supplied or remembered unit.
   // This is an ordinary original-ID lookup, not another submission or cancellation.
   const receipt=await this.callBackend({name:'terminal_exec',arguments:{task_id:a.task_id,command_id:a.command_id,wait_ms:0,max_preview_bytes:1}});
   if(receipt.isError||receipt.structuredContent?.ok===false)return receipt;
   const bound=receipt.structuredContent;
   if(bound?.ok!==true||bound.task_id!==a.task_id||bound.command_id!==a.command_id||typeof bound.work_unit_id!=='string'||!bound.work_unit_id.trim())throw Error('persistent_command_binding_required');
   if(a.work_unit_id!==undefined&&a.work_unit_id!==null&&a.work_unit_id!==bound.work_unit_id)throw Error('persistent_command_scope_rejected');
   // Receipt lookup can wait on the backend. Recheck current authority after it.
   const currentPolicies=await this.authorize(id);
   policy=currentPolicies.find(p=>p.activeBinding.taskId===bound.task_id&&p.activeBinding.workUnitId===bound.work_unit_id);
   if(!policy)throw Error('persistent_command_scope_rejected');
   argumentsToForward={...a,work_unit_id:bound.work_unit_id};
   policy.checkRequest({name:request.name,arguments:argumentsToForward});
  }
   if(submission&&this.submissions){
    const saved=submission.record;
    if(policy.activeBinding.taskId!==saved.taskId||policy.activeBinding.workUnitId!==saved.workUnitId||sha(a.command)!==saved.commandSha256)throw Error('persistent_submission_scope_rejected');
    submission.record=this.submissions.transition(saved,'sent');argumentsToForward={...argumentsToForward,submission_id:saved.submissionId};
   }
   const result=await this.callBackend({name:request.name,arguments:argumentsToForward});
   const data=result.structuredContent;
   if(submission&&this.submissions){
    const saved=submission.record;
    if(!result.isError&&data?.ok===true){
     if(data.command_id!==saved.commandId||data.task_id!==saved.taskId||data.work_unit_id!==saved.workUnitId||data.submission_id!==saved.submissionId||data.command_sha256!==saved.commandSha256)throw Error('persistent_submission_receipt_mismatch');
     submission.record=this.submissions.transition(saved,'received');
    }else submission.record=this.submissions.transition(saved,'unknown');
   }
   if(request.name==='stage_execute'&&!result.isError&&data?.ok===true&&typeof data.job_id==='string')policy.jobs.add(data.job_id);
   if(this.multiScope()&&!result.isError&&data?.ok===true){
    if(request.name==='terminal_open')this.terminalBindings.set(a.terminal_id,{chat:policy.activeBinding.chat,taskId:policy.activeBinding.taskId,scope:controllerScope(policy.activeBinding.taskId,policy.activeBinding.workUnitId)});
    if(request.name==='terminal_close')this.terminalBindings.delete(a.terminal_id);
   }
   return result;
 }
 private async recoverSubmissions(id:string,args:any,policies:PersistentHarnessOwner[]){
  if(!this.submissions)throw Error('persistent_submission_journal_required');
  if(args.command||args.command_id||args.worker_task||args.cancel||args.finish_maintenance||args.recover_worker)throw Error('persistent_submission_recovery_effect_rejected');
  if(args.work_unit_id!==undefined&&!policies.some(p=>p.activeBinding.taskId===args.task_id&&p.activeBinding.workUnitId===args.work_unit_id))throw Error('persistent_submission_scope_rejected');
  const cursor=args.submission_cursor??0;
  if(!Number.isSafeInteger(cursor)||cursor<0||cursor>4096)throw Error('persistent_submission_cursor_rejected');
  const owns=(p:PersistentHarnessOwner,r:SubmissionRecord)=>p.activeBinding.taskId===r.taskId&&p.activeBinding.workUnitId===r.workUnitId;
  const all=this.submissions.list(args.task_id,args.work_unit_id??undefined).filter(r=>policies.some(p=>owns(p,r)));
  const selected=all.slice(cursor,cursor+16),receipts:any[]=[];
  for(let saved of selected){
   if(!(await this.authorize(id)).some(p=>owns(p,saved)))throw Error('persistent_submission_scope_rejected');
   if(['queued','denied'].includes(saved.phase)){receipts.push({command_id:saved.commandId,work_unit_id:saved.workUnitId,phase:saved.phase,input_available:!!saved.input,lookup_only:true,disposition:saved.phase==='queued'?'no_dispatch_receipt_do_not_resubmit':'denied_before_dispatch'});continue;}
   const result=await this.callBackend({name:'terminal_exec',arguments:{task_id:saved.taskId,work_unit_id:saved.workUnitId,command_id:saved.commandId,wait_ms:0,max_preview_bytes:1}});
   const data=result.structuredContent;
   if(!result.isError&&data?.ok===true){
    if(data.command_id!==saved.commandId||data.task_id!==saved.taskId||data.work_unit_id!==saved.workUnitId||data.submission_id!==saved.submissionId||data.command_sha256!==saved.commandSha256)throw Error('persistent_submission_receipt_mismatch');
    if(saved.phase!=='received')saved=this.submissions.transition(saved,'received');
   }else if(saved.phase==='sent')saved=this.submissions.transition(saved,'unknown');
   receipts.push({command_id:saved.commandId,work_unit_id:saved.workUnitId,phase:saved.phase,input_available:!!saved.input,lookup_only:true,response:result});
  }
  return {content:[],structuredContent:{ok:true,schema:'persistent-submission-recovery/v1',lookup_only:true,task_id:args.task_id,receipts,next_cursor:cursor+selected.length<all.length?cursor+selected.length:null,owner:{generation:this.generation,backendStarts:this.starts},research_acceptance:'not_assessed'}};
 }
 async detach(id:string){const old=this.attachments.get(id);if(!old)return;this.attachments.delete(id);await old.app.close();}
 async stopManual(){
  if(this.transitioning||this.state==='starting')throw Error('persistent_connection_transition_pending');
  if(this.pending||this.attaching||this.attachingCount)throw Error('persistent_request_pending');
  this.state='closed';for(const id of [...this.attachments.keys()])await this.detach(id);await this.backend?.close();
  if(processWorkplaceOwners.get(this.workplace)===this.generation)processWorkplaceOwners.delete(this.workplace);
 }
}

// Explicit manual entry helper; neither a tool call nor an attachment invokes this.
export function declaredBackendTransport(configPath:string,env:NodeJS.ProcessEnv){
 const c=JSON.parse(readFileSync(configPath,'utf8')).mcpServers?.solution_agent_harness;
 if(!c?.command||!Array.isArray(c.args)||!c.cwd)throw Error('invalid_installed_harness_config');
 if(c.env!==undefined&&(!c.env||typeof c.env!=='object'||Array.isArray(c.env)||Object.values(c.env).some(v=>typeof v!=='string')))throw Error('invalid_installed_harness_environment');
 return new StdioClientTransport({command:c.command,args:[...c.args],cwd:c.cwd,env:{...env,...c.env} as any,stderr:'pipe'});
}

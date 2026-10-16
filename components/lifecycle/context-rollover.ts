import {DatabaseSync} from 'node:sqlite';
import {existsSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {digest} from '../goal-routing-integration/store.ts';
import {ReviewedStore} from './reviewed-store.ts';
import {NativeAppPort} from './native-app-port.ts';
import {sessionAppContext,connectHookApp} from './session-app-context.ts';
import {controllerRootAdmission,currentController,initializeControllerAuthority,transferControllers,type ControllerGrant} from './controller-authority.ts';
import {readRollover,rolloverKey,rolloverChatId as canonicalId,type RolloverCheckpoint} from './controller-authority.ts';
export {readRollover,rolloverKey,rolloverAdmission,pendingRollover} from './controller-authority.ts';
import {requestLifecycleHandoff} from './lifecycle-handoff.ts';
import type {SessionLifecycleConfig} from './session-lifecycle.ts';

export type RolloverPolicy={authorizationRef:string,projectId:string,cliPath:string,configPath:string};
type Scope={taskId:string,workUnitId:string,epoch:number};
// Hook stdout has a strict product schema. Extra receipt fields invalidate even
// continue:false; keep all IDs/state in the original queue, never on this wire.
export function contextRolloverHookOutput(state:string){return state==='requested'?{continue:false,stopReason:'Harness context handoff durably requested'}:{};}
export function rolloverPolicy(config:SessionLifecycleConfig){
 const p=config.rollover;
 if(!p||Object.keys(p).sort().join(',')!=='authorizationRef,cliPath,configPath,projectId'||
  !p.authorizationRef?.trim()||p.authorizationRef.length>512||!p.projectId?.trim()||p.projectId.length>256||
  !isAbsolute(p.cliPath??'')||!isAbsolute(p.configPath??'')||!isAbsolute(config.workspace)||!isAbsolute(config.manager?.stateRoot??''))throw Error('invalid_rollover_configuration');
 return p;
}
const policyHash=(p:RolloverPolicy)=>digest(JSON.stringify([p.authorizationRef,p.projectId,resolve(p.cliPath),resolve(p.configPath)]));
const save=(store:ReviewedStore,c:RolloverCheckpoint)=>store.db.prepare('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(rolloverKey,JSON.stringify(c));
function configured(c:RolloverCheckpoint,p:RolloverPolicy){if(c.policySha256!==policyHash(p)||c.authorizationRef!==p.authorizationRef)throw Error('rollover_policy_changed');}
async function verifiedApp(config:SessionLifecycleConfig,caller:string,env:NodeJS.ProcessEnv,app:NativeAppPort){
 const result=await connectHookApp(app,config.manager.appServerPath,{...env,CODEX_THREAD_ID:caller});
 if(!result.pass)throw Error('rollover_app_not_verified:'+result.reason);
 const snapshot=await app.read(caller);
 if(snapshot.thread?.id!==caller||snapshot.thread.hostId!=='local'||typeof snapshot.thread.cwd!=='string'||resolve(snapshot.thread.cwd)!==resolve(config.workspace))throw Error('rollover_app_workspace_mismatch');
}
async function ownQueue(config:SessionLifecycleConfig,caller:string){
 const root=config.manager.stateRoot,path=resolve(root,'queue.sqlite');
 if(!existsSync(path))throw Error('controller_store_not_found');
 const wait=config.ownerWaitMs??10000;if(!Number.isInteger(wait)||wait<1||wait>60000)throw Error('invalid_controller_owner_wait');
 requestLifecycleHandoff(root,caller);
 const start=Date.now();
 while(true){try{return new ReviewedStore(path);}catch(e:any){if(e.message!=='controller_owner_alive')throw e;if(Date.now()-start>=wait)throw Error('controller_owner_not_quiescent');await delay(100);}}
}
function scopes(store:ReviewedStore,caller:string){
 initializeControllerAuthority(store.db);
 if(!controllerRootAdmission(resolvePath(store),caller).allowed)throw Error('rollover_scope_not_owned');
 const rows=store.db.prepare('SELECT scope,binding FROM publisher_routes ORDER BY scope LIMIT 129').all() as any[];
 if(!rows.length||rows.length>128)throw Error('rollover_scope_capacity');
 return rows.map(row=>{const c=currentController(store.db,row.scope);return {taskId:c.binding.task_id,workUnitId:c.binding.work_unit_id,epoch:c.epoch};}) as Scope[];
}
function resolvePath(store:ReviewedStore){return (store.db.prepare('PRAGMA database_list').all() as any[]).find(r=>r.name==='main').file as string;}

// The hook has no model work to do: the queue/journal already hold continuation.
// Hold the ORIGINAL queue owner lease while snapshotting. No separate registry.
export async function prepareContextRollover(config:SessionLifecycleConfig,event:any,env:NodeJS.ProcessEnv,app=new NativeAppPort()){
 if(event?.hook_event_name!=='PreCompact'||event.trigger!=='auto')return {state:'ignored' as const};
 const policy=rolloverPolicy(config);
 if(!canonicalId(event.session_id)||!canonicalId(event.turn_id))throw Error('invalid_rollover_event');
 const context=sessionAppContext(config.workspace,[event.session_id],{...event,hook_event_name:'SessionStart',source:'resume'},env);
 if(context.state!=='ready')throw Error('trusted_controller_context_required');
 try{
  await verifiedApp(config,event.session_id,context.env!,app);
  if(!app.canCreateSuccessor())throw Error('native_app_create_unavailable');
  const path=resolve(config.manager.stateRoot,'queue.sqlite');
  if(!controllerRootAdmission(path,event.session_id).allowed)throw Error('rollover_scope_not_owned');
  const store=await ownQueue(config,event.session_id);
  try{return store.tx(()=>{
   const current=scopes(store,event.session_id),prior=readRollover(store.db),id=digest(JSON.stringify([event.session_id,event.turn_id]));
   if(prior&&prior.phase!=='transferred'){
    configured(prior,policy);if(prior.fromChat!==event.session_id)throw Error('rollover_already_pending');
    return {state:'requested' as const,id:prior.id};
   }
   const checkpoint:RolloverCheckpoint={schema:'context-rollover/v1',id,fromChat:event.session_id,turnId:event.turn_id,authorizationRef:policy.authorizationRef,
    policySha256:policyHash(policy),scopes:current,phase:'requested'};
   save(store,checkpoint);return {state:'requested' as const,id};
  });}finally{store.close();}
 }finally{await app.close();}
}

export async function createContextSuccessor(config:SessionLifecycleConfig,id:string,env:NodeJS.ProcessEnv,app=new NativeAppPort()){
 const policy=rolloverPolicy(config),caller=env.CODEX_THREAD_ID;
 if(!canonicalId(caller)||!/^([0-9a-f]{64})$/.test(id))throw Error('invalid_rollover_identity');
 try{
  await verifiedApp(config,caller,env,app);
  const preflightDb=new DatabaseSync(resolve(config.manager.stateRoot,'queue.sqlite'),{readOnly:true});
  let before:RolloverCheckpoint|undefined;try{before=readRollover(preflightDb);}finally{preflightDb.close();}
  if(!before||before.id!==id||before.fromChat!==caller)throw Error('rollover_identity_mismatch');configured(before,policy);
  if(before.phase==='creating')throw Error('rollover_creation_uncertain');
  if(['created','transferred'].includes(before.phase))return {state:before.phase,threadId:before.toChat};
  if(!controllerRootAdmission(resolve(config.manager.stateRoot,'queue.sqlite'),caller).allowed)throw Error('rollover_scope_not_owned');
  const store=await ownQueue(config,caller);
  try{
   const c=readRollover(store.db);if(!c||c.id!==id||c.fromChat!==caller)throw Error('rollover_identity_mismatch');configured(c,policy);
   if(c.phase==='creating')throw Error('rollover_creation_uncertain');
   if(['created','transferred'].includes(c.phase))return {state:c.phase,threadId:c.toChat};
   if(!app.canCreateSuccessor())throw Error('native_app_create_unavailable');
   if(JSON.stringify(scopes(store,caller))!==JSON.stringify(c.scopes))throw Error('rollover_scope_changed');
   // Commit before RPC. Loss of its ACK is never permission for another create.
   const argv=[process.execPath,...process.execArgv,policy.cliPath,policy.configPath,'--rollover-successor',c.id];
   const command=process.platform==='win32'?'& '+argv.map(a=>"'"+a.replaceAll("'","''")+"'").join(' '):argv.map(a=>"'"+a.replaceAll("'","'\\''")+"'").join(' ');
   const prompt='Continue the authorized Harness controller handoff. Run this existing Lifecycle CLI once in your genuine App executor context; do not supply or guess any chat/session/command ID. Command:\n'+command+'\nRead the original queue/journal/results; do not relaunch workers or accept research results automatically. If identity/epoch/receipt verification fails, stop and report it.';
   if(c.phase==='requested'){
    c.phase='creating';store.tx(()=>save(store,c));
    const created=await app.createSuccessor(policy.projectId,prompt);
    if(!canonicalId(created.threadId)||created.threadId===caller)throw Error('rollover_invalid_successor');
    // Persist the machine-returned ID before any further awaited operation.
    // A failed readback resumes verification of THIS ID, never another create.
    c.toChat=created.threadId;c.phase='verifying';store.tx(()=>save(store,c));
   }
   const snapshot=await app.read(c.toChat!);
   if(snapshot.thread?.id!==c.toChat||snapshot.thread.hostId!=='local'||typeof snapshot.thread.cwd!=='string'||resolve(snapshot.thread.cwd)!==resolve(config.workspace))throw Error('rollover_successor_readback_mismatch');
   c.phase='created';store.tx(()=>save(store,c));return {state:c.phase,threadId:c.toChat};
  }finally{store.close();}
 }finally{await app.close();}
}

export async function acceptContextSuccessor(config:SessionLifecycleConfig,id:string,env:NodeJS.ProcessEnv,app=new NativeAppPort()){
 const policy=rolloverPolicy(config),caller=env.CODEX_THREAD_ID;
 if(!canonicalId(caller)||!env.CODEX_APP_TOOLS_PIPE_PATH?.trim()||!/^([0-9a-f]{64})$/.test(id))throw Error('invalid_rollover_identity');
 try{
  await verifiedApp(config,caller,env,app);
  // The initial successor prompt can arrive before its creation ACK is saved.
  // Read only while the creator still owns the queue. Never infer an identity.
  const path=resolve(config.manager.stateRoot,'queue.sqlite'),start=Date.now(),wait=config.ownerWaitMs??10000;
  while(true){
   const db=new DatabaseSync(path,{readOnly:true});let c:RolloverCheckpoint|undefined;try{c=readRollover(db);}finally{db.close();}
   if(!c||c.id!==id)throw Error('rollover_identity_mismatch');configured(c,policy);
   if(c.phase==='created'||c.phase==='transferred'){if(c.toChat!==caller)throw Error('rollover_successor_mismatch');break;}
   if(!['creating','verifying'].includes(c.phase)||Date.now()-start>=wait)throw Error('rollover_creation_not_acknowledged');await delay(100);
  }
  const store=await ownQueue(config,caller);
  try{
   const c=readRollover(store.db)!;configured(c,policy);
   if(c.id!==id||c.toChat!==caller||!['created','transferred'].includes(c.phase))throw Error('rollover_successor_mismatch');
   const grants:ControllerGrant[]=c.scopes.map(s=>({transferId:digest(c.id+'\0'+JSON.stringify([s.taskId,s.workUnitId])),taskId:s.taskId,workUnitId:s.workUnitId,expectedEpoch:s.epoch,fromChat:c.fromChat,toChat:caller,authorizationRef:c.authorizationRef}));
   // All scopes must still match the exact snapshot. In particular, a newly
   // added direction cannot be silently acquired along with the old scopes.
   const rows=store.db.prepare('SELECT scope FROM publisher_routes').all() as any[];
   if(rows.length!==c.scopes.length)throw Error('rollover_scope_changed');
   initializeControllerAuthority(store.db);
   const receipts=transferControllers(store.db,grants,caller,grant=>grant.fromChat===c.fromChat&&grant.toChat===caller&&grant.authorizationRef===policy.authorizationRef&&
    c.scopes.some(s=>s.taskId===grant.taskId&&s.workUnitId===grant.workUnitId&&s.epoch===grant.expectedEpoch));
   c.phase='transferred';store.tx(()=>save(store,c));
   return {state:'transferred' as const,receipts};
  }finally{store.close();}
 }finally{await app.close();}
}

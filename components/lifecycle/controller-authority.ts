import {DatabaseSync} from 'node:sqlite';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {digest} from '../goal-routing-integration/store.ts';
import {ReviewedStore} from './reviewed-store.ts';
import {requestLifecycleHandoff} from './lifecycle-handoff.ts';
import {setTimeout as delay} from 'node:timers/promises';

export const controllerScope=(task:string,unit:string)=>digest(JSON.stringify([task,unit]));
export type ControllerGrant={transferId:string,taskId:string,workUnitId:string,fromChat:string,toChat:string,expectedEpoch:number,authorizationRef:string};
export function initializeControllerAuthority(db:DatabaseSync){db.exec(`
 CREATE TABLE IF NOT EXISTS controller_authority(scope TEXT PRIMARY KEY,chat TEXT NOT NULL,epoch INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS controller_transfers(id TEXT PRIMARY KEY,scope TEXT NOT NULL,grant_json TEXT NOT NULL,receipt TEXT NOT NULL);
 `);}
export function currentController(db:DatabaseSync,scope:string){
 const route=db.prepare('SELECT binding FROM publisher_routes WHERE scope=?').get(scope) as any;
 if(!route)throw Error('controller_scope_not_registered');
 const binding=JSON.parse(route.binding);
 const row=db.prepare('SELECT chat,epoch FROM controller_authority WHERE scope=?').get(scope) as any;
 return {scope,chat:row?.chat??binding.origin_chat_id,epoch:Number(row?.epoch??0),binding,transferred:!!row};
}
// A grant is supplied by the trusted local manager AFTER human authorization.
// Neither a remote envelope nor a string containing an approval reference grants
// permission. The caller must verify the exact approved grant and App context.
export function transferController(db:DatabaseSync,grant:ControllerGrant,verifiedCaller:string,approve:(grant:ControllerGrant)=>boolean,fault:()=>void=()=>{}){
 return transferControllers(db,[grant],verifiedCaller,approve,fault)[0];
}
function approvedGrants(grants:ControllerGrant[],verifiedCaller:string,approve:(grant:ControllerGrant)=>boolean){
 if(!Array.isArray(grants)||!grants.length||grants.length>128)throw Error('invalid_controller_grants');
 // Snapshot caller data before authorization and mutation. Never allow duplicate
 // scope transitions to turn one batch into an implicit multi-epoch transfer.
 const snapshots=grants.map(grant=>({...grant}));
 const scopes=new Set<string>(),ids=new Set<string>();
 for(const grant of snapshots){
 if(!grant||[grant.transferId,grant.taskId,grant.workUnitId,grant.fromChat,grant.toChat,grant.authorizationRef].some(v=>typeof v!=='string'||!v.trim()||v.length>512)||
    !Number.isSafeInteger(grant.expectedEpoch)||grant.expectedEpoch<0||grant.fromChat===grant.toChat)throw Error('invalid_controller_grant');
 if(verifiedCaller!==grant.toChat||!approve(grant))throw Error('controller_transfer_not_authorized');
 const scope=controllerScope(grant.taskId,grant.workUnitId);
 if(scopes.has(scope)||ids.has(grant.transferId))throw Error('duplicate_controller_transfer');
 scopes.add(scope);ids.add(grant.transferId);
 }
 return snapshots;
}
export function transferControllers(db:DatabaseSync,grants:ControllerGrant[],verifiedCaller:string,approve:(grant:ControllerGrant)=>boolean,fault:()=>void=()=>{}){
 const snapshots=approvedGrants(grants,verifiedCaller,approve);
 db.exec('BEGIN IMMEDIATE');
 try{
 const receipts=snapshots.map(grant=>{
 const scope=controllerScope(grant.taskId,grant.workUnitId),encoded=JSON.stringify(Object.fromEntries(Object.entries(grant).sort(([a],[b])=>a.localeCompare(b))));
  const prior=db.prepare('SELECT grant_json,receipt FROM controller_transfers WHERE id=?').get(grant.transferId) as any;
  if(prior){if(prior.grant_json!==encoded)throw Error('controller_transfer_id_conflict');return JSON.parse(prior.receipt);}
  const old=currentController(db,scope);
  if(old.chat!==grant.fromChat||old.epoch!==grant.expectedEpoch)throw Error('controller_epoch_conflict');
  if(old.epoch>=Number.MAX_SAFE_INTEGER)throw Error('controller_epoch_exhausted');
  const receipt={schema:'controller-transfer/v1',transferId:grant.transferId,scope,previousChat:old.chat,currentChat:grant.toChat,epoch:old.epoch+1,authorizationRef:grant.authorizationRef};
  db.prepare('INSERT INTO controller_authority VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET chat=excluded.chat,epoch=excluded.epoch').run(scope,receipt.currentChat,receipt.epoch);
  db.prepare('INSERT INTO controller_transfers VALUES (?,?,?,?)').run(grant.transferId,scope,encoded,JSON.stringify(receipt));
  fault();return receipt;
 });
 db.exec('COMMIT');return receipts;
 }catch(error){db.exec('ROLLBACK');throw error;}
}
// The CLI has already bootstrapped the successor App identity. It must also own
// the existing queue lease before changing epochs: a pre-submit guard alone
// cannot revoke a remote submission that is already awaiting its ACK.
export async function transferControllersWhenQuiescent(root:string,grants:ControllerGrant[],verifiedCaller:string,approve:(grant:ControllerGrant)=>boolean,
 options:{waitMs?:number,signal?:AbortSignal,now?:()=>number,sleep?:(ms:number)=>Promise<void>}={}){
 const snapshots=approvedGrants(grants,verifiedCaller,approve),wait=options.waitMs??10000;
 if(!Number.isInteger(wait)||wait<1||wait>60000)throw Error('invalid_controller_owner_wait');
 const path=resolve(root,'queue.sqlite');if(!existsSync(path))throw Error('controller_store_not_found');
 if(options.signal?.aborted)throw Error('controller_transfer_cancelled');
 assertRootTransfer(path,snapshots,verifiedCaller);
 requestLifecycleHandoff(root,verifiedCaller);
 const now=options.now??Date.now,start=now();
 const sleep=options.sleep??(ms=>delay(ms,undefined,{signal:options.signal}));
 while(true){
  if(options.signal?.aborted)throw Error('controller_transfer_cancelled');
  let store:ReviewedStore;
  try{store=new ReviewedStore(path);}catch(error:any){
   if(error?.message!=='controller_owner_alive')throw error;
   const remaining=wait-(now()-start);if(remaining<=0)throw Error('controller_owner_not_quiescent');
   await sleep(Math.min(100,remaining));continue;
  }
  try{initializeControllerAuthority(store.db);assertRootTransfer(path,snapshots,verifiedCaller);return transferControllers(store.db,snapshots,verifiedCaller,approve);}
  finally{store.close();}
 }
}
export function assertController(db:DatabaseSync,scope:string,caller:string|undefined,epoch:number){
 const current=currentController(db,scope);
 if(current.epoch!==epoch||caller!==current.chat)throw Error('controller_authority_stale');
 return current;
}
function rootControllers(path:string){
 if(!existsSync(path))return [];
 const db=new DatabaseSync(path,{readOnly:true});
 try{
  const has=(name:string)=>!!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name);
  if(!has('publisher_routes'))return [];
  const rows=db.prepare(has('controller_authority')?
   'SELECT r.scope,r.binding,a.chat,a.epoch FROM publisher_routes r LEFT JOIN controller_authority a ON a.scope=r.scope':
   'SELECT scope,binding,NULL chat,0 epoch FROM publisher_routes').all() as any[];
  return rows.map(row=>{const binding=JSON.parse(row.binding),chat=row.chat??binding.origin_chat_id,epoch=Number(row.epoch??0);
   if(controllerScope(binding.task_id,binding.work_unit_id)!==row.scope||typeof chat!=='string'||!chat.trim()||!Number.isSafeInteger(epoch)||epoch<0)throw Error('controller_scope_unverified');
   return {scope:row.scope,chat,epoch};
  });
 }finally{db.close();}
}
export function controllerRootAdmission(path:string,caller:string){
 const owners=rootControllers(path),owned=owners.filter(owner=>owner.chat===caller).length;
 if(!owners.length)return {allowed:true,reason:'unbound_controller_root'};
 if(owned===owners.length)return {allowed:true,reason:'controller_root_owned'};
 return {allowed:false,reason:owned?'mixed_controller_root':'controller_scope_not_owned'};
}
function assertRootTransfer(path:string,grants:ControllerGrant[],caller:string){
 const owners=rootControllers(path),projected=new Map(owners.map(owner=>[owner.scope,owner.chat]));
 for(const grant of grants){
  const scope=controllerScope(grant.taskId,grant.workUnitId),old=owners.find(owner=>owner.scope===scope);
  if(!old)throw Error('controller_scope_not_registered');
  if(!(old.chat===grant.fromChat&&old.epoch===grant.expectedEpoch)&&!(old.chat===grant.toChat&&old.epoch===grant.expectedEpoch+1))throw Error('controller_epoch_conflict');
  projected.set(scope,grant.toChat);
 }
 if([...projected.values()].some(chat=>chat!==caller))throw Error('controller_root_split_required');
}
export function hasTransferredControllerGrant(path:string,caller:string){
 if(!existsSync(path)||!caller)return false;
 const db=new DatabaseSync(path,{readOnly:true});
 try{
  if(!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='controller_authority'").get())return false;
  const rows=db.prepare('SELECT a.scope,a.epoch,t.grant_json,t.receipt FROM controller_authority a JOIN controller_transfers t ON t.scope=a.scope JOIN publisher_routes r ON r.scope=a.scope WHERE a.chat=?').all(caller) as any[];
  return rows.some(row=>{const grant=JSON.parse(row.grant_json),receipt=JSON.parse(row.receipt);return grant.toChat===caller&&receipt.currentChat===caller&&receipt.epoch===row.epoch&&receipt.scope===row.scope&&typeof grant.authorizationRef==='string'&&!!grant.authorizationRef.trim();});
 }finally{db.close();}
}
// Read-only inspection works even while another process owns the manager lease.
// It never asks the old chat to reply and never claims a lease or launches work.
export function inspectControllerProgress(path:string,task:string,unit:string){
 if(!existsSync(path))throw Error('controller_store_not_found');
 const db=new DatabaseSync(path,{readOnly:true});
 try{
  const scope=controllerScope(task,unit);
  const has=(name:string)=>!!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name);
  const route=db.prepare('SELECT binding FROM publisher_routes WHERE scope=?').get(scope) as any;
  if(!route)throw Error('controller_scope_not_registered');
  const authority=has('controller_authority')?currentController(db,scope):{scope,chat:JSON.parse(route.binding).origin_chat_id,epoch:0,binding:JSON.parse(route.binding),transferred:false};
  const events=(db.prepare('SELECT id,state,objective,result,thread,body FROM events WHERE source=? ORDER BY seq DESC LIMIT 16').all(scope) as any[]).reverse().map(row=>{
   const {result,...event}=row;return {...event,resultAvailable:result!==null,resultPreview:typeof result==='string'?result.slice(0,1024):null};
  });
  const native=has('native_dispatch')?db.prepare('SELECT n.job_id,n.thread,n.state FROM native_dispatch n JOIN events e ON e.id=n.job_id WHERE e.source=? ORDER BY e.seq DESC LIMIT 128').all(scope):[];
  const late=has('controller_late_receipts')?(db.prepare('SELECT l.*,p.path FROM controller_late_receipts l JOIN events e ON e.id=l.job LEFT JOIN help_episodes p ON p.job=l.job WHERE e.source=? ORDER BY e.seq DESC LIMIT 16').all(scope) as any[]).map(row=>({job:row.job,thread:row.thread,receiptSha256:digest(row.receipt),receiptPath:row.path,turn:JSON.parse(row.turn)})):[];
  const resume=has('help_resume_context')?db.prepare('SELECT body FROM help_resume_context WHERE scope=?').get(scope) as any:undefined;
  const context=resume?JSON.parse(resume.body):undefined;
  const receiptRoot=(db.prepare("SELECT value FROM meta WHERE key='help_receipt_root'").get() as any)?.value;
  const reviewRequests=authority.transferred&&receiptRoot?events.filter(event=>event.state==='running'&&native.some((row:any)=>row.job_id===event.id&&row.thread!==authority.chat)).map(event=>({
   path:resolve(receiptRoot,event.id+'.controller-review.json'),
   template:{jobId:event.id,taskId:task,workUnitId:unit,controllerChat:authority.chat,epoch:authority.epoch,
    evidenceRefs:['original taskbook reference','original remote result reference'],text:'JSON string with decision, summary and proposal when appropriate'},
   note:'Review original evidence in the current controller chat; write this scoped receipt and complete the actual App turn. No remote relaunch.'
  })):[];
  return {schema:'controller-progress/v1',authority,events,native,lateReceipts:late,
   reviewRequests,
   counts:db.prepare('SELECT state,count(*) count FROM events WHERE source=? GROUP BY state').all(scope),eventHistoryLimit:16,
   remoteReferences:context?{previous_command_id:context.previous_command_id,mailbox:context.mailbox}:null,
   readOnly:true,relaunchPermitted:false};
 }finally{db.close();}
}

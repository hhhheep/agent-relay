import {existsSync,readFileSync,writeFileSync,renameSync,openSync,fsyncSync,closeSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {networkInterfaces} from 'node:os';

// Operator-owned policy, separate from task/controller state. Consume the attempt
// durably BEFORE opening a transport; toggling or restarting cannot clear a latch.
export class PersistentReconnect {
 private value:{schema:string,enabled:boolean,latched:boolean,nextAttemptAt:number,lastReason:string,attempts:number[]};
 private busy=false;private offlineSince=0;private network='';private stableSince=0;
 constructor(private path:string,private cooldownMs=60000,private now=()=>Date.now()){
  if(!Number.isFinite(cooldownMs)||cooldownMs<60000||cooldownMs>86400000)throw Error('persistent_reconnect_cooldown_invalid');
  this.value=existsSync(path)?JSON.parse(readFileSync(path,'utf8')):{schema:'persistent-reconnect/v1',enabled:false,latched:false,nextAttemptAt:0,lastReason:'not_attempted',attempts:[]};
  const v=this.value;
  if(v.schema!=='persistent-reconnect/v1'||typeof v.enabled!=='boolean'||typeof v.latched!=='boolean'||!Number.isFinite(v.nextAttemptAt)||v.nextAttemptAt<0||typeof v.lastReason!=='string'||!Array.isArray(v.attempts)||v.attempts.length>4096||v.attempts.some(t=>!Number.isFinite(t)||t<0))throw Error('persistent_reconnect_state_invalid');
 }
 private save(){const tmp=this.path+'.'+randomUUID()+'.tmp';writeFileSync(tmp,JSON.stringify(this.value)+'\n',{mode:0o600,flag:'wx'});const fd=openSync(tmp,'r+');try{fsyncSync(fd);}finally{closeSync(fd);}renameSync(tmp,this.path);}
 status(){return {...this.value,busy:this.busy,cooldownSeconds:Math.max(0,Math.ceil((this.value.nextAttemptAt-this.now())/1000))};}
 noteStart(){this.value.nextAttemptAt=Math.max(this.value.nextAttemptAt,this.now()+this.cooldownMs);this.save();}
 setEnabled(enabled:boolean){if(typeof enabled!=='boolean')throw Error('persistent_reconnect_boolean_required');if(this.busy)throw Error('persistent_reconnect_busy');this.value.enabled=enabled;this.save();return this.status();}
 async attempt(mode:'manual'|'automatic',ready:()=>void,connect:()=>Promise<any>){
  if(this.busy)throw Error('persistent_reconnect_busy');
  if(this.now()<this.value.nextAttemptAt)throw Error('persistent_reconnect_cooldown');
  if(mode==='automatic'&&(!this.value.enabled||this.value.latched))throw Error('persistent_reconnect_automatic_latched_or_disabled');
  this.value.attempts=this.value.attempts.filter(t=>t>this.now()-3600000);
  if(mode==='automatic'&&this.value.attempts.length>=3){this.value.latched=true;this.value.lastReason='hourly_budget_exhausted_manual_required';this.save();throw Error('persistent_reconnect_hourly_budget');}
  ready();this.busy=true;
  try{
   this.value.latched=true;this.value.nextAttemptAt=this.now()+this.cooldownMs;this.value.attempts.push(this.now());this.value.lastReason=mode+'_in_progress';this.save();
   const result=await connect();this.value.latched=false;this.value.lastReason=mode+'_connected';this.save();return result;
  }catch(error){this.value.latched=true;this.value.lastReason=mode+'_failed_operator_required';this.save();throw error;}
  finally{this.busy=false;}
 }
 // Address changes only postpone recovery. Healthy transports are never killed.
 // No TCP/SSH probe: the existing transport/keepalive determines actual failure.
 async observe(state:{state:string,pending:number},fingerprint:string,ready:()=>void,connect:()=>Promise<any>){
  const now=this.now();if(fingerprint!==this.network){this.network=fingerprint;this.stableSince=now;}
  if(state.state!=='offline'){this.offlineSince=0;return;}
  if(!this.offlineSince)this.offlineSince=now;
  if(!fingerprint||now-this.stableSince<10000||now-this.offlineSince<10000||state.pending||!this.value.enabled||this.value.latched||this.busy||now<this.value.nextAttemptAt)return;
  try{await this.attempt('automatic',ready,connect);}catch{/* status carries the failure; no retry loop */}
 }
}
export function persistentNetworkFingerprint(){const addresses=Object.entries(networkInterfaces()).flatMap(([name,addresses])=>(addresses??[]).filter(a=>!a.internal).map(a=>[name,a.family,a.address,a.netmask])).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));return addresses.length?JSON.stringify(addresses):'';}

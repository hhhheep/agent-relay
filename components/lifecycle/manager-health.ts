import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';

function ownerState(pid:number){
 try{process.kill(pid,0);return 'alive';}
 catch(error:any){return error?.code==='ESRCH'?'exited':'unknown';}
}

// A status file is a dated observation, never an execution/resource ledger.
// Open the existing ownership database read-only; never claim/release its owner.
export function inspectManagerStatus(root:string,options:{now?:number,maxAgeMs?:number,probeOwner?:(pid:number)=>string}={}){
 const now=options.now??Date.now(),maxAge=options.maxAgeMs??6000;
 let snapshot:any;
 try{snapshot=JSON.parse(readFileSync(resolve(root,'status.json'),'utf8'));}
 catch{return {state:'unknown',reason:'status_unavailable',online:false};}
 if(!snapshot||typeof snapshot!=='object'||Array.isArray(snapshot))return {state:'unknown',reason:'invalid_status',online:false};
 const lastObserved={at:snapshot.at,lifecycle:snapshot.lifecycle,transport:snapshot.transport,
  appAvailability:snapshot.appAvailability,resumeState:snapshot.resumeState,pending:snapshot.pending};
 const base={online:false,lastObserved,ownerPid:snapshot.ownerPid,managerGeneration:snapshot.managerGeneration};
 const observed=typeof snapshot.at==='string'?Date.parse(snapshot.at):NaN,age=now-observed;
 if(!Number.isFinite(observed)||!Number.isFinite(maxAge)||maxAge<1||!Number.isFinite(now)||age<0)return {...base,state:'unknown',reason:'invalid_observation_time'};
 if(!Number.isInteger(snapshot.ownerPid)||snapshot.ownerPid<=0||typeof snapshot.managerGeneration!=='string'||!snapshot.managerGeneration)return {...base,state:'unknown',reason:'owner_identity_missing',ageMs:age};
 let alive:string;
 try{alive=(options.probeOwner??ownerState)(snapshot.ownerPid);}catch{alive='unknown';}
 if(alive==='exited')return {...base,state:'offline',reason:'owner_exited',ageMs:age};
 if(alive!=='alive')return {...base,state:'unknown',reason:'owner_liveness_unavailable',ageMs:age};
 if(age>maxAge)return {...base,state:'stale',reason:'heartbeat_expired',ageMs:age};
 if(snapshot.lifecycle!=='active')return {...base,state:'stopped',reason:'manager_not_active',ageMs:age};
 let db:DatabaseSync|undefined;
 try{
  db=new DatabaseSync(resolve(root,'queue.sqlite'),{readOnly:true});
  const rows=db.prepare("SELECT key,value FROM meta WHERE key IN ('owner','manager_generation')").all() as any[];
  const authority=Object.fromEntries(rows.map(r=>[r.key,r.value]));
  if(authority.owner!==String(snapshot.ownerPid)||authority.manager_generation!==snapshot.managerGeneration)
   return {...base,state:'unknown',reason:'owner_generation_not_current',ageMs:age};
 }catch{return {...base,state:'unknown',reason:'ownership_evidence_unavailable',ageMs:age};}
 finally{db?.close();}
 const online=snapshot.state==='running'&&snapshot.appAvailability==='online'&&snapshot.transport==='connected'&&['ready','disabled'].includes(snapshot.resumeState);
 return {...base,state:online?'online':'degraded',reason:online?'fresh_owned_observation':'connection_not_ready',online,ageMs:age};
}

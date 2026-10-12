import {readFileSync,writeFileSync,renameSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

// Local control message only: no pipe, credentials, task prompt or destination
// change. Exact process generation prevents a delayed request stopping a new owner.
export function requestLifecycleHandoff(root:string,caller:string,now=Date.now()){
 let owner:any;
 try{owner=JSON.parse(readFileSync(resolve(root,'status.json'),'utf8'));}catch{return {state:'owner_not_observed'};}
 if(owner.lifecycle!=='active'||!Number.isInteger(owner.ownerPid)||typeof owner.managerGeneration!=='string')return {state:'owner_not_observed'};
 const request={schema:'manager-handoff/v1',ownerPid:owner.ownerPid,managerGeneration:owner.managerGeneration,
  requestId:randomUUID(),verifiedCaller:caller,requestedAt:now,expiresAt:now+60000};
 mkdirSync(root,{recursive:true});const path=resolve(root,'handoff.json'),temp=path+'.'+process.pid+'.'+request.requestId+'.tmp';
 writeFileSync(temp,JSON.stringify(request),{mode:0o600});renameSync(temp,path);
 return {state:'handoff_requested',ownerPid:request.ownerPid,requestId:request.requestId};
}

export function lifecycleHandoffRequested(root:string,generation:string,pid=process.pid,now=Date.now()){
 let request:any;try{request=JSON.parse(readFileSync(resolve(root,'handoff.json'),'utf8'));}catch{return false;}
 return request.schema==='manager-handoff/v1'&&request.ownerPid===pid&&request.managerGeneration===generation&&
  typeof request.verifiedCaller==='string'&&!!request.verifiedCaller.trim()&&
  Number.isFinite(request.requestedAt)&&Number.isFinite(request.expiresAt)&&request.requestedAt<=now&&
  request.expiresAt>now&&request.expiresAt-request.requestedAt<=60000;
}

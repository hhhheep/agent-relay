import {readFileSync} from 'node:fs';
import {isAbsolute,relative} from 'node:path';
import {request} from 'node:http';

// Opt-in, temporary candidate supervisor only. No general transport fallback.
// Pipe capability is transferred in memory over loopback, never saved/logged.
export async function notifyAcceptance(workspace:string,descriptorPath:string,event:any,pipe:string){
 const rel=relative(workspace,descriptorPath);if(!isAbsolute(descriptorPath)||!rel||rel.startsWith('..')||isAbsolute(rel))throw Error('acceptance_descriptor_outside_workspace');
 const descriptor=JSON.parse(readFileSync(descriptorPath,'utf8'));
 const authorized=descriptor.authorizedExecutorChats??[descriptor.caller];
 if(descriptor.schema!=='a3-restart-supervisor/v1'||descriptor.workspace!==workspace||!Array.isArray(authorized)||!authorized.length||authorized.some((id:any)=>typeof id!=='string'||!id.trim())||!authorized.includes(event.session_id)||Date.now()>descriptor.expiresAt||!Number.isInteger(descriptor.port)||descriptor.port<1||descriptor.port>65535||typeof descriptor.nonce!=='string'||descriptor.nonce.length!==64)throw Error('invalid_acceptance_descriptor');
 const body=JSON.stringify({event,pipe});
 await new Promise<void>((yes,no)=>{
  const req=request({host:'127.0.0.1',port:descriptor.port,path:'/resume',method:'POST',headers:{Authorization:'Bearer '+descriptor.nonce,'Content-Type':'application/json','Content-Length':Buffer.byteLength(body)},signal:AbortSignal.timeout(1500)},res=>{res.resume();res.on('end',()=>res.statusCode===202?yes():no(Error('acceptance_handoff_rejected')));});
  req.on('error',()=>no(Error('acceptance_handoff_unavailable')));req.end(body);
 });
 return {state:'submitted',scope:'candidate supervisor only; inspect its receipt for takeover acceptance'};
}

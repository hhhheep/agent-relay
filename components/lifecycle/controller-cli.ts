import {readFileSync,existsSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {NativeAppPort} from './native-app-port.ts';
import {inspectControllerProgress,transferControllersWhenQuiescent,type ControllerGrant} from './controller-authority.ts';
import {sessionAppContext} from './session-app-context.ts';

if(process.argv.includes('--help'))console.log('controller-cli inspect <queue.sqlite> <task-id> <work-unit-id> | transfer <trusted-local-config.json> <approved-grant.json> --apply; no remote submission');
else{
 const [operation,path,arg1,arg2]=process.argv.slice(2);
 if(operation==='inspect'){
  if(!path||!isAbsolute(path)||!arg1||!arg2)throw Error('absolute_store_and_scope_required');
  console.log(JSON.stringify(inspectControllerProgress(path,arg1,arg2)));
 }else if(operation==='transfer'){
  if(!process.argv.includes('--apply')||!isAbsolute(path??'')||!isAbsolute(arg1??''))throw Error('explicit_controller_transfer_required');
  const config=JSON.parse(readFileSync(path,'utf8')),input=JSON.parse(readFileSync(arg1,'utf8'));
  const grants:ControllerGrant[]=Array.isArray(input)?input:[input];
  if(!grants.length||grants.length>128||grants.some(grant=>!grant||typeof grant.toChat!=='string'||grant.toChat!==grants[0].toChat))throw Error('invalid_controller_grants');
  if(!isAbsolute(config.stateRoot??'')||!isAbsolute(config.workspace??'')||!isAbsolute(config.appServerPath??'')||!Array.isArray(config.approvedControllerTransfers))throw Error('invalid_controller_configuration');
  const approved=grants.every(grant=>config.approvedControllerTransfers.some((candidate:ControllerGrant)=>['transferId','taskId','workUnitId','fromChat','toChat','expectedEpoch','authorizationRef'].every(key=>(candidate as any)[key]===(grant as any)[key])));
  if(!approved)throw Error('controller_transfer_not_authorized');
  const context=sessionAppContext(config.workspace,[grants[0].toChat],{hook_event_name:'SessionStart',source:'resume',session_id:process.env.CODEX_THREAD_ID,cwd:config.workspace},process.env);
  if(context.state!=='ready')throw Error('trusted_controller_context_required');
  const app=new NativeAppPort();
  try{
   await app.connect(config.appServerPath,context.env!);
   const target=resolve(config.stateRoot,'queue.sqlite');if(!existsSync(target))throw Error('controller_store_not_found');
   const receipts=await transferControllersWhenQuiescent(config.stateRoot,grants,app.identity().threadId,()=>approved,{waitMs:config.ownerWaitMs});
   console.log(JSON.stringify(Array.isArray(input)?receipts:receipts[0]));
  }finally{await app.close();}
 }else throw Error('unknown_controller_operation');
}

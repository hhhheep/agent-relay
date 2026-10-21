import {readFileSync,existsSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {NativeAppPort} from './native-app-port.ts';
import {initializeControllerAuthority,inspectControllerProgress,transferController,type ControllerGrant} from './controller-authority.ts';
import {sessionAppContext} from './session-app-context.ts';

if(process.argv.includes('--help'))console.log('controller-cli inspect <queue.sqlite> <task-id> <work-unit-id> | transfer <trusted-local-config.json> <approved-grant.json> --apply; no remote submission');
else{
 const [operation,path,arg1,arg2]=process.argv.slice(2);
 if(operation==='inspect'){
  if(!path||!isAbsolute(path)||!arg1||!arg2)throw Error('absolute_store_and_scope_required');
  console.log(JSON.stringify(inspectControllerProgress(path,arg1,arg2)));
 }else if(operation==='transfer'){
  if(!process.argv.includes('--apply')||!isAbsolute(path??'')||!isAbsolute(arg1??''))throw Error('explicit_controller_transfer_required');
  const config=JSON.parse(readFileSync(path,'utf8')),grant:ControllerGrant=JSON.parse(readFileSync(arg1,'utf8'));
  if(!isAbsolute(config.stateRoot??'')||!isAbsolute(config.workspace??'')||!isAbsolute(config.appServerPath??'')||!Array.isArray(config.approvedControllerTransfers))throw Error('invalid_controller_configuration');
  const matches=(candidate:ControllerGrant)=>['transferId','taskId','workUnitId','fromChat','toChat','expectedEpoch','authorizationRef'].every(key=>(candidate as any)[key]===(grant as any)[key]);
  const approved=config.approvedControllerTransfers.some(matches);
  if(!approved)throw Error('controller_transfer_not_authorized');
  const context=sessionAppContext(config.workspace,[grant.toChat],{hook_event_name:'SessionStart',source:'resume',session_id:process.env.CODEX_THREAD_ID,cwd:config.workspace},process.env);
  if(context.state!=='ready')throw Error('trusted_controller_context_required');
  const app=new NativeAppPort();
  try{
   await app.connect(config.appServerPath,context.env!);
   // The event/configuration cwd is a declaration, not proof of the actual App
   // thread's workspace. Check it before opening or initializing authority.
   await app.assertWorkspace(config.workspace);
   const target=resolve(config.stateRoot,'queue.sqlite');if(!existsSync(target))throw Error('controller_store_not_found');
   const db=new DatabaseSync(target);db.exec('PRAGMA busy_timeout=5000;PRAGMA synchronous=FULL;');
   try{initializeControllerAuthority(db);console.log(JSON.stringify(transferController(db,grant,app.identity().threadId,()=>approved)));}
   finally{db.close();}
  }finally{await app.close();}
 }else throw Error('unknown_controller_operation');
}

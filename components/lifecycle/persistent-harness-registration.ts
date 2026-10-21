import {existsSync,realpathSync} from 'node:fs';
import {NativeAppPort} from './native-app-port.ts';
import {PublisherRoutes} from './publisher-route.ts';
import {ReviewedStore} from './reviewed-store.ts';
import {initializeControllerAuthority} from './controller-authority.ts';
import type {PersistentConfig} from './persistent-harness.ts';

// Explicit local publisher operation only. It neither dispatches work nor contacts
// Harness/SSH, and cannot replace another chat's existing task/work-unit binding.
export async function registerPersistentScope(config:PersistentConfig,env:NodeJS.ProcessEnv,
 input:{taskId:string,workUnitId:string,source:string},dependencies:{app?:()=>NativeAppPort}={}){
 if(!input||[input.taskId,input.workUnitId,input.source].some(v=>typeof v!=='string'||!v.trim()||v.length>256))throw Error('persistent_registration_invalid');
 const app=dependencies.app?.()??new NativeAppPort();
 try{
  await app.connect(config.appServerPath,env);
  const caller=app.identity().threadId,data=await app.read(caller);
  if(data.thread?.id!==caller||data.thread.hostId!=='local')throw Error('persistent_app_context_mismatch');
  const cwd=realpathSync.native(data.thread.cwd);
  const stores=[{workspace:config.workspace,queuePath:config.queuePath},...config.authorityStores??[]];
  const candidates=stores.filter(s=>realpathSync.native(s.workspace)===cwd);
  const paths=[...new Set(candidates.map(s=>realpathSync.native(s.queuePath)))];
  if(paths.length!==1||!existsSync(paths[0]))throw Error('persistent_registration_store_rejected');
  const store=new ReviewedStore(paths[0]);
  try{
   initializeControllerAuthority(store.db);
   const binding=new PublisherRoutes(store).publish(input.source,input.taskId,input.workUnitId,'persistent-local-publisher',{threadId:caller});
   return {schema:'persistent-scope-registration/v1',binding,dispatched:false,backendStarts:0};
  }finally{store.close();}
 }finally{await app.close();}
}

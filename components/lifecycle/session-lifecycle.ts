import {isAbsolute,resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {sessionAppContext,connectHookApp} from './session-app-context.ts';
import {NativeAppPort} from './native-app-port.ts';
import {runManager,validateDriverConfiguration,type DriverConfig} from './manager-driver.ts';
import {requestLifecycleHandoff} from './lifecycle-handoff.ts';
import {hasTransferredControllerGrant,controllerRootAdmission} from './controller-authority.ts';
import {rolloverAdmission,type RolloverPolicy} from './context-rollover.ts';

export type SessionLifecycleConfig={workspace:string,authorizedExecutorChats:string[],manager:DriverConfig,ownerWaitMs?:number,rollover?:RolloverPolicy};
export function validateSessionStart(config:SessionLifecycleConfig,event:any,env:NodeJS.ProcessEnv){
 if(!isAbsolute(config.workspace)||!Array.isArray(config.authorizedExecutorChats)||!config.authorizedExecutorChats.length||config.authorizedExecutorChats.some(x=>typeof x!=='string'||!x.trim()))throw Error('invalid_lifecycle_configuration');
 if(!config.manager||!isAbsolute(config.manager.stateRoot)||!isAbsolute(config.manager.appServerPath))throw Error('invalid_lifecycle_manager_paths');
 const authorized=[...config.authorizedExecutorChats];
 if(typeof event?.session_id==='string'&&!authorized.includes(event.session_id)&&hasTransferredControllerGrant(resolve(config.manager.stateRoot,'queue.sqlite'),event.session_id))authorized.push(event.session_id);
 const context=sessionAppContext(config.workspace,authorized,event,env);if(context.state!=='ready')return context;
 const ownership=controllerRootAdmission(resolve(config.manager.stateRoot,'queue.sqlite'),context.caller!);
 if(!ownership.allowed)return {state:'awaiting_authority',reason:ownership.reason};
 if(!rolloverAdmission(resolve(config.manager.stateRoot,'queue.sqlite'),context.caller!))return {state:'awaiting_authority',reason:'context_rollover_pending'};
 if(!env.HARNESS_XATS_TOKEN)return {state:'awaiting_configuration',reason:'transport_token_not_provided'};
 validateDriverConfiguration(config.manager,env);
 if(!config.manager.appLifecycle?.exitWhenAwaitingApp)throw Error('lifecycle_owner_must_release');
 const wait=config.ownerWaitMs??60000;if(!Number.isInteger(wait)||wait<1||wait>60000)throw Error('invalid_lifecycle_owner_wait');
 return {...context,state:'ready'};
}

// A fresh hook process inherits context in memory. It waits for the existing SQL
// owner to release; it never kills that owner, deletes a lease, or persists env.
export async function runSessionLifecycle(config:SessionLifecycleConfig,event:any,env:NodeJS.ProcessEnv,signal:AbortSignal,
 dependencies:{run?:typeof runManager,now?:()=>number,sleep?:(ms:number,signal:AbortSignal)=>Promise<void>,app?:()=>NativeAppPort}={}){
 const admission=validateSessionStart(config,event,env);if(admission.state!=='ready')return admission;
 const app=dependencies.app?.()??new NativeAppPort();
 let bootstrap;try{bootstrap=await connectHookApp(app,config.manager.appServerPath,admission.env!,signal);}finally{await app.close();}
 if(!bootstrap.pass)return {state:'awaiting_app',reason:bootstrap.reason,bootstrapAttempts:bootstrap.attempts};
 if(signal.aborted)return {state:'cancelled',attempts:0};
 const ownership=controllerRootAdmission(resolve(config.manager.stateRoot,'queue.sqlite'),admission.caller!);
 if(!ownership.allowed)return {state:'awaiting_authority',reason:ownership.reason};
 // Only a freshly verified context may ask the old manager to release. The old
 // manager checks exact PID+generation, stops normally and keeps durable queues.
 const handoff=requestLifecycleHandoff(config.manager.stateRoot,admission.caller!,Date.now(),Math.max(5000,(config.manager.intervalMs??2000)*3));
 const now=dependencies.now??Date.now,sleep=dependencies.sleep??(async(ms,s)=>{await delay(ms,undefined,{signal:s});}),start=now(),budget=config.ownerWaitMs??60000;
 let attempts=0;
 while(!signal.aborted){
  attempts++;
  try{await (dependencies.run??runManager)(config.manager,admission.env!,signal);return {state:'stopped',attempts,handoff};}
  catch(error:any){
   if(error?.message!=='controller_owner_alive')throw error;
   const remaining=budget-(now()-start);if(remaining<=0)return {state:'awaiting_owner',attempts};
   try{await sleep(Math.min(1000,remaining),signal);}catch(error){if(!signal.aborted)throw error;}
  }
 }
 return {state:'cancelled',attempts};
}

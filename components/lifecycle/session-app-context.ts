import {resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {NativeAppPort} from './native-app-port.ts';

// Only the product SessionStart event supplies a missing caller. Never discover
// a pipe or fall back to a configured publication target. Env stays in memory.
export function sessionAppContext(workspace:string,authorized:string[],event:any,env:NodeJS.ProcessEnv){
 if(event?.hook_event_name!=='SessionStart'||!['startup','resume'].includes(event.source))return {state:'ignored' as const,reason:'not_start_or_resume'};
 if(typeof event.cwd!=='string'||resolve(event.cwd)!==resolve(workspace))throw Error('lifecycle_workspace_mismatch');
 const caller=event.session_id;
 if(typeof caller!=='string'||!caller.trim()||!authorized.includes(caller))throw Error('lifecycle_caller_not_authorized');
 if(env.CODEX_THREAD_ID&&env.CODEX_THREAD_ID!==caller)throw Error('lifecycle_caller_mismatch');
 if(!env.CODEX_APP_TOOLS_PIPE_PATH?.trim())return {state:'awaiting_app' as const,reason:'app_pipe_not_provided'};
 return {state:'ready' as const,caller,env:{...env,CODEX_THREAD_ID:caller},callerSource:env.CODEX_THREAD_ID?'matching_event_and_environment':'session_start_event'};
}

export async function connectHookApp(app:NativeAppPort,path:string,env:NodeJS.ProcessEnv,signal?:AbortSignal,
 deps:{now?:()=>number,sleep?:(ms:number)=>Promise<void>}={}){
 const now=deps.now??Date.now,start=now(),budget=10000;let attempts=0;
 const sleep=deps.sleep??(async ms=>{await delay(ms,undefined,{signal});});
 while(attempts<3&&!signal?.aborted){
  const remaining=budget-(now()-start);if(remaining<=0)break;
  attempts++;
  try{await app.connect(path,env,{signal,timeoutMs:Math.min(2500,remaining)});return {pass:true,attempts,reason:'caller_verified'};}
  catch(error:any){
   await app.close();
   if(['native_app_tools_missing','bootstrap_caller_mismatch','bootstrap_host_mismatch','native_app_tool_error','native_app_response_error','missing_trusted_app_context'].includes(error?.message))return {pass:false,attempts,reason:error.message};
   if(attempts<3&&now()-start<budget&&!signal?.aborted){try{await sleep(Math.min(250,budget-(now()-start)));}catch(error){if(!signal?.aborted)throw error;}}
  }
 }
 return {pass:false,attempts,reason:signal?.aborted?'bootstrap_cancelled':'bootstrap_retry_budget_exhausted'};
}

import {isAbsolute,resolve,relative} from 'node:path';
import {mkdirSync,writeFileSync,renameSync} from 'node:fs';import {dirname,basename} from 'node:path';
import {randomUUID} from 'node:crypto';
import {NativeAppPort} from './native-app-port.ts';
import {sessionAppContext,connectHookApp} from './session-app-context.ts';
import {notifyAcceptance} from './session-acceptance-notify.ts';

export type SessionProbeConfig={workspace:string,authorizedExecutorChats:string[],appServerPath:string,targets:string[],receiptPath:string,managerAcceptanceDescriptor?:string};
export async function probeSessionContext(config:SessionProbeConfig,event:any,env:NodeJS.ProcessEnv,app=new NativeAppPort()){
 if(!isAbsolute(config.workspace)||!isAbsolute(config.appServerPath)||!isAbsolute(config.receiptPath)||!Array.isArray(config.authorizedExecutorChats)||!Array.isArray(config.targets)||config.targets.length>8||[...config.authorizedExecutorChats,...config.targets].some(v=>typeof v!=='string'||!v.trim()))throw Error('invalid_probe_configuration');
 const path=relative(config.workspace,config.receiptPath);if(!path||path.startsWith('..')||isAbsolute(path))throw Error('probe_receipt_outside_workspace');
 if(event?.hook_event_name!=='SessionStart'||!['startup','resume'].includes(event.source))return {state:'ignored'};
 if(typeof event.cwd!=='string'||resolve(event.cwd)!==resolve(config.workspace))throw Error('probe_workspace_mismatch');
 const deadline=Date.now()+12000,signal=AbortSignal.timeout(12000);
 let caller:string|undefined;const report:any={schema:'session-context-probe/v1',pass:false,at:new Date().toISOString(),scope:'read-only context/readback; product trigger and App restart require separate evidence',source:event.source,eventSessionId:event.session_id,invocation:env.HARNESS_SESSION_PROBE_MANUAL==='1'?'manual':'trigger_not_independently_verified',targets:[],model_dispatches:0,
  contextPresence:{threadId:!!env.CODEX_THREAD_ID,appPipe:!!env.CODEX_APP_TOOLS_PIPE_PATH},missingContextFields:['CODEX_THREAD_ID','CODEX_APP_TOOLS_PIPE_PATH'].filter(name=>!env[name])};
 try{
  const context=sessionAppContext(config.workspace,config.authorizedExecutorChats,event,env);
  if(context.state!=='ready'){report.state=context.state;report.reason=context.reason;return report;}
  caller=context.caller;report.callerSource=context.callerSource;
  report.stage='bootstrap';const bootstrap=await connectHookApp(app,config.appServerPath,context.env,signal);
  report.bootstrapAttempts=bootstrap.attempts;
  if(!bootstrap.pass){report.state='awaiting_app';report.reason=bootstrap.reason;return report;}
  report.caller=caller;
  report.stage='target_readback';
  for(const id of config.targets){const remaining=deadline-Date.now();if(remaining<=0)throw Error('probe_deadline_exceeded');const result=await app.read(id,{signal,timeoutMs:Math.min(3000,remaining)});if(result.thread?.id!==id||result.thread.hostId&&result.thread.hostId!=='local')throw Error('probe_target_mismatch');report.targets.push({threadId:id,hostId:result.thread.hostId??'unreported',readback:true});}
  report.pass=true;report.stage='complete';report.state='verified_current_context';
  if(config.managerAcceptanceDescriptor){
   try{report.managerAcceptance=await notifyAcceptance(config.workspace,config.managerAcceptanceDescriptor,event,context.env.CODEX_APP_TOOLS_PIPE_PATH!);}
   catch{report.managerAcceptance={state:'not_submitted',reason:'temporary_supervisor_unavailable_or_rejected'};}
  }
  return report;
 }catch(error:any){
  if(['lifecycle_caller_not_authorized','lifecycle_caller_mismatch','lifecycle_workspace_mismatch','probe_target_mismatch'].includes(error?.message)){report.state='rejected';report.reason=error.message;throw error;}
  report.state='awaiting_app';report.reason=signal.aborted?'probe_deadline_exceeded':report.stage==='target_readback'?'target_readback_failed':'bootstrap_failed';return report;
 }finally{
  await app.close();mkdirSync(dirname(config.receiptPath),{recursive:true});
  const content=JSON.stringify(report,null,2)+'\n',id=randomUUID();
  // Preserve each invocation: another chat's hook must not erase acceptance.
  const history=resolve(dirname(config.receiptPath),basename(config.receiptPath,'.json')+'-history');mkdirSync(history,{recursive:true});
  writeFileSync(resolve(history,report.at.replace(/[:.]/g,'-')+'-'+id+'.json'),content,{flag:'wx'});
  const temp=config.receiptPath+'.'+id+'.tmp';writeFileSync(temp,content);renameSync(temp,config.receiptPath);
 }
}

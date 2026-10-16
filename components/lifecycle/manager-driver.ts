import {mkdirSync,writeFileSync,renameSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {ReviewedStore} from './reviewed-store.ts';
import {PublisherRoutes} from './publisher-route.ts';
import {RoutingService} from './routing-service.ts';
import {NativeAppPort} from './native-app-port.ts';
import {connectStableInbox} from './stable-inbox.ts';
import {HarnessPort} from './harness-port.ts';
import {ResumeController} from './resume-controller.ts';
import {ResumeScheduler} from './resume-scheduler.ts';
import {TaskPublisher,WorkerProfile} from './task-publisher.ts';
import {randomUUID} from 'node:crypto';
import {lifecycleHandoffRequested} from './lifecycle-handoff.ts';
import {currentController,assertController,controllerScope} from './controller-authority.ts';
import {rolloverAdmission,pendingRollover} from './context-rollover.ts';

export type DriverConfig={stateRoot:string,url:string,name:string,team:string,appServerPath:string,intervalMs?:number,
 appLifecycle?:{healthIntervalMs:number,exitWhenAwaitingApp:boolean},
 appReconnect?:{maxAttempts?:number,maxElapsedMs?:number,delayMs?:number,connectTimeoutMs?:number},
 resume?:{python:string,rpcScript:string,installedConfig:string,contexts?:Array<{binding:any,context:any}>,publicationJournal?:string,workerProfile?:WorkerProfile,captureVerification?:boolean,
 autoDispatch?:{poolFingerprint:string,releaseId:string,maxPerTick:number,intervalMs:number}}};
export function validateDriverConfiguration(config:DriverConfig,env:NodeJS.ProcessEnv){
 if(!isAbsolute(config.stateRoot)||!isAbsolute(config.appServerPath)||!config.name?.trim()||!config.team?.trim())throw Error('invalid_driver_configuration');
 const interval=config.intervalMs??2000;if(!Number.isInteger(interval)||interval<250||interval>60000)throw Error('invalid_driver_interval');
 if(!env.HARNESS_XATS_TOKEN)throw Error('missing_transport_token');
 let url:URL;try{url=new URL(config.url);}catch{throw Error('invalid_driver_transport_url');}
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('invalid_driver_transport_url');
 const retry={maxAttempts:3,maxElapsedMs:30000,delayMs:250,connectTimeoutMs:5000,...config.appReconnect};
 if(!Number.isInteger(retry.maxAttempts)||retry.maxAttempts<1||retry.maxAttempts>5||!Number.isInteger(retry.maxElapsedMs)||retry.maxElapsedMs<1||retry.maxElapsedMs>60000||!Number.isInteger(retry.delayMs)||retry.delayMs<0||retry.delayMs>10000||!Number.isInteger(retry.connectTimeoutMs)||retry.connectTimeoutMs<1||retry.connectTimeoutMs>30000)throw Error('invalid_app_reconnect_policy');
 const lifecycle=config.appLifecycle;
 if(lifecycle&&(!Number.isInteger(lifecycle.healthIntervalMs)||lifecycle.healthIntervalMs<1000||lifecycle.healthIntervalMs>10000||interval>lifecycle.healthIntervalMs||typeof lifecycle.exitWhenAwaitingApp!=='boolean'))throw Error('invalid_app_lifecycle_policy');
 if(config.resume){
  const resume=config.resume;
  if([resume.python,resume.rpcScript,resume.installedConfig].some(p=>!isAbsolute(p)))throw Error('invalid_resume_config');
  if(!!resume.publicationJournal!==!!resume.workerProfile)throw Error('incomplete_publication_resume_config');
  if(resume.publicationJournal&&!isAbsolute(resume.publicationJournal))throw Error('invalid_publication_journal');
  const auto=resume.autoDispatch;
  if(auto&&(!resume.publicationJournal||!resume.workerProfile||Object.keys(auto).sort().join(',')!=='intervalMs,maxPerTick,poolFingerprint,releaseId'
    ||!/^([0-9a-f]{64})$/.test(auto.poolFingerprint)||typeof auto.releaseId!=='string'||!auto.releaseId.trim()||auto.releaseId.length>256
    ||!Number.isInteger(auto.maxPerTick)||auto.maxPerTick<1||auto.maxPerTick>64
    ||!Number.isInteger(auto.intervalMs)||auto.intervalMs<1000||auto.intervalMs>60000))throw Error('invalid_worker_auto_dispatch');
 }
 return {interval,retry,lifecycle};
}
export async function runManager(config:DriverConfig,env:NodeJS.ProcessEnv,signal:AbortSignal,options:{once?:boolean,onStatus?:(s:any)=>void}={},dependencies:{connectInbox:typeof connectStableInbox,app:()=>NativeAppPort,harness?:(tasks:Set<string>,releaseId?:string)=>HarnessPort,now?:()=>number,sleep?:(ms:number,signal:AbortSignal)=>Promise<void>}={connectInbox:connectStableInbox,app:()=>new NativeAppPort()}){
 const {interval,retry,lifecycle}=validateDriverConfiguration(config,env);
 const now=dependencies.now??Date.now;
 const sleep=dependencies.sleep??(async(ms,s)=>{await delay(ms,undefined,{signal:s});});
 const root=resolve(config.stateRoot);mkdirSync(root,{recursive:true});
 if(!rolloverAdmission(resolve(root,'queue.sqlite'),env.CODEX_THREAD_ID))throw Error('context_rollover_pending');
 const managerGeneration=randomUUID();
 const store=new ReviewedStore(resolve(root,'queue.sqlite')),app=dependencies.app();
 if(pendingRollover(store.db,env.CODEX_THREAD_ID)){store.close();throw Error('context_rollover_pending');}
 store.db.prepare("INSERT INTO meta VALUES ('manager_generation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(managerGeneration);
 let inbox:any,appReady=false,connectionFailures=0,nextConnect=0,lastStatus:any,harness:HarnessPort|undefined,scheduler:ResumeScheduler|undefined,resumeState='disabled',publisher:TaskPublisher|undefined;
 let appAttempts=0,outageAttempts=0,outageStart:number|undefined,nextAppAttempt=0,appReason='initial_context_required';
 let nextHealth=0;
 const allowedTasks=new Set<string>();
 let publicationRecovery:any[]=[];let nextPublicationRecovery=0;
 let publicationDispatch:any[]=[];let nextPublicationDispatch=0;
 const status=(value:any)=>{
  const output={...value,ownerPid:process.pid,managerGeneration,at:new Date().toISOString()};lastStatus=output;
  writeFileSync(resolve(root,'status.tmp'),JSON.stringify(output,null,2));renameSync(resolve(root,'status.tmp'),resolve(root,'status.json'));options.onStatus?.(output);
 };
 const transport={get:(cursor:number)=>{if(!inbox)throw Error('transport_offline');return inbox.get(cursor);},send:(to:string,body:string)=>{if(!inbox)throw Error('transport_offline');return inbox.send(to,body);}};
 const guardedApp={controllerChat:()=>app.identity().threadId,read:(thread:string)=>{if(!appReady)throw Error('app_rebootstrap_required');return app.read(thread);},send:(thread:string,prompt:string)=>{if(!appReady)throw Error('app_rebootstrap_required');return app.send(thread,prompt);}};
 try{
  // Initial bootstrap requires a genuine context. Only a previously verified
  // context may be retried; reconnect never discovers a replacement pipe.
  try{await app.connect(config.appServerPath,env);appReady=true;appReason='initial_context_verified';}catch{await app.close();appReason='initial_context_unavailable';}
  const service=new RoutingService(store,new PublisherRoutes(store),transport,guardedApp,resolve(root,'receipts'));
  if(config.resume){
   const resume=config.resume;
   if(resume.publicationJournal)publisher=new TaskPublisher(resume.publicationJournal);
   for(const c of resume.contexts??[])allowedTasks.add(c.binding.task_id);
   harness=dependencies.harness?.(allowedTasks,resume.autoDispatch?.releaseId)??new HarnessPort(allowedTasks,undefined,resume.autoDispatch?.releaseId);
   const candidate=new ResumeScheduler(store,new ResumeController(resume.python,resume.rpcScript,resolve(root,'resume.sqlite'),harness),scope=>{
    const authority=currentController(store.db,scope);
    const guard=()=>{if(!appReady)throw Error('app_rebootstrap_required');assertController(store.db,scope,app.identity().threadId,authority.epoch);};
    guard();return guard;
   });
   for(const context of resume.contexts??[])candidate.register(context.binding,context.context);
   try{await harness.connect(resume.installedConfig,env);if(resume.autoDispatch)harness.enableWorkerPool(resume.autoDispatch.poolFingerprint);scheduler=candidate;resumeState='ready';}catch{resumeState='harness_reconnect_required';}
  }
  do{
   if(signal.aborted)break;
   if(lifecycle?.exitWhenAwaitingApp&&lifecycleHandoffRequested(root,managerGeneration)){
    status({...lastStatus,lifecycle:'releasing',state:'context_handoff',handoffReason:'verified_new_context'});break;
   }
   try{app.identity();appReady=true;}catch{appReady=false;}
   if(lifecycle&&appReady&&now()>=nextHealth){
    nextHealth=now()+lifecycle.healthIntervalMs;
    try{const caller=app.identity().threadId;const snapshot=await app.read(caller);if(snapshot.thread?.id!==caller||snapshot.thread.hostId&&snapshot.thread.hostId!=='local'){await app.close();throw Error('app_health_identity_mismatch');}}
    catch{appReady=false;appReason=app.canReconnect()?'connection_lost':'new_trusted_context_required';}
   }
   if(!appReady&&app.canReconnect()){
    outageStart??=now();
    const remaining=retry.maxElapsedMs-(now()-outageStart);
    if(outageAttempts<retry.maxAttempts&&remaining>0&&now()>=nextAppAttempt){
     outageAttempts++;appAttempts++;appReason='same_context_reconnect';
     try{
      await app.reconnect({signal,timeoutMs:Math.min(remaining,retry.connectTimeoutMs)});
      appReady=true;appReason='same_context_verified';outageAttempts=0;outageStart=undefined;
     }catch{appReason='same_context_unavailable';nextAppAttempt=now()+Math.min(retry.maxElapsedMs,retry.delayMs*2**(outageAttempts-1));}
    }
    if(!appReady&&(outageAttempts>=retry.maxAttempts||now()-outageStart!>=retry.maxElapsedMs))appReason='same_context_retry_budget_exhausted';
   }
   if(!inbox&&Date.now()>=nextConnect){
    try{inbox=await dependencies.connectInbox(resolve(root,'identity.sqlite'),{url:config.url,token:env.HARNESS_XATS_TOKEN,name:config.name,team:config.team});connectionFailures=0;}
    catch{connectionFailures++;nextConnect=Date.now()+Math.min(60000,1000*2**Math.min(connectionFailures,6));}
   }
   const tick=await service.tick();
   if(scheduler&&harness?.connected){
    try{
     if(publisher&&Date.now()>=nextPublicationRecovery){
      publicationRecovery=await publisher.recoverPending(request=>{allowedTasks.add(request.task_id);return harness!.recoverWorker(request);},(task,command)=>{allowedTasks.add(task);return harness!.recover(task,command);});
      if(publicationRecovery.some(row=>row.state==='finished'))nextPublicationDispatch=0;
      nextPublicationRecovery=Date.now()+30000;
     }
     if(publisher)for(const item of publisher.resumeContexts(config.resume!.workerProfile!,config.resume!.captureVerification)){scheduler.register(item.binding,item.context);allowedTasks.add(item.binding.task_id);}
     const auto=config.resume?.autoDispatch;
     if(publisher&&auto&&appReady&&Date.now()>=nextPublicationDispatch){
      publicationDispatch=await publisher.dispatchReady(auto.maxPerTick,binding=>{
       const scope=controllerScope(binding.task_id,binding.work_unit_id),authority=currentController(store.db,scope);
       const guard=()=>{if(signal.aborted||!appReady)throw Error('app_rebootstrap_required');assertController(store.db,scope,app.identity().threadId,authority.epoch);};
       guard();allowedTasks.add(binding.task_id);return guard;
      },request=>harness!.submit(request));
      nextPublicationDispatch=Date.now()+auto.intervalMs;
     }
     const resumes=await scheduler.tick();resumeState=resumes.some(r=>r.state==='requires_attention')?'resume_requires_attention':resumes.some(r=>r.state==='submission_uncertain')?'submission_uncertain':'ready';}
    catch{resumeState='resume_requires_attention';}
   }
   if(harness&&!harness.connected)resumeState='harness_reconnect_required';
   if(tick.transport.length&&inbox){await inbox.close();inbox=undefined;connectionFailures++;nextConnect=Date.now()+Math.min(60000,1000*2**Math.min(connectionFailures,6));}
   try{app.identity();}catch{if(appReady)appReason=app.canReconnect()?'connection_lost':'new_trusted_context_required';appReady=false;}
   status({lifecycle:'active',state:appReady?'running':'app_rebootstrap_required',transport:inbox?'connected':'offline',
    appAvailability:appReady?'online':app.canReconnect()&&appReason!=='same_context_retry_budget_exhausted'?'reconnect_pending':'awaiting_app',
    appReconnect:{attempts:appAttempts,reason:appReason,...app.diagnostics()},
    agentId:inbox?.agentId??null,pending:store.rows().filter(r=>!['done','human'].includes(r.state)).length,publicationDispatch,
    attention:tick.threads.filter((r:any)=>r.state==='attention').map((r:any)=>({thread:r.thread,state:r.state})),connectionFailures,resumeState,publicationRecovery});
   if(options.once)break;
   if(lifecycle?.exitWhenAwaitingApp&&lastStatus.appAvailability==='awaiting_app')break;
   try{await sleep(interval,signal);}catch(error){if(!signal.aborted)throw error;}
  }while(!signal.aborted);
 }finally{
  try{await inbox?.close();}finally{try{await app.close();await harness?.close();}finally{
   try{publisher?.close();if(lastStatus)status({...lastStatus,lifecycle:'stopped',transport:'disconnected'});}
   finally{store.close();}
  }}
  // Write stopped while we still own the store. After owner release, a fresh
  // manager may publish status; the departing owner must not overwrite it.
 }
}

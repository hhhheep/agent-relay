import {TaskPublisher,TaskSubmission,MarkdownSnapshot,WorkerProfile} from './task-publisher.ts';
import {PublisherRoutes} from './publisher-route.ts';
import {posix} from 'node:path';
import {HarnessOperationError} from './harness-port.ts';
import {initializeControllerAuthority,currentController,assertController,controllerScope} from './controller-authority.ts';

// Independent publications keep their original scopes and journal IDs.
// A failed item does not prevent later items; callers persist each result.
export async function publishTasksIsolated<T>(items:readonly T[],publish:(item:T)=>Promise<any>,save:(item:T,result:any)=>void=()=>{}){
 const results=[];
 for(const item of items){
  let result:any;
  try{const row=await publish(item);result={ok:true,id:row.id,state:row.state,command_id:row.command_id??null};}
  catch(error){const evidence=error instanceof HarnessOperationError?error.evidence:undefined;
   result={ok:false,state:'requires_attention',errorCode:evidence?.code??'publication_failed',
    command_id:evidence?.command_id??evidence?.response?.command_id??null};}
  save(item,result);results.push(result);
 }
 return results;
}

const quote=(s:string)=>"'"+s.replaceAll("'","'\\''")+"'";
export function registryCommand(profile:WorkerProfile,binding:any,markdown?:MarkdownSnapshot){
 if(Object.values(profile).some(s=>typeof s!=='string'||!s.startsWith('/')||s.includes('\\')||s.includes('\n')||posix.normalize(s)!==s))throw Error('invalid_declared_worker_profile');
 const data=Buffer.from(JSON.stringify({binding,markdown})).toString('base64');
 const code=`import sys,json,base64;from pathlib import Path;sys.path.insert(0,${JSON.stringify(profile.runtimeRoot)});from solution_agent.harness.publication_registry import install;p=json.loads(base64.b64decode('${data}',validate=True));print(json.dumps(install(Path(${JSON.stringify(profile.runtimeRoot)})/'config/help-publications.sqlite',p['binding'],markdown=p.get('markdown'),workplace=${JSON.stringify(profile.workplace)})))`;
 return [profile.python,'-c',code].map(quote).join(' ');
}
export function absenceCommand(profile:WorkerProfile,deploymentProfile:string,body:any){
 registryCommand(profile,body.binding);
 const data=Buffer.from(JSON.stringify({binding:body.binding,text:body.request.worker_task,phase:body.request.worker_phase,profile:deploymentProfile})).toString('base64');
 const code=`import sys,json,base64;sys.path.insert(0,${JSON.stringify(profile.runtimeRoot)});from solution_agent.harness.publication_recovery import inspect_unstarted;p=json.loads(base64.b64decode('${data}',validate=True));print(json.dumps(inspect_unstarted(${JSON.stringify(profile.runtimeRoot)},p['profile'],p['binding'],p['text'],p['phase'])))`;
 return [profile.python,'-c',code].map(quote).join(' ');
}
export async function publishTask(journal:TaskPublisher,routes:PublisherRoutes,input:TaskSubmission,app:any,
 profile:WorkerProfile,harness:any,options:{markdownPlan?:any,recoverRejected?:boolean,existingId?:string,recoverUnstarted?:boolean,deploymentProfile?:string}={}){
 const plan=options.markdownPlan;
 // Pin the selected runtime/workplace before any remote side effect. An
 // existing journal cannot silently migrate to a candidate namespace.
 journal.resumeContexts(profile);
 const snapshot=plan?{version:plan.version,manifest:plan.manifest}:undefined;
 let prepared:any;
 if(options.existingId){
  const old=journal.inspect(options.existingId),body=JSON.parse(old.body);
  const original={task_id:body.binding.task_id,work_unit_id:body.binding.work_unit_id,worker_id:body.binding.worker_id,source:body.source,
   worker_task:body.request.worker_task,worker_phase:body.request.worker_phase,timeout_seconds:body.request.timeout_seconds};
  if(Object.keys(input).length!==Object.keys(original).length||Object.keys(original).some(k=>(input as any)[k]!==original[k])||JSON.stringify(body.markdown)!==JSON.stringify(snapshot))throw Error('publication_resume_input_mismatch');
  // Genuine caller stays the current executor; destination is the immutable
  // journal binding, not a fabricated caller or a new publication.
  app.identity();
  prepared={id:old.id,binding:body.binding};
 }else prepared=await journal.prepare(input,app,snapshot);
 // Known commands only recover; neither an offline origin nor a new controller
 // is grounds for relaunching them. Side effects below require current scope.
 const known=journal.inspect(prepared.id);
 if(known.command_id)return journal.reconcile(prepared.id,await harness.recover(input.task_id,known.command_id));
 initializeControllerAuthority(routes.store.db);
 const scope=controllerScope(prepared.binding.task_id,prepared.binding.work_unit_id);
 const registered=routes.store.db.prepare('SELECT scope FROM publisher_routes WHERE scope=?').get(scope);
 const epoch=registered?currentController(routes.store.db,scope).epoch:0;
 const guard=()=>{
  const caller=app.identity().threadId;
  if(routes.store.db.prepare('SELECT scope FROM publisher_routes WHERE scope=?').get(scope))assertController(routes.store.db,scope,caller,epoch);
  else if(caller!==prepared.binding.origin_chat_id)throw Error('controller_authority_stale');
 };
 guard();
 if(options.recoverUnstarted){
  if(!options.existingId||!options.deploymentProfile)throw Error('declared_legacy_recovery_required');
  await journal.recoverUnstarted(prepared.id,async body=>{
   const receipt=await harness.inspectCommand(input.task_id,absenceCommand(profile,options.deploymentProfile!,body));
   if(receipt.state!=='succeeded'||receipt.exit_code!==0)throw Error('absence_inspection_not_completed');
   return {...receipt,proof:JSON.parse(receipt.stdout?.preview)};
  });
 }
 if(options.recoverRejected)journal.recoverRejected(prepared.id);
 const operation=async(kind:string,command:string)=>{
  const unit='publish-'+prepared.id.slice(0,24)+'-'+kind;
  const request={task_id:input.task_id,work_unit_id:unit,command,access_mode:'writer',timeout_seconds:60,wait_ms:1000,max_preview_bytes:16384};
  return journal.operation(prepared.id,kind,request,{start:r=>{guard();return harness.maintenance(r);},recover:id=>harness.recoverMaintenance(input.task_id,unit,id)});
 };
 await journal.admit(prepared.id,routes,async binding=>{
  const result=await operation('registry',registryCommand(profile,binding,snapshot));
  return JSON.parse(result.stdout?.preview);
 },plan?async expected=>{
  if(plan.workplace!==profile.workplace||!plan.command||Buffer.byteLength(plan.command)>48000)throw Error('invalid_markdown_plan');
  const result=await operation('markdown',plan.command);const receipt=JSON.parse(result.stdout?.preview);
  const directory=profile.workplace+'/.solution-agent/md-versions/'+expected.version;
  if(receipt.directory!==directory)throw Error('markdown_sync_destination_mismatch');
  return receipt;
 }:undefined);
 const row=journal.inspect(prepared.id);
 if(row.command_id){return journal.reconcile(prepared.id,await harness.recover(input.task_id,row.command_id));}
 guard();return journal.submit(prepared.id,request=>{guard();return harness.submit(request);});
}

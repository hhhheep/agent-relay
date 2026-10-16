import {readFileSync} from 'node:fs';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {createHash} from 'node:crypto';

export class HarnessOperationError extends Error {
 constructor(readonly evidence:any){super('harness_operation_not_confirmed:'+evidence.code);}
}
export function rejectedBeforeLaunch(request:any,evidence:any){
 const data=evidence?.response,s=data?.submission;
 return s?.schema==='harness-submission/v1'&&s.outcome==='rejected'&&s.phase==='before_executor'&&
  data.ok===false&&!data.command_id&&data.task_id===request.task_id&&data.work_unit_id===request.work_unit_id&&
  s.worker_phase===request.worker_phase&&s.timeout_seconds===request.timeout_seconds&&
  s.worker_task_sha256===createHash('sha256').update(request.worker_task).digest('hex');
}

export class HarnessPort {
 private client:any;
 private pendingClient:any;
 private maintenanceSupported=false;
 private workerRecoverySupported=false;
 private workerPoolSupported=false;
 private workerPoolFingerprint?:string;
 lastFailure:any;
 get connected(){return this.client!==undefined;}
 constructor(readonly allowedTasks:ReadonlySet<string>,readonly factory=()=>new Client({name:'harness-routing-manager',version:'1'},{capabilities:{}}),readonly expectedReleaseId?:string){}
 async connect(installedConfig:string,env:NodeJS.ProcessEnv){
  if(this.client||this.pendingClient)throw Error('harness_already_connected');
  const config=JSON.parse(readFileSync(installedConfig,'utf8')).mcpServers?.solution_agent_harness;
  if(!config?.command||!Array.isArray(config.args)||!config.cwd)throw Error('invalid_installed_harness_config');
  const startup=config.startup_timeout_sec;
  if(startup!==undefined&&(!Number.isSafeInteger(startup)||startup<=0||startup>2147483))throw Error('invalid_harness_startup_timeout');
  const client=this.factory();
  this.pendingClient=client;
  let closed=false;
  client.onclose=()=>{closed=true;if(this.client===client)this.client=undefined;if(this.pendingClient===client)this.pendingClient=undefined;};
  try{
   await client.connect(new StdioClientTransport({command:config.command,args:config.args,cwd:config.cwd,env:env as any,stderr:'pipe'}),startup===undefined?undefined:{timeout:startup*1000});
   const listing=await client.listTools();const properties=listing.tools.find((t:any)=>t.name==='terminal_exec')?.inputSchema?.properties;
   if(!properties?.worker_task||!properties?.command_id)throw Error('CLIENT_UPDATE_REQUIRED');
   const maintenanceSupported=!!properties?.finish_maintenance;
   const workerRecoverySupported=!!properties?.recover_worker;
   const workerPoolSupported=!!properties?.worker_pool_fingerprint;
   if(this.expectedReleaseId){
    const probe=await client.callTool({name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:1}});
    const identity=probe.structuredContent??JSON.parse(probe.content.find((c:any)=>c.type==='text').text);
    if(identity.server?.release_id!==this.expectedReleaseId)throw Error('candidate_runtime_identity_mismatch');
   }
   if(closed||this.pendingClient!==client)throw Error('harness_reconnect_required');
   this.maintenanceSupported=maintenanceSupported;
   this.workerRecoverySupported=workerRecoverySupported;
   this.workerPoolSupported=workerPoolSupported;
   this.client=client;
  }catch(error){await client.close();throw error;}
  finally{if(this.pendingClient===client)this.pendingClient=undefined;}
 }
 enableWorkerPool(fingerprint:string){
  if(!this.connected||!this.workerPoolSupported)throw Error('worker_pool_client_update_required');
  if(!/^[0-9a-f]{64}$/.test(fingerprint))throw Error('invalid_worker_pool_fingerprint');
  this.workerPoolFingerprint=fingerprint;
 }
 private async call(args:any,allowUnresolved=false){
  this.lastFailure=undefined;
  if(!this.allowedTasks.has(args.task_id))throw Error('task_not_authorized_for_driver');
  if(!this.client)throw Error('harness_reconnect_required');
  const result=await this.client.callTool({name:'terminal_exec',arguments:args},undefined,{timeout:60000});
  const data=result.structuredContent??JSON.parse(result.content.find((c:any)=>c.type==='text').text);
  if(result.isError||data.ok===false||data.error){
   this.lastFailure={task_id:args.task_id,work_unit_id:args.work_unit_id,command_id:args.command_id??data.command_id,state:data.state,
    code:typeof data.error==='string'?data.error: data.error?.code??data.error?.type??'unknown',isError:result.isError,ok:data.ok,
    response:data,envelope:result};
   throw new HarnessOperationError(this.lastFailure);
  }
  if(allowUnresolved&&data.state==='submission_uncertain'&&data.lookup_only===true&&data.command_id===null&&data.task_id===args.task_id&&data.work_unit_id===args.work_unit_id)return data;
  if(data.task_id!==args.task_id||!data.command_id||(args.command_id&&data.command_id!==args.command_id)||(args.work_unit_id&&data.work_unit_id!==args.work_unit_id))throw Error('harness_receipt_identity_mismatch');
  return data;
 }
 recover(task:string,command:string){return this.call({task_id:task,command_id:command,wait_ms:1000,max_preview_bytes:2048});}
 recoverWorker(request:any){
  if(!this.workerRecoverySupported)throw Error('CLIENT_UPDATE_REQUIRED: recover_worker');
  if(!request.worker_task||request.command||request.command_id)throw Error('invalid_worker_recovery');
  return this.call({...request,recover_worker:true},true);
 }
 inspectCommand(task:string,command:string){return this.call({task_id:task,command,access_mode:'read_only',timeout_seconds:60,wait_ms:1000,max_preview_bytes:16384});}
 maintenance(request:any){
  if(!this.maintenanceSupported)throw Error('candidate_maintenance_schema_required');
  if(!request.command||request.worker_task||request.command_id||!request.work_unit_id||request.access_mode!=='writer')throw Error('invalid_maintenance_request');
  return this.call({...request,finish_maintenance:true});
 }
 recoverMaintenance(task:string,unit:string,command:string){
  if(!this.maintenanceSupported)throw Error('candidate_maintenance_schema_required');
  return this.call({task_id:task,work_unit_id:unit,command_id:command,finish_maintenance:true,wait_ms:1000,max_preview_bytes:16384});
 }
 submit(request:any){
  if(!request.worker_task||request.command||request.command_id||request.access_mode!=='writer')throw Error('invalid_worker_submission');
  if(this.workerPoolFingerprint){
   if(!this.workerPoolSupported)throw Error('worker_pool_client_update_required');
   return this.call({...request,worker_pool_fingerprint:this.workerPoolFingerprint});
  }
  return this.call(request);
 }
 async close(){const clients=new Set([this.client,this.pendingClient]);this.client=undefined;this.pendingClient=undefined;for(const client of clients)await client?.close();}
}

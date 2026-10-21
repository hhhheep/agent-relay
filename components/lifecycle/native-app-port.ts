import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {executorCaller} from './publisher-route.ts';
import {realpathSync} from 'node:fs';

// Bootstrap only from a genuine App executor context. No pipe discovery or
// impersonation. A restarted App must provide a new context before reconnecting.
export class NativeAppPort {
 private client:any;private caller?:string;
 private context?:{serverPath:string,env:Record<string,string|undefined>};
 private connecting?:Promise<{threadId:string}>;
 private blocked=false;private generation=0;
 private lifetime=new AbortController();
 constructor(readonly factory=()=>new Client({name:'harness-help-router',version:'1'},{capabilities:{}})){}
 async connect(serverPath:string,env:Record<string,string|undefined>,options:{signal?:AbortSignal,timeoutMs?:number}={}){
  if(this.client||this.connecting)throw Error('app_port_already_connected');
  executorCaller(env);
  if(this.lifetime.signal.aborted)this.lifetime=new AbortController();
  return this.start(serverPath,{...env},options);
 }
 canReconnect(){return !!this.context&&!this.blocked;}
 diagnostics(){return {generation:this.generation,reconnectAllowed:this.canReconnect()};}
 reconnect(options:{signal?:AbortSignal,timeoutMs?:number}={}){
  if(this.client)return Promise.resolve(this.identity());
  if(this.connecting)return this.connecting;
  if(!this.canReconnect())return Promise.reject(Error('new_trusted_app_context_required'));
  return this.start(this.context!.serverPath,this.context!.env,options);
 }
 private start(serverPath:string,env:Record<string,string|undefined>,options:{signal?:AbortSignal,timeoutMs?:number}={}){
  const operation=this.bootstrap(serverPath,env,options);this.connecting=operation;
  return operation.finally(()=>{if(this.connecting===operation)this.connecting=undefined;});
 }
 private async bootstrap(serverPath:string,env:Record<string,string|undefined>,options:{signal?:AbortSignal,timeoutMs?:number}){
  const caller=executorCaller(env).threadId;
  const timeout=options.timeoutMs??5000;
  if(!Number.isInteger(timeout)||timeout<1||timeout>30000)throw Error('invalid_app_connect_timeout');
  const signal=AbortSignal.any([this.lifetime.signal,AbortSignal.timeout(timeout),...(options.signal?[options.signal]:[])]);
  const client=this.factory();
  let abort:()=>void=()=>{};
  try{
   const work=async()=>{
    await client.connect(new StdioClientTransport({command:process.execPath,args:[serverPath],env:env as any,stderr:'pipe'}),{signal,timeout});
    const listing=await client.listTools(undefined,{signal,timeout});
    if(!['read_thread','send_message_to_thread'].every(n=>listing.tools.some((t:any)=>t.name===n)))throw Error('native_app_tools_missing');
    const result=await client.callTool({name:'read_thread',arguments:{threadId:caller,turnLimit:1,includeOutputs:false,maxOutputCharsPerItem:100},_meta:{'openai/threadId':caller}},undefined,{signal,timeout});
    const data=this.decode(result);
    if(data.thread?.id!==caller)throw Error('bootstrap_caller_mismatch');
    if(data.thread.hostId&&data.thread.hostId!=='local')throw Error('bootstrap_host_mismatch');
   };
   const cancelled=new Promise<never>((_,reject)=>{abort=()=>reject(Error('app_connect_cancelled_or_timeout'));if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true});});
   await Promise.race([work(),cancelled]);
   if(signal.aborted)throw Error('app_connect_cancelled_or_timeout');
   this.client=client;this.caller=caller;
   this.context={serverPath,env:{...env}};this.blocked=false;this.generation++;
   return {threadId:caller};
  }catch(error){
   if(this.permanent(error))this.blocked=true;
   await client.close();throw error;
  }finally{signal.removeEventListener('abort',abort);}
 }
 private permanent(error:any){return ['native_app_tools_missing','bootstrap_caller_mismatch','bootstrap_host_mismatch','native_app_tool_error','native_app_response_error','native_app_destination_mismatch'].includes(error?.message);}
 private decode(result:any){
  if(result.isError)throw Error('native_app_tool_error');
  const data=result.structuredContent??JSON.parse(result.content.find((c:any)=>c.type==='text').text);
  if(data.error)throw Error('native_app_response_error');
  return data;
 }
 identity(){if(!this.client||!this.caller)throw Error('app_rebootstrap_required');return {threadId:this.caller};}
 async assertWorkspace(workspace:string){
  const caller=this.identity().threadId,data=await this.read(caller);
  if(data.thread?.id!==caller||data.thread.hostId!=='local'||typeof data.thread.cwd!=='string'||realpathSync.native(data.thread.cwd)!==realpathSync.native(workspace))throw Error('native_app_workspace_mismatch');
  return caller;
 }
 async read(threadId:string,options:{signal?:AbortSignal,timeoutMs?:number}={}){return this.call('read_thread',{threadId,turnLimit:8,includeOutputs:false,maxOutputCharsPerItem:1000},options);}
 async send(threadId:string,prompt:string){
  const result=await this.call('send_message_to_thread',{threadId,hostId:'local',prompt});
  if(result.threadId!==threadId){this.blocked=true;await this.drop();throw Error('native_app_destination_mismatch');}
  return result;
 }
 private async call(name:string,args:any,options:{signal?:AbortSignal,timeoutMs?:number}={}){
  if(!this.client||!this.caller)throw Error('app_rebootstrap_required');
  const client=this.client,caller=this.caller;
  try{
   const data=this.decode(await client.callTool({name,arguments:args,_meta:{'openai/threadId':caller}},undefined,{timeout:options.timeoutMs??10000,signal:options.signal}));
   if(client!==this.client)throw Error('stale_app_connection');
   return data;
  }catch(error){if(client===this.client){if(this.permanent(error))this.blocked=true;await this.drop();}throw error;}
 }
 private async drop(){const client=this.client;this.client=undefined;this.caller=undefined;if(client)await client.close();}
 async close(){this.lifetime.abort();this.context=undefined;this.blocked=true;await this.drop();}
}

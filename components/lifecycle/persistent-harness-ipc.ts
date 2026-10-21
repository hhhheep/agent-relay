import {createServer,request as httpRequest} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {PersistentHarnessOwner,type PersistentConfig} from './persistent-harness.ts';
import type {PersistentReaderPort} from './persistent-harness-reader.ts';
import {McpError} from '@modelcontextprotocol/sdk/types.js';
import type {RuntimePins} from './persistent-harness-pins.ts';
import type {PersistentReconnect} from './persistent-reconnect.ts';

export type PersistentIpc={host:'127.0.0.1',port:number,clientToken:string,adminToken?:string,configurationSha256?:string,runtimeSha256?:string};
export type PersistentHostConfig={schema:'persistent-harness-host/v1',ipc:PersistentIpc,owner:PersistentConfig,installedBackendConfig:string,guardianPython?:string,connectionCooldownSeconds?:number,reconnectStatePath?:string,environment?:Record<string,string|null>,runtimePins?:RuntimePins};
export function loadPersistentHost(path:string):PersistentHostConfig{
 const c=JSON.parse(readFileSync(path,'utf8'));
 if(c.schema!=='persistent-harness-host/v1'||!c.owner||!c.installedBackendConfig)throw Error('invalid_persistent_host_config');
 validateIpc(c.ipc);if(!/^[a-f0-9]{64}$/.test(c.ipc.adminToken)||c.ipc.adminToken===c.ipc.clientToken)throw Error('invalid_persistent_admin_token');return c;
}
export function validateIpc(c:PersistentIpc){if(c?.host!=='127.0.0.1'||!Number.isInteger(c.port)||c.port<1024||c.port>65535||!/^[a-f0-9]{64}$/.test(c.clientToken))throw Error('invalid_persistent_ipc');}
const equal=(a:unknown,b:string)=>{const x=Buffer.from(String(a??'')),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);};
export async function persistentRpc(c:PersistentIpc,path:string,data?:any,admin=false){
 validateIpc(c);const response=await fetch(`http://${c.host}:${c.port}${path}`,{method:data===undefined?'GET':'POST',headers:{authorization:`Bearer ${admin?c.adminToken:c.clientToken}`,'content-type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),signal:AbortSignal.timeout(['/call','/reconnect'].includes(path)?65000:15000)});
 const value=await response.json();if(!response.ok){if(value.rpcError)throw new McpError(value.rpcError.code,value.rpcError.message,value.rpcError.data);throw Error(value.error??'persistent_ipc_error');}return value;
}

// All candidate instances must bind the OS-exclusive endpoint before invoking startBackend.
export async function servePersistentHarness(owner:PersistentHarnessOwner,c:PersistentIpc,startBackend:()=>Promise<any>,reconnect?:{policy:PersistentReconnect,connect:()=>Promise<any>}){
 validateIpc(c);if(!/^[a-f0-9]{64}$/.test(c.adminToken??''))throw Error('persistent_admin_token_required');
 const sessions=new Map<string,{lease?:any,timer?:NodeJS.Timeout}>();let stopping=false;
 async function detach(id:string){const session=sessions.get(id);if(!session)return;sessions.delete(id);clearTimeout(session.timer);session.lease?.end();await owner.detach(id);}
 const server=createServer(async(req,res)=>{
  const reply=(status:number,value:any)=>{if(!res.destroyed){res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(value));}};
  try{
   // A hostile web origin must not use a local bearer authenticated endpoint.
   if(req.headers.origin)return reply(403,{error:'persistent_origin_rejected'});
   const admin=equal(req.headers.authorization,`Bearer ${c.adminToken}`),client=equal(req.headers.authorization,`Bearer ${c.clientToken}`);
   if(!admin&&!client)return reply(401,{error:'persistent_unauthorized'});
   if(req.method==='GET'&&req.url==='/status')return reply(200,{...owner.status(),reconnect:reconnect?.policy.status()??null});
   if(req.method==='GET'&&req.url==='/catalog')return reply(200,owner.catalog());
   if(req.method==='GET'&&req.url==='/lease'){
    const id=req.headers['x-harness-session'];const session=typeof id==='string'?sessions.get(id):undefined;
    if(typeof id!=='string'||!session||session.lease)return reply(403,{error:'persistent_attachment_stale'});
    session.lease=res;clearTimeout(session.timer);session.timer=undefined;res.writeHead(200,{'content-type':'application/octet-stream'});res.flushHeaders();
    res.once('close',()=>{void detach(id);});return;
   }
   if(req.method!=='POST')return reply(405,{error:'persistent_method_rejected'});
   let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>1048576)return reply(413,{error:'persistent_request_too_large'});}
   const body=JSON.parse(raw||'{}');
   if(req.url==='/stop'){
    if(!admin)return reply(403,{error:'persistent_admin_required'});
    const state=owner.status(),expected=body.expected;
    if(expected?.generation!==state.generation||expected.configurationSha256!==state.configurationSha256||expected.runtimeSha256!==state.runtimeSha256)return reply(409,{error:'persistent_stop_conflict'});
    await owner.stopManual();stopping=true;reply(200,{stopped:true});setImmediate(()=>{for(const id of sessions.keys())void detach(id);server.close();server.closeAllConnections();});return;
   }
   if(stopping)return reply(503,{error:'persistent_service_stopping'});
   if(req.url==='/reconnect'||req.url==='/reconnect-policy'){
    if(!admin)return reply(403,{error:'persistent_admin_required'});
    const s=owner.status(),e=body.expected;
    if(e?.generation!==s.generation||e.configurationSha256!==s.configurationSha256||e.runtimeSha256!==s.runtimeSha256)return reply(409,{error:'persistent_reconnect_conflict'});
    if(!reconnect)return reply(409,{error:'persistent_reconnect_not_configured'});
    if(req.url==='/reconnect-policy'){
     if(Object.keys(body).some(k=>!['expected','enabled'].includes(k)))throw Error('persistent_reconnect_request_rejected');
     return reply(200,reconnect.policy.setEnabled(body.enabled));
    }
    if(Object.keys(body).some(k=>k!=='expected'))throw Error('persistent_reconnect_request_rejected');
    return reply(200,await reconnect.policy.attempt('manual',()=>owner.assertReconnectReady(),reconnect.connect));
   }
   if(req.url==='/rebind'){
    if(!admin)return reply(403,{error:'persistent_admin_required'});
    const context={...process.env,CODEX_THREAD_ID:body.context?.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:body.context?.CODEX_APP_TOOLS_PIPE_PATH};
    return reply(200,await owner.rebind(context,body.expected));
   }
   if(req.url==='/attach'){
    // Actual executor or Desktop MCP-call context only; never saved, echoed or used as shell env.
    const context={...process.env,CODEX_THREAD_ID:body.context?.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:body.context?.CODEX_APP_TOOLS_PIPE_PATH};
    const a=await owner.attach(context);
    if(stopping){await owner.detach(a.id);return reply(503,{error:'persistent_service_stopping'});}
    const session:{lease?:any,timer?:NodeJS.Timeout}={};sessions.set(a.id,session);
    session.timer=setTimeout(()=>{if(!session.lease)void detach(a.id);},5000);session.timer.unref();return reply(200,a);
   }
   const session=typeof body.id==='string'?sessions.get(body.id):undefined;
   if(!session)return reply(403,{error:'persistent_attachment_stale'});
   if(req.url==='/detach'){await detach(body.id);return reply(200,{detached:true});}
   if(!session.lease)return reply(403,{error:'persistent_lease_required'});
   if(req.url==='/list')return reply(200,await owner.listTools(body.id));
   if(req.url==='/call')return reply(200,await owner.callTool(body.id,body.request));
   return reply(404,{error:'persistent_route_rejected'});
  }catch(error:any){
   if(error instanceof McpError)reply(409,{rpcError:{code:error.code,message:error.message.replace(/^MCP error -?\d+: /,''),data:error.data}});
   else reply(409,{error:typeof error?.message==='string'?error.message.slice(0,180):'persistent_request_failed'});
  }
 });
 await new Promise<void>((yes,no)=>{server.once('error',no);server.listen({host:c.host,port:c.port,exclusive:true},()=>{server.off('error',no);yes();});});
 try{await startBackend();}catch(error){await owner.stopManual();server.close();server.closeAllConnections();throw error;}
 return {server,stop:async()=>{await owner.stopManual();for(const id of [...sessions.keys()])await detach(id);server.close();server.closeAllConnections();}};
}

// The plugin connects to local IPC only. It cannot start, stop or replace the owner.
export function persistentRemotePort(c:PersistentIpc):PersistentReaderPort&{catalog:()=>Promise<any>,close:()=>Promise<void>}{
 validateIpc(c);if(![c.configurationSha256,c.runtimeSha256].every(v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)))throw Error('persistent_client_pins_required');let lease:any,current:string|undefined;
 const check=async()=>{const state=await persistentRpc(c,'/status');if(state.configurationSha256!==c.configurationSha256||state.runtimeSha256!==c.runtimeSha256)throw Error('persistent_existing_configuration_mismatch');};
 const detach=async(id:string)=>{if(current!==id)return;current=undefined;lease?.destroy();lease=undefined;try{await persistentRpc(c,'/detach',{id});}catch{}};
 return {
  async catalog(){
   await check();
   return persistentRpc(c,'/catalog');
  },
  async attach(env){
   await check();
   const a=await persistentRpc(c,'/attach',{context:{CODEX_THREAD_ID:env.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:env.CODEX_APP_TOOLS_PIPE_PATH}});current=a.id;
   try{await new Promise<void>((yes,no)=>{
    lease=httpRequest({host:c.host,port:c.port,path:'/lease',headers:{authorization:`Bearer ${c.clientToken}`,'x-harness-session':a.id}},res=>{
     if(res.statusCode!==200){res.resume();no(Error('persistent_lease_rejected'));return;}res.on('error',()=>{});res.resume();yes();
    });lease.once('error',no);lease.end();
   });}catch(error){await detach(a.id);throw error;}return a;
  },
  listTools:id=>persistentRpc(c,'/list',{id}),callTool:(id,request)=>persistentRpc(c,'/call',{id,request}),detach,
  close:async()=>{if(current)await detach(current);}
 };
}

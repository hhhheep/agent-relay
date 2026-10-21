import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {ListToolsRequestSchema,CallToolRequestSchema} from '@modelcontextprotocol/sdk/types.js';
export type PersistentReaderPort={attach:(env:NodeJS.ProcessEnv)=>Promise<{id:string,generation:string}>,listTools:(id:string)=>Promise<any>,callTool:(id:string,request:any)=>Promise<any>,detach:(id:string)=>Promise<void>};

// An MCP consumer of the manually started owner. No process spawning or implicit backend start.
// The IPC adapter supplies the actual executor env in memory, never a claimed chat ID from tool args.
export async function createPersistentReader(owner:PersistentReaderPort,env:NodeJS.ProcessEnv){
 const attachment=await owner.attach(env);
 const server=new Server({name:'solution-agent-harness',version:'0.6.0'},{capabilities:{tools:{}}});
 server.setRequestHandler(ListToolsRequestSchema,()=>owner.listTools(attachment.id));
 server.setRequestHandler(CallToolRequestSchema,request=>owner.callTool(attachment.id,request.params));
 server.onclose=()=>{void owner.detach(attachment.id);};
 return {server,attachment,close:async()=>{await owner.detach(attachment.id);await server.close();}};
}

// MCP hosts supply thread identity per call, not at plugin process startup.
// Read metadata only from the request envelope; arguments never supply identity.
export function desktopCallerContext(env:NodeJS.ProcessEnv,meta:any):NodeJS.ProcessEnv{
 if(!env.CODEX_APP_TOOLS_PIPE_PATH)throw Error('missing_trusted_app_context');
 const ids:string[]=[];
 const add=(v:any)=>{if(v===undefined)return;if(typeof v!=='string'||!v.trim()||v.length>128)throw Error('invalid_desktop_thread_metadata');ids.push(v);};
 if(!meta||typeof meta!=='object'||Array.isArray(meta))throw Error('missing_desktop_thread_metadata');
 for(const key of ['openai/threadId','openai/thread_id','codexThreadId','codex_thread_id','threadId','thread_id'])add(meta[key]);
 if(meta.thread!==undefined){if(!meta.thread||typeof meta.thread!=='object')throw Error('invalid_desktop_thread_metadata');add(meta.thread.id);}
 if(meta['x-codex-turn-metadata']!==undefined){
  let turn=meta['x-codex-turn-metadata'];
  if(typeof turn==='string'){if(turn.length>8192)throw Error('invalid_desktop_thread_metadata');try{turn=JSON.parse(turn);}catch{throw Error('invalid_desktop_thread_metadata');}}
  if(!turn||typeof turn!=='object'||Array.isArray(turn))throw Error('invalid_desktop_thread_metadata');add(turn.thread_id);
 }
 if(!ids.length)throw Error('missing_desktop_thread_metadata');
 if(new Set(ids).size!==1||(env.CODEX_THREAD_ID&&env.CODEX_THREAD_ID!==ids[0]))throw Error('ambiguous_desktop_thread_metadata');
 return {...env,CODEX_THREAD_ID:ids[0]};
}

export async function createDesktopPersistentReader(owner:PersistentReaderPort&{catalog:()=>Promise<any>},env:NodeJS.ProcessEnv){
 if(!env.CODEX_APP_TOOLS_PIPE_PATH)throw Error('missing_trusted_app_context');
 const server=new Server({name:'solution-agent-harness',version:'0.6.0'},{capabilities:{tools:{}}});
 let busy=false,closing=false;
 server.setRequestHandler(ListToolsRequestSchema,()=>owner.catalog());
 server.setRequestHandler(CallToolRequestSchema,async request=>{
  if(closing)throw Error('persistent_reader_closed');
  if(busy)throw Error('persistent_request_pending');
  const context=desktopCallerContext(env,request.params._meta);
  busy=true;let attachment:{id:string}|undefined;
  try{
   attachment=await owner.attach(context);
   if(closing)throw Error('persistent_reader_closed');
   return await owner.callTool(attachment.id,{name:request.params.name,arguments:request.params.arguments??{}});
  }finally{try{if(attachment)await owner.detach(attachment.id);}finally{busy=false;}}
 });
 const close=async()=>{closing=true;await server.close();};
 server.onclose=()=>{closing=true;};
 return {server,close};
}

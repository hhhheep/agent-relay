// Test-only genuine OS process boundary around the existing manager driver.
// App/MCP transports are synthetic; no Desktop capability or credential is used.
import {readFileSync} from 'node:fs';
import {runManager} from '../../components/lifecycle/manager-driver.ts';
import {HarnessPort} from '../../components/lifecycle/harness-port.ts';
import {NativeAppPort} from '../../components/lifecycle/native-app-port.ts';

const [path,caller,url]=process.argv.slice(2),config=JSON.parse(readFileSync(path,'utf8'));
const dependencies={
 connectInbox:async()=>({get:async()=>({messages:[],last_event_id:0}),send:async()=>({ok:true}),close:async()=>{},agentId:config.name}) as any,
 app:()=>new NativeAppPort((()=>({connect:async()=>{},close:async()=>{},
  listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),
  callTool:async(request:any)=>({structuredContent:{thread:{id:request.arguments.threadId,hostId:'local'}}})})) as any),
 harness:(tasks:Set<string>,release?:string)=>new HarnessPort(tasks,(()=>({
  connect:async()=>{},close:async()=>{},
  listTools:async()=>({tools:[{name:'terminal_exec',inputSchema:{properties:{worker_task:{},command_id:{},recover_worker:{},worker_pool_fingerprint:{}}}}]}),
  callTool:async(request:any)=>{
   if(request.name==='terminal_read')return {structuredContent:{server:{release_id:'fixture-release'}}};
   const response=await fetch(url,{method:'POST',body:JSON.stringify(request.arguments),signal:AbortSignal.timeout(20000)});
   if(!response.ok)throw Error('synthetic_transport_unavailable');
   return {structuredContent:await response.json()};
  }
 })) as any,release)
};
let status:any;
await runManager(config,{HARNESS_XATS_TOKEN:'synthetic-test-only',CODEX_THREAD_ID:caller,CODEX_APP_TOOLS_PIPE_PATH:'synthetic-test-only'},
 new AbortController().signal,{once:true,onStatus:value=>{status=value;}},dependencies);
console.log(JSON.stringify({pid:process.pid,state:status.state,resumeState:status.resumeState,recovery:status.publicationRecovery,dispatch:status.publicationDispatch}));

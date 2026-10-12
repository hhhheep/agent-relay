import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
export async function connectInbox(url:string,token:string,name:string,team:string){
  const client=new Client({name,version:'1'},{capabilities:{}});
  await client.connect(new StreamableHTTPClientTransport(new URL(url),{requestInit:{headers:{Authorization:`Bearer ${token}`}}}));
  const call=async(name:string,args:any)=>{const r=await client.callTool({name,arguments:args});const x=JSON.parse((r.content as any)[0].text);if(r.isError||x.error)throw Error(JSON.stringify(x));return x;};
  const registration=await call('register_agent',{agent_type:'custom',name,team});
  return {client,registration,call,get:(cursor:number)=>call('get_inbox',{since_event_id:cursor,limit:100}),send:(to:string,body:string)=>call('send_message_by_id',{to_agent_id:to,body,auto_poke:false,need_reply:false,await_ack_s:0}),close:()=>client.close()};
}

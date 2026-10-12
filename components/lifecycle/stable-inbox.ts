import {DatabaseSync} from 'node:sqlite';
import {randomBytes} from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';

// This is a custom deterministic bridge identity, not a Codex pane identity.
// Its private key is minted here, persisted before registration, never logged.
export async function connectStableInbox(path:string,options:{url:string,token:string,name:string,team:string,endpointIdentity?:string}){
 const db=new DatabaseSync(path);let client:Client|undefined;
 try{
  db.exec(`PRAGMA busy_timeout=5000;PRAGMA synchronous=FULL;
   CREATE TABLE IF NOT EXISTS identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding TEXT NOT NULL,secret TEXT NOT NULL,agent_id TEXT);`);
  const binding=JSON.stringify({url:options.endpointIdentity??options.url,name:options.name,team:options.team});
  db.prepare('INSERT OR IGNORE INTO identity(singleton,binding,secret) VALUES (1,?,?)').run(binding,randomBytes(32).toString('hex'));
  const row=db.prepare('SELECT * FROM identity WHERE singleton=1').get() as any;
  const old=JSON.parse(row.binding);
  // A tunnel address may move. Only opt in for a declared service identity;
  // migrate after the remote server proves it recognizes the original secret/ID.
  const migrate=row.binding!==binding&&!!options.endpointIdentity&&!!row.agent_id&&old.name===options.name&&old.team===options.team;
  if(row.binding!==binding&&!migrate)throw Error('inbox_identity_binding_changed');
  client=new Client({name:'harness-routing-bridge',version:'1'},{capabilities:{}});
  await client.connect(new StreamableHTTPClientTransport(new URL(options.url),{requestInit:{headers:{Authorization:`Bearer ${options.token}`}}}));
  const call=async(name:string,args:any)=>{
   const result=await client!.callTool({name,arguments:args});
   const text=(result.content as any[]).find(c=>c.type==='text')?.text;
   const data=JSON.parse(text);
   // Avoid putting opaque identity keys or headers from tool errors into logs.
   if(result.isError||data.error)throw Error('xats_call_failed:'+name+':'+String(data.error??'tool_error'));
   return data;
  };
  const registration=row.agent_id
   ?await call('reconnect',{identity_key:row.secret})
   :await call('register_agent',{agent_type:'custom',name:options.name,team:options.team,identity_key:row.secret});
  const id=registration.agent_id;
  if(typeof id!=='string'||!id||(row.agent_id&&id!==row.agent_id))throw Error('inbox_identity_not_recovered');
  if(migrate)db.prepare('UPDATE identity SET binding=? WHERE singleton=1 AND agent_id=?').run(binding,id);
  db.prepare('UPDATE identity SET agent_id=? WHERE singleton=1').run(id);
  db.close();
  return {agentId:id,call,get:(cursor:number)=>call('get_inbox',{since_event_id:cursor,limit:100}),
   send:(to:string,body:string)=>call('send_message_by_id',{to_agent_id:to,body,auto_poke:false,need_reply:false,await_ack_s:0}),close:()=>client!.close()};
 }catch(error){await client?.close();db.close();throw error;}
}

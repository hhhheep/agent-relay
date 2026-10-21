import {createServer} from 'node:http';
import {randomBytes,randomUUID,timingSafeEqual} from 'node:crypto';
import {realpathSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {NativeAppPort} from './native-app-port.ts';
import {controllerScope,currentController,transferController} from './controller-authority.ts';
import {PersistentHarnessOwner} from './persistent-harness.ts';
import {persistentRpc,type PersistentHostConfig} from './persistent-harness-ipc.ts';
import {registerPersistentScope} from './persistent-harness-registration.ts';

// Operator UI, deliberately outside MCP. Its receiver is the genuine launching
// chat, never a chat/task/epoch supplied in an HTTP request or a model argument.
export class PersistentOperatorPanel{
 private app:any;private caller='';private cwd='';private queue='';private busy=false;
 private choices=new Map<string,{task:string,unit:string,chat:string,epoch:number,transferId:string}>();
 constructor(private host:PersistentHostConfig,private runtime:string,private env:NodeJS.ProcessEnv,private deps:{app?:()=>any,rpc?:(path:string,body?:any)=>Promise<any>}={}){}
 private rpc(path:string,body?:any){return this.deps.rpc?.(path,body)??persistentRpc(this.host.ipc,path,body,true);}
 private async status(){const s=await this.rpc('/status');if(s.configurationSha256!==new PersistentHarnessOwner(this.host.owner).status().configurationSha256||s.runtimeSha256!==this.runtime)throw Error('panel_owner_mismatch_or_offline');return s;}
 async connection(){await this.verify();const s=await this.status();return {state:s.state,pending:s.pending,connection:s.connection,reconnect:s.reconnect};}
 async connectionAction(input:any){
  if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).some(k=>!['action','enabled'].includes(k))||!['reconnect','auto-reconnect'].includes(input.action)||(input.action==='reconnect'&&Object.keys(input).length!==1)||(input.action==='auto-reconnect'&&typeof input.enabled!=='boolean'))throw Error('panel_request_rejected');
  if(this.busy)throw Error('panel_busy');this.busy=true;
  try{await this.verify();const s=await this.status(),expected={generation:s.generation,configurationSha256:s.configurationSha256,runtimeSha256:s.runtimeSha256};
   return await this.rpc(input.action==='reconnect'?'/reconnect':'/reconnect-policy',{expected,...input.action==='auto-reconnect'?{enabled:input.enabled}:{}});
  }finally{this.busy=false;}
 }
 private async verify(){const d=await this.app.read(this.caller);if(d.thread?.id!==this.caller||d.thread.hostId!=='local'||realpathSync.native(d.thread.cwd)!==this.cwd)throw Error('panel_actual_chat_changed');return d.thread;}
 async open(){
  this.app=this.deps.app?.()??new NativeAppPort();
  try{
   await this.app.connect(this.host.owner.appServerPath,this.env);this.caller=this.app.identity().threadId;
   const d=await this.app.read(this.caller);if(d.thread?.id!==this.caller||d.thread.hostId!=='local'||typeof d.thread.cwd!=='string')throw Error('panel_actual_chat_required');
   this.cwd=realpathSync.native(d.thread.cwd);
   const stores=[this.host.owner,...this.host.owner.authorityStores??[]].filter(s=>realpathSync.native(s.workspace)===this.cwd);
   const queues=[...new Set(stores.map(s=>realpathSync.native(s.queuePath)))];if(queues.length!==1)throw Error('panel_declared_workspace_required');this.queue=queues[0];await this.status();
   return this;
  }catch(error){await this.close();throw error;}
 }
 async catalog(){
  if(this.busy)throw Error('panel_busy');const thread=await this.verify(),state=await this.status();
  const db=new DatabaseSync(this.queue,{readOnly:true});
  try{
   const rows=db.prepare('SELECT scope,source,binding FROM publisher_routes ORDER BY scope LIMIT 1025').all() as any[];
   if(rows.length>1024)throw Error('panel_catalog_too_large');this.choices.clear();
   const work=rows.map((r,index)=>{const b=JSON.parse(r.binding);if(r.scope!==controllerScope(b.task_id,b.work_unit_id))throw Error('panel_catalog_invalid');const c=currentController(db,r.scope),selection=randomUUID();
    this.choices.set(selection,{task:b.task_id,unit:b.work_unit_id,chat:c.chat,epoch:c.epoch,transferId:randomUUID()});
    return {selection,label:`${index+1}. ${r.source} — ${b.task_id} / ${b.work_unit_id}`,owned:c.chat===this.caller};
   });
   return {receiver:thread.title||'目前開啟控制面板的 chat',work,pending:state.pending,online:state.state==='online'};
  }finally{db.close();}
 }
 async action(input:any){
  if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).some(k=>!['action','selection','name'].includes(k)))throw Error('panel_request_rejected');
  if(this.busy)throw Error('panel_busy');this.busy=true;
  try{
   await this.verify();const state=await this.status();if(state.state!=='online')throw Error('panel_owner_mismatch_or_offline');if(state.pending)throw Error('panel_wait_for_pending_calls');
   if(input.action==='register'){
    if(Object.keys(input).some(k=>!['action','name'].includes(k))||typeof input.name!=='string'||!input.name.trim()||input.name.length>100)throw Error('panel_work_name_required');
    // Human-entered text is a label only. All binding identifiers are generated.
    const result=await registerPersistentScope(this.host.owner,this.env,{taskId:'work-'+randomUUID(),workUnitId:'unit-'+randomUUID(),source:'operator: '+input.name.trim()},{app:this.deps.app});
    return {ok:true,message:'新工作已登記到目前 chat；未啟動或派送工作。',registered:result.dispatched===false};
   }
   if(input.action!=='takeover'||Object.keys(input).some(k=>!['action','selection'].includes(k)))throw Error('panel_request_rejected');
   const selected=this.choices.get(input.selection);if(!selected)throw Error('panel_refresh_selection_required');
   const scope=controllerScope(selected.task,selected.unit);let committed=false;
   const db=new DatabaseSync(this.queue);db.exec('PRAGMA busy_timeout=5000;PRAGMA synchronous=FULL;');
   try{
    const current=currentController(db,scope);
    if(current.chat!==this.caller){
     if(current.chat!==selected.chat||current.epoch!==selected.epoch)throw Error('panel_work_changed_refresh_required');
     transferController(db,{transferId:selected.transferId,taskId:selected.task,workUnitId:selected.unit,fromChat:selected.chat,toChat:this.caller,expectedEpoch:selected.epoch,authorizationRef:'local-operator-button:'+selected.transferId},this.caller,()=>true);
    }
    committed=true;
   }finally{db.close();}
   try{
    const latest=await this.status(),binding=latest.activeScopes.find((b:any)=>controllerScope(b.taskId,b.workUnitId)===scope);
    if(binding)await this.rpc('/rebind',{expected:{generation:latest.generation,epoch:binding.epoch,scope},context:{CODEX_THREAD_ID:this.env.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:this.env.CODEX_APP_TOOLS_PIPE_PATH}});
    else{const a=await this.rpc('/attach',{context:{CODEX_THREAD_ID:this.env.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:this.env.CODEX_APP_TOOLS_PIPE_PATH}});await this.rpc('/detach',{id:a.id});}
    return {ok:true,message:'此工作已由目前 chat 接手；原始工作與資料 ID 保留，未重新派工。'};
   }catch(error){if(committed)return {ok:false,committed:true,message:'授權已提交，工具掛接尚未完成。請按重新整理，再按「連接此工作」；不要重新派工。'};throw error;}
  }finally{this.busy=false;}
 }
 async close(){await this.app?.close();}
}

const html=`<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Harness 工作交接</title><style>body{font:17px system-ui;max-width:850px;margin:40px auto;padding:20px;background:#f5f7fa;color:#192d42}section{background:white;padding:22px;margin:18px 0;border-radius:14px}button,input,select{font:inherit;padding:10px;margin:6px 0;max-width:100%}button{background:#164f82;color:white;border:0;border-radius:7px;cursor:pointer}select{display:block;width:100%}#result{white-space:pre-wrap}small{color:#526578}</style><h1>Harness 控制面板</h1><section><h2>連線</h2><p id="connection" role="status">讀取連線狀態…</p><button id="reconnect">立即重新連線</button><button id="auto">開啟自動重新連線</button><p><small>保留原工作與 ID。失敗後自動停止，請確認網路再手動重連；不會重送工作。</small></p></section><p id="receiver"></p><section><h2>接手既有工作</h2><p>請先在要接手的新 chat 開啟面板。尚未建立新 chat 時，舊 chat 仍保有控制權。</p><select id="work"></select><button id="takeover">將選定工作接到目前 chat</button><p><small>這是整個工作單元的移交。拆分子任務請另建新工作，主工作不會自動移轉。</small></p></section><section><h2>登記獨立工作或子任務</h2><input id="name" maxlength="100" placeholder="工作名稱"><button id="register">登記到目前 chat</button><p><small>只建立本機工作登記，並不派工、複製研究資料或授權存取其他工作的證據。</small></p></section><button id="refresh">重新整理</button><p id="result" role="status"></p><script>
const key=location.hash.slice(1);history.replaceState(null,'',location.pathname);let catalog,connection,busy=false;
const el=id=>document.getElementById(id);const buttons=[...document.querySelectorAll('button')];
async function request(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{authorization:'Bearer '+key,'content-type':'application/json'},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error);return d;}
async function refresh(){catalog=await request('/catalog');el('receiver').textContent='接手對象：'+catalog.receiver+'；進行中呼叫：'+catalog.pending;el('work').replaceChildren();for(const w of catalog.work){const o=document.createElement('option');o.value=w.selection;o.textContent=(w.owned?'〔本 chat〕':'〔可移交〕')+w.label;el('work').append(o);}label();}
function label(){const w=catalog?.work.find(w=>w.selection===el('work').value);el('takeover').textContent=w?.owned?'連接此工作':'將選定工作接到目前 chat';el('takeover').disabled=busy||!w||!catalog?.online;el('register').disabled=busy||!catalog?.online;}
async function run(f){busy=true;buttons.forEach(b=>b.disabled=true);try{await f();}catch(e){el('result').textContent='未完成：'+e.message;}finally{busy=false;buttons.forEach(b=>b.disabled=false);label();await connectionRefresh().catch(()=>{});}}
el('work').onchange=label;el('refresh').onclick=()=>run(refresh);
el('takeover').onclick=()=>run(async()=>{const w=catalog.work.find(w=>w.selection===el('work').value);if(!w||!confirm('確認將「'+w.label+'」接到目前 chat「'+catalog.receiver+'」？原始資料與工作 ID 保留，不重新派工。'))return;const r=await request('/action',{action:'takeover',selection:w.selection});el('result').textContent=r.message;await refresh();});
el('register').onclick=()=>run(async()=>{const name=el('name').value.trim();if(!name||!confirm('將「'+name+'」登記到目前 chat？這不會派工或移交主工作。'))return;const r=await request('/action',{action:'register',name});el('result').textContent=r.message;el('name').value='';await refresh();});run(refresh);
async function connectionRefresh(){connection=await request('/connection');const r=connection.reconnect;el('connection').textContent='狀態：'+({online:'已連線',offline:'已斷線',starting:'連線中',closed:'已關閉'}[connection.state]||connection.state)+'；進行中呼叫：'+connection.pending+'；原因：'+(connection.connection?.lastDisconnectReason||'無')+(r?'；冷卻：'+r.cooldownSeconds+' 秒；'+(r.latched?'自動重連已暫停，請手動處理':r.enabled?'自動重連已開啟':'自動重連已關閉'):'；此版本未提供重連');el('reconnect').disabled=busy||connection.state!=='offline'||connection.pending>0||!r||r.busy||r.cooldownSeconds>0;el('auto').disabled=busy||!r||r.busy;el('auto').textContent=r?.enabled?'關閉自動重新連線':'開啟自動重新連線';}
el('reconnect').onclick=()=>run(async()=>{await request('/connection',{action:'reconnect'});el('result').textContent='已重新連線；原工作與 ID 保留。';await refresh();});
el('auto').onclick=()=>run(async()=>{await request('/connection',{action:'auto-reconnect',enabled:!connection.reconnect.enabled});});
connectionRefresh().catch(e=>el('connection').textContent='讀取失敗：'+e.message);setInterval(()=>connectionRefresh().catch(e=>el('connection').textContent='讀取失敗：'+e.message),2000);
</script></html>`;

export async function servePersistentOperatorPanel(panel:PersistentOperatorPanel){
 const token=randomBytes(32).toString('hex');let timer:NodeJS.Timeout;
 const server=createServer(async(req,res)=>{
  const reply=(code:number,value:any)=>{res.writeHead(code,{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff'});res.end(JSON.stringify(value));};
  try{
   if(req.headers.origin&&req.headers.origin!==`http://127.0.0.1:${(server.address() as any).port}`)return reply(403,{error:'panel_origin_rejected'});
   if(req.method==='GET'&&req.url==='/'){res.writeHead(200,{'content-type':'text/html; charset=utf-8','cache-control':'no-store','content-security-policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"});res.end(html);return;}
   const auth=Buffer.from(req.headers.authorization??''),expected=Buffer.from('Bearer '+token);if(auth.length!==expected.length||!timingSafeEqual(auth,expected))return reply(403,{error:'panel_auth_required'});
   if(req.method==='GET'&&req.url==='/catalog')return reply(200,await panel.catalog());
   if(req.method==='GET'&&req.url==='/connection')return reply(200,await panel.connection());
   if(req.method==='POST'&&req.url==='/connection'){let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>4096)return reply(413,{error:'panel_request_too_large'});}return reply(200,await panel.connectionAction(JSON.parse(raw)));}
   if(req.method==='POST'&&req.url==='/action'){let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>4096)return reply(413,{error:'panel_request_too_large'});}return reply(200,await panel.action(JSON.parse(raw)));}
   reply(404,{error:'panel_route_rejected'});
  }catch(error:any){reply(409,{error:error?.message??'panel_failed'});}
 });
 await new Promise<void>((yes,no)=>{server.once('error',no);server.listen(0,'127.0.0.1',yes);});
 const close=async()=>{clearTimeout(timer);server.close();server.closeAllConnections();await panel.close();};
 timer=setTimeout(()=>{void close();},30*60*1000);timer.unref();
 return {url:`http://127.0.0.1:${(server.address() as any).port}/#${token}`,close};
}

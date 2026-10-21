import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync,mkdirSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';import {DatabaseSync} from 'node:sqlite';
import {ReviewedStore} from './reviewed-store.ts';import {PublisherRoutes} from './publisher-route.ts';import {initializeControllerAuthority,controllerScope,currentController} from './controller-authority.ts';
import {PersistentHarnessOwner} from './persistent-harness.ts';import {PersistentOperatorPanel,servePersistentOperatorPanel} from './persistent-harness-panel.ts';
function fixture(){
 const root=mkdtempSync(join(tmpdir(),'handoff-panel-')),queuePath=join(root,'queue.sqlite'),identity=join(root,'workplace.id');writeFileSync(identity,'solution-agent-workplace-v1:'+'7'.repeat(32)+'\n');
 const store=new ReviewedStore(queuePath);new PublisherRoutes(store).publish('Existing work','TASK','UNIT','fixture-worker',{threadId:'old'});initializeControllerAuthority(store.db);store.close();
 const config:any={workspace:root,queuePath,appServerPath:'/fixture/app.mjs',workplaceIdentityPath:identity,binding:{chat:'old',epoch:0,taskId:'TASK',workUnitId:'UNIT'},terminalIds:['main'],peer:{protocol_version:'0.6',release_id:'fixture',source_root_sha256:'a'.repeat(64),mcp_schema_sha256:'b'.repeat(64)}};
 const host:any={owner:config,ipc:{}},env={CODEX_THREAD_ID:'receiving',CODEX_APP_TOOLS_PIPE_PATH:'fixture-pipe'};
 const context={cwd:root,id:'receiving',hostId:'local',title:'Receiving chat'};const app=()=>({connect:async()=>{},identity:()=>({threadId:'receiving'}),read:async()=>({thread:{...context}}),close:async()=>{}});
 const state:any={state:'online',pending:0,generation:'same-owner',configurationSha256:new PersistentHarnessOwner(config).status().configurationSha256,runtimeSha256:'d'.repeat(64),activeScopes:[config.binding]};
 const calls:any[]=[];let failRebind=false;
 const rpc=async(path:string,body?:any)=>{calls.push({path,body});if(path==='/status')return structuredClone(state);if(path==='/reconnect'||path==='/reconnect-policy')return {ok:true};if(path==='/rebind'){if(failRebind)throw Error('fixture_busy');return {changed:true};}throw Error('unexpected_rpc');};
 const panel=new PersistentOperatorPanel(host,'d'.repeat(64),env,{app,rpc});
 return {root,queuePath,panel,context,state,calls,config,fail:()=>failRebind=true,recover:()=>failRebind=false,cleanup:async()=>{await panel.close();rmSync(root,{recursive:true,force:true});}};
}

test('offline panel keeps recovery buttons usable while blocking ownership mutations',async()=>{
 const f=fixture();try{
  f.state.state='offline';f.state.reconnect={enabled:false,latched:true,cooldownSeconds:0};
  await f.panel.open();const catalog=await f.panel.catalog();assert.equal(catalog.online,false);
  assert.equal((await f.panel.connection()).state,'offline');
  await assert.rejects(f.panel.action({action:'register',name:'no offline writes'}),/offline/);
  await f.panel.connectionAction({action:'reconnect'});await f.panel.connectionAction({action:'auto-reconnect',enabled:true});
  const reconnect=f.calls.find(c=>c.path==='/reconnect');assert.deepEqual(reconnect.body,{expected:{generation:f.state.generation,configurationSha256:f.state.configurationSha256,runtimeSha256:f.state.runtimeSha256}});
  for(const input of [{action:'reconnect',chat:'forged'},{action:'reconnect',enabled:true},{action:'auto-reconnect',enabled:'true'}])await assert.rejects(f.panel.connectionAction(input),/request_rejected/);
 }finally{await f.cleanup();}
});
test('buttons derive exact transfer from catalog and genuine receiver; retry retains original origin/epoch',async()=>{
 const f=fixture();try{
  await f.panel.open();const view=await f.panel.catalog();assert.equal(view.receiver,'Receiving chat');assert.equal(view.work.length,1);
  assert.equal('taskId' in view.work[0],false);assert.equal('epoch' in view.work[0],false);
  const action={action:'takeover',selection:view.work[0].selection};assert.equal((await f.panel.action(action)).ok,true);assert.equal((await f.panel.action(action)).ok,true);
  const db=new DatabaseSync(f.queuePath,{readOnly:true});try{const c=currentController(db,controllerScope('TASK','UNIT'));assert.equal(c.chat,'receiving');assert.equal(c.epoch,1);assert.equal(c.binding.origin_chat_id,'old');assert.equal((db.prepare('SELECT count(*) n FROM controller_transfers').get() as any).n,1);}finally{db.close();}
  assert.ok(f.calls.filter(c=>c.path==='/rebind').every(c=>c.body.context.CODEX_THREAD_ID==='receiving'&&c.body.expected.epoch===0));
 }finally{await f.cleanup();}
});
test('buttons refuse injected IDs, stale choices, changed App cwd and pending work before any transfer',async()=>{
 const f=fixture();try{
  await f.panel.open();const v=await f.panel.catalog(),selection=v.work[0].selection;
  for(const input of [{action:'takeover',selection,taskId:'forged'},{action:'takeover',selection,toChat:'forged'},{action:'takeover',selection,epoch:99},{action:'takeover',selection:'unknown'}])await assert.rejects(f.panel.action(input),/request_rejected|refresh_selection/);
  f.state.pending=1;await assert.rejects(f.panel.action({action:'takeover',selection}),/pending/);f.state.pending=0;
  const other=join(f.root,'other');mkdirSync(other);f.context.cwd=other;await assert.rejects(f.panel.action({action:'takeover',selection}),/chat_changed/);f.context.cwd=f.root;
  const db=new DatabaseSync(f.queuePath);db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(controllerScope('TASK','UNIT'),'third-chat',1);db.close();
  await assert.rejects(f.panel.action({action:'takeover',selection}),/work_changed/);assert.equal(f.calls.some(c=>c.path==='/rebind'),false);
 }finally{await f.cleanup();}
});
test('partial transfer tells user to reconnect; same button never increments epoch twice',async()=>{
 const f=fixture();try{
  await f.panel.open();const v=await f.panel.catalog(),action={action:'takeover',selection:v.work[0].selection};f.fail();const r=await f.panel.action(action);assert.equal(r.committed,true);assert.equal(r.ok,false);
  f.recover();assert.equal((await f.panel.action(action)).ok,true);
  const db=new DatabaseSync(f.queuePath,{readOnly:true});try{assert.equal(currentController(db,controllerScope('TASK','UNIT')).epoch,1);}finally{db.close();}
 }finally{await f.cleanup();}
});
test('new subtask button generates binding identifiers and never transfers parent or dispatches',async()=>{
 const f=fixture();try{
  await f.panel.open();await assert.rejects(f.panel.action({action:'register',name:'Child',taskId:'chosen-by-model'}),/request_rejected/);
  assert.equal((await f.panel.action({action:'register',name:'Child work'})).registered,true);
  const db=new DatabaseSync(f.queuePath,{readOnly:true});try{const rows=db.prepare('SELECT binding FROM publisher_routes').all() as any[];assert.equal(rows.length,2);const child=rows.map(r=>JSON.parse(r.binding)).find(b=>b.origin_chat_id==='receiving');assert.match(child.task_id,/^work-[a-f0-9-]+$/);assert.match(child.work_unit_id,/^unit-[a-f0-9-]+$/);assert.equal(currentController(db,controllerScope('TASK','UNIT')).chat,'old');assert.equal((db.prepare('SELECT count(*) n FROM controller_transfers').get() as any).n,0);}finally{db.close();}
  assert.equal(f.calls.some(c=>c.path!=='/status'),false);
 }finally{await f.cleanup();}
});
test('panel mutation requires bearer and same origin; HTML exposes buttons, no identity input',async()=>{
 const f=fixture();let service:any;try{
  await f.panel.open();service=await servePersistentOperatorPanel(f.panel);const url=new URL(service.url),base=url.origin,token=url.hash.slice(1);
  const page=await (await fetch(base)).text();assert.match(page,/將選定工作接到目前 chat/);assert.doesNotMatch(page,/<input[^>]+(?:taskId|chatId|epoch)/);
  assert.equal((await fetch(base+'/catalog')).status,403);
  assert.equal((await fetch(base+'/action',{method:'POST',headers:{authorization:'Bearer '+token,origin:'https://evil.invalid'},body:'{}'})).status,403);
  const catalog=await (await fetch(base+'/catalog',{headers:{authorization:'Bearer '+token}})).json();assert.equal(catalog.receiver,'Receiving chat');
  assert.equal((await fetch(base+'/action',{method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json'},body:JSON.stringify({action:'takeover',selection:'forged',toChat:'arbitrary'})})).status,409);
 }finally{await service?.close();await f.cleanup();}
});

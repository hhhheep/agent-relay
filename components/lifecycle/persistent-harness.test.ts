import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join,resolve,dirname} from 'node:path';import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';import {DatabaseSync} from 'node:sqlite';import {execFileSync} from 'node:child_process';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {InMemoryTransport} from '@modelcontextprotocol/sdk/inMemory.js';
import {createServer as createNetServer} from 'node:net';
import {randomBytes} from 'node:crypto';
import {McpError,ErrorCode} from '@modelcontextprotocol/sdk/types.js';
import {servePersistentHarness,persistentRemotePort,persistentRpc} from './persistent-harness-ipc.ts';
import {PersistentHarnessOwner,verifyPersistentCaller,type PersistentConfig} from './persistent-harness.ts';
import {PersistentSubmissions,submissionCommandId} from './persistent-submissions.ts';
import {initializeControllerAuthority,transferController,controllerScope} from './controller-authority.ts';
import {sealPersistentHost,verifyPersistentRuntime} from './persistent-harness-pins.ts';
import {registerPersistentScope} from './persistent-harness-registration.ts';
import {ReviewedStore} from './reviewed-store.ts';
import {PersistentReconnect} from './persistent-reconnect.ts';
import {createPersistentReader,createDesktopPersistentReader,desktopCallerContext} from './persistent-harness-reader.ts';import {NativeAppPort} from './native-app-port.ts';
const names=['stage_execute','stage_status','stage_collect','terminal_open','terminal_exec','terminal_send','terminal_read','terminal_close','artifact_inspect','artifact_store','artifact_apply'];
const scope=createHash('sha256').update(JSON.stringify(['task','unit'])).digest('hex');
function fixture(toolMode?:'full'){
 const root=mkdtempSync(join(tmpdir(),'persistent-harness-'));const identity='solution-agent-workplace-v1:'+'1'.repeat(32)+'\n';
 const identityPath=join(root,'workplace.id');writeFileSync(identityPath,identity);
 const path=join(root,'queue.sqlite'),db=new DatabaseSync(path);
 db.exec('CREATE TABLE publisher_routes(scope TEXT PRIMARY KEY,binding TEXT);CREATE TABLE controller_authority(scope TEXT PRIMARY KEY,chat TEXT,epoch INTEGER);CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);');
 db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(scope,JSON.stringify({task_id:'task',work_unit_id:'unit',origin_chat_id:'old-chat'}));
 db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(scope,'caller',1);
 const config:PersistentConfig={workspace:root,queuePath:path,appServerPath:'/declared/app.mjs',workplaceIdentityPath:identityPath,
  binding:{chat:'caller',epoch:1,taskId:'task',workUnitId:'unit'},terminalIds:['main'],peer:{protocol_version:'0.6',release_id:'fixture-release',source_root_sha256:'a'.repeat(64),mcp_schema_sha256:'b'.repeat(64)}};
 const env={CODEX_THREAD_ID:'caller',CODEX_APP_TOOLS_PIPE_PATH:'synthetic-native-pipe'};
 const observation={chat:'caller',hostId:'local',cwd:root,fail:false};
 const app=()=>new NativeAppPort((()=>({connect:async()=>{},close:async()=>{},listTools:async()=>({tools:[{name:'read_thread'},{name:'send_message_to_thread'}]}),callTool:async()=>{
  if(observation.fail)throw Error('pipe lost');return {structuredContent:{thread:{id:observation.chat,hostId:observation.hostId,cwd:observation.cwd}}};
 }})) as any);
 const calls:any[]=[];let connects=0,closes=0;
 const peer={...config.peer,workplace_sha256:createHash('sha256').update(identity).digest('hex')};
 const backend={onclose:undefined as any,connect:async()=>{connects++;},close:async()=>{closes++;backend.onclose?.();},
  listTools:async()=>({tools:names.map(name=>({name,inputSchema:{type:'object'}}))}),
  callTool:async(request:any)=>{calls.push(request);return {structuredContent:{ok:true,operation:'terminal_read',server:{...peer}},content:[]};}};
 if(toolMode)config.toolMode=toolMode;
 const owner=new PersistentHarnessOwner(config,{app,backend:()=>backend,runtimeSha256:'d'.repeat(64)});
 return {root,config,env,db,app,observation,backend,peer,owner,calls,counts:()=>({connects,closes}),cleanup:async()=>{await owner.stopManual();db.close();rmSync(root,{recursive:true,force:true});}};
}

test('reconnect preserves owner, original IDs and frontend; requires confirmed old tree exit',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env),before=f.owner.status(),catalog=f.owner.catalog();
  await assert.rejects(f.owner.reconnectManual(()=>({}),async()=>{}),/requires_offline/);
  f.backend.onclose();assert.deepEqual(f.owner.catalog(),catalog);
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main'}}),/not_online/);
  await assert.rejects(f.owner.reconnectManual(()=>{throw Error('must not spawn');},async()=>{throw Error('old_tree_active');}),/old_tree_active/);assert.equal(f.counts().connects,1);
  const original={name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command',access_mode:'read_only'}};
  let release!:()=>void;const reconnect=f.owner.reconnectManual(()=>({}),async()=>{await new Promise<void>(r=>release=r);});
  await assert.rejects(f.owner.reconnectManual(()=>({}),async()=>{}),/requires_offline/);await assert.rejects(f.owner.stopManual(),/transition/);
  release();await reconnect;const after=f.owner.status();
  for(const key of ['generation','configurationSha256','runtimeSha256','activeScopes','recovery','attachments'] as const)assert.deepEqual(after[key],before[key]);
  assert.equal(after.backendStarts,2);await f.owner.callTool(a.id,original);assert.deepEqual(f.calls.at(-1),original);assert.equal(f.calls.filter(c=>c.name!=='terminal_read').length,1);
 }finally{await f.cleanup();}
});

test('reconnect rejects changed peer and schema; cached discovery grants no execution',async()=>{
 for(const change of ['peer','schema']){const f=fixture('full');try{
  await f.owner.startManual({});const catalog=f.owner.catalog();f.backend.onclose();
  if(change==='peer')f.peer.release_id='wrong';else f.backend.listTools=async()=>({tools:names.map(name=>({name,inputSchema:{type:'object',description:'changed'}}))});
  await assert.rejects(f.owner.reconnectManual(()=>({}),async()=>{}),/peer_identity|catalog_changed/);
  assert.equal(f.owner.status().state,'offline');assert.deepEqual(f.owner.catalog(),catalog);await assert.rejects(f.owner.attach(f.env),/not_online/);
 }finally{await f.cleanup();}}
});

test('admin reconnect IPC rejects client token and stale owner, preserves live frontend lease',async()=>{
 const f=fixture('full');let service:any,remote:any;
 try{
  const probe=createNetServer();await new Promise<void>(r=>probe.listen(0,'127.0.0.1',r));const port=(probe.address() as any).port;await new Promise<void>(r=>probe.close(()=>r()));
  const s=f.owner.status(),ipc={host:'127.0.0.1' as const,port,clientToken:randomBytes(32).toString('hex'),adminToken:randomBytes(32).toString('hex'),configurationSha256:s.configurationSha256,runtimeSha256:s.runtimeSha256!};
  const policy=new PersistentReconnect(join(f.root,'reconnect.json'));
  service=await servePersistentHarness(f.owner,ipc,()=>f.owner.startManual({}),{policy,connect:()=>f.owner.reconnectManual(()=>({}),async()=>{})});
  remote=persistentRemotePort(ipc);const a=await remote.attach(f.env),before=f.owner.status();f.backend.onclose();
  assert.equal((await remote.catalog()).tools.length,11);
  const expected={generation:before.generation,configurationSha256:before.configurationSha256,runtimeSha256:before.runtimeSha256};
  await assert.rejects(persistentRpc(ipc,'/reconnect',{expected}),/admin_required/);
  await assert.rejects(persistentRpc(ipc,'/reconnect',{expected:{...expected,generation:'stale'}},true),/conflict/);
  await assert.rejects(persistentRpc(ipc,'/reconnect-policy',{expected,enabled:'true'},true),/boolean/);
  const result=await persistentRpc(ipc,'/reconnect',{expected},true);assert.equal(result.generation,before.generation);assert.equal(result.backendStarts,2);
  const request={name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command',access_mode:'read_only'}};
  await remote.callTool(a.id,request);assert.deepEqual(f.calls.at(-1),request);
  assert.equal((await persistentRpc(ipc,'/status')).reconnect.cooldownSeconds>0,true);
 }finally{await remote?.close();await service?.stop();await f.cleanup();}
});
test('attachment cannot start backend; detach/rebuild retains manually started session',async()=>{
 const f=fixture();try{
  await assert.rejects(f.owner.attach(f.env),/not_online/);assert.equal(f.counts().connects,0);
  await f.owner.startManual({});
  for(let i=0;i<20;i++){const a=await f.owner.attach(f.env);assert.deepEqual((await f.owner.listTools(a.id)).tools.map(t=>t.name),['terminal_read']);await f.owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:1}});await f.owner.detach(a.id);}
  assert.equal(f.counts().connects,1);assert.equal(f.counts().closes,0);assert.equal(f.owner.status().backendStarts,1);
  await assert.rejects(f.owner.startManual({}),/new_owner/);
 }finally{await f.cleanup();}
});

test('same workplace cannot acquire another in-process owner through a different endpoint',async()=>{
 const f=fixture();const second=new PersistentHarnessOwner(f.config,{app:f.app,backend:()=>f.backend});
 try{
  await f.owner.startManual({});
  await assert.rejects(second.startManual({}),/workplace_busy/);
  await second.stopManual();
  const third=new PersistentHarnessOwner(f.config,{app:f.app,backend:()=>f.backend});
  await assert.rejects(third.startManual({}),/workplace_busy/);await third.stopManual();
  assert.equal(f.counts().connects,1);await f.owner.stopManual();
  const next=new PersistentHarnessOwner(f.config,{app:f.app,backend:()=>f.backend});
  await next.startManual({});assert.equal(f.counts().connects,2);await next.stopManual();
 }finally{await f.cleanup();}
});

test('controlled rebind requires an existing authorized transfer and preserves the connection and original command',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const before=f.owner.status(),a=await f.owner.attach(f.env);
  initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'fixture-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'fixture-only-approval'},'next',()=>true);
  const digest=()=>createHash('sha256').update(readFileSync(f.config.queuePath)).digest('hex'),unchanged=digest();
  const next={...f.env,CODEX_THREAD_ID:'next'};f.observation.chat='next';
  await assert.rejects(f.owner.rebind(next,{generation:'wrong',epoch:1}),/conflict/);
  await assert.rejects(f.owner.rebind(f.env,{generation:before.generation,epoch:1}));
  const rebound=await f.owner.rebind(next,{generation:before.generation,epoch:1});
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command'}}));
  await f.owner.detach(a.id);
  assert.equal(rebound.activeBinding.chat,'next');assert.equal(rebound.activeBinding.epoch,2);assert.equal(rebound.generation,before.generation);
  assert.equal(f.owner.status().configurationSha256,before.configurationSha256);assert.equal(digest(),unchanged);
  await assert.rejects(f.owner.attach(f.env));const attached=await f.owner.attach(next);
  const original={name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command'}};
  await f.owner.callTool(attached.id,original);assert.deepEqual(f.calls.at(-1),original);assert.equal(f.counts().connects,1);
  await f.owner.detach(attached.id);assert.equal((await f.owner.rebind(next,{generation:before.generation,epoch:2})).changed,false);
 }finally{await f.cleanup();}
});

test('authority changes without a valid complete transfer chain cannot rebind',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});const expected={generation:f.owner.generation,epoch:1};
  f.db.prepare('UPDATE controller_authority SET chat=?,epoch=?').run('next',2);f.observation.chat='next';
  const env={...f.env,CODEX_THREAD_ID:'next'};await assert.rejects(f.owner.rebind(env,expected),/grant_missing/);
  initializeControllerAuthority(f.db);
  f.db.prepare('INSERT INTO controller_transfers VALUES (?,?,?,?)').run('bad',scope,JSON.stringify({expectedEpoch:1}),JSON.stringify({epoch:2}));
  await assert.rejects(f.owner.rebind(env,expected),/grant_invalid/);
  assert.equal(f.owner.status().activeBinding.chat,'caller');assert.equal(f.counts().connects,1);
 }finally{await f.cleanup();}
});

test('restart restores original command/job references from pinned canonical receipts without submission',async()=>{
 const f=fixture('full');let owner:PersistentHarnessOwner|undefined;
 try{
  const job=join(f.root,'original-job'),command=join(f.root,'original-command');mkdirSync(job);mkdirSync(command);
  const focus=join(job,'focus_runtime.json'),request=join(command,'request.json');
  writeFileSync(focus,JSON.stringify({schema_version:1,focus_runtime_integrated:true,capsule_id:'task',work_unit_id:'unit',release_id:'fixture-release'}));
  writeFileSync(request,JSON.stringify({schema_version:2,command_id:'original-command',task_id:'task',work_unit_id:'unit',runtime_release_id:'fixture-release',command_sha256:'c'.repeat(64)}));
  const config={...f.config,recoveryRefs:[{kind:'stage-focus' as const,path:focus,sha256:createHash('sha256').update(readFileSync(focus)).digest('hex')},{kind:'command-request' as const,path:request,sha256:createHash('sha256').update(readFileSync(request)).digest('hex')}]};
  for(let i=0;i<2;i++){
   owner=new PersistentHarnessOwner(config,{app:f.app,backend:()=>f.backend});await owner.startManual({});const a=await owner.attach(f.env);
   assert.deepEqual(owner.status().recovery,{commandIds:['original-command'],jobIds:['original-job']});
   const resume={name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command'}};
   await owner.callTool(a.id,resume);assert.deepEqual(f.calls.at(-1),resume);
   await owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'original-job'}});
   await assert.rejects(owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'other'}}),/scope_rejected/);
   if(i===1){writeFileSync(focus,'{}');await assert.rejects(owner.callTool(a.id,{name:'stage_collect',arguments:{job_id:'original-job'}}),/receipt_mismatch/);}
   await owner.stopManual();owner=undefined;
  }
  assert.equal(f.calls.filter(r=>r.name==='stage_execute'||r.arguments?.worker_task||r.arguments?.command).length,0);
  assert.throws(()=>new PersistentHarnessOwner(config),/receipt_mismatch/);
 }finally{await owner?.stopManual();await f.cleanup();}
});

test('explicit execution-release recovery does not weaken connected-peer or task scope',async()=>{
 const f=fixture('full');let owner:PersistentHarnessOwner|undefined;
 try{
  const dir=join(f.root,'retained-command');mkdirSync(dir);const path=join(dir,'request.json');
  writeFileSync(path,JSON.stringify({schema_version:2,command_id:'retained-command',task_id:'task',work_unit_id:'unit',runtime_release_id:'retained-old-release',command_sha256:'c'.repeat(64)}));
  const ref={kind:'command-request' as const,path,sha256:createHash('sha256').update(readFileSync(path)).digest('hex')};
  assert.throws(()=>new PersistentHarnessOwner({...f.config,recoveryRefs:[ref]}),/command_scope_rejected/);
  const config={...f.config,recoveryRefs:[ref],recoveryReleaseIds:['retained-old-release']};
  owner=new PersistentHarnessOwner(config,{app:f.app,backend:()=>f.backend});await owner.startManual({});const a=await owner.attach(f.env);
  const resume={name:'terminal_exec',arguments:{task_id:'task',command_id:'retained-command'}};
  await owner.callTool(a.id,resume);assert.deepEqual(f.calls.at(-1),resume);
  await assert.rejects(owner.callTool(a.id,{name:'stage_execute',arguments:{task_capsule_id:'task',work_unit_id:'unit',release_id:'retained-old-release'}}),/scope_rejected/);
  await owner.stopManual();owner=undefined;
  f.peer.release_id='retained-old-release';owner=new PersistentHarnessOwner(config,{app:f.app,backend:()=>f.backend});
  await assert.rejects(owner.startManual({}),/peer_identity_mismatch/);await owner.stopManual();owner=undefined;
  const data=JSON.parse(readFileSync(path,'utf8'));data.task_id='another-task';writeFileSync(path,JSON.stringify(data));
  assert.throws(()=>new PersistentHarnessOwner({...config,recoveryRefs:[{...ref,sha256:createHash('sha256').update(readFileSync(path)).digest('hex')}]}),/command_scope_rejected/);
 }finally{await owner?.stopManual();await f.cleanup();}
});

test('runtime pins detect declaration, bundle, executable, environment and configuration changes',async()=>{
 const f=fixture();try{
  const files=['host.mjs','frontend.mjs','guardian.py','python.exe','app.mjs','backend.py'];
  for(const name of files)writeFileSync(join(f.root,name),'fixture '+name);
  f.config.appServerPath=join(f.root,'app.mjs');
  const declaration=join(f.root,'backend.json');
  writeFileSync(declaration,JSON.stringify({mcpServers:{solution_agent_harness:{command:join(f.root,'python.exe'),args:[join(f.root,'backend.py')],cwd:f.root,env:{FIXTURE_PIN:'one'}}}}));
  const c:any={schema:'persistent-harness-host/v1',owner:f.config,installedBackendConfig:declaration,ipc:{host:'127.0.0.1',port:45001,clientToken:'1'.repeat(64),adminToken:'2'.repeat(64)}};
  const paths={hostEntry:join(f.root,'host.mjs'),guardian:join(f.root,'guardian.py'),node:process.execPath,frontend:join(f.root,'frontend.mjs')},env={PATH:'fixture'};
  assert.throws(()=>verifyPersistentRuntime(c,paths,env),/pins_required/);
  c.runtimePins=sealPersistentHost(c,paths,env);const pinned=verifyPersistentRuntime(c,paths,env);
  assert.equal(verifyPersistentRuntime(c,paths,env),pinned);
  if(process.platform==='win32')assert.equal(verifyPersistentRuntime(c,paths,{Path:'fixture'}),pinned);
  assert.throws(()=>verifyPersistentRuntime(c,paths,{...env,NODE_OPTIONS:'--inspect'}),/environment_changed/);
  c.ipc.port++;assert.notEqual(verifyPersistentRuntime(c,paths,env),pinned);c.ipc.port--;
  for(const name of ['host.mjs','frontend.mjs','guardian.py','python.exe','backend.py','backend.json']){
   const path=join(f.root,name),bytes=readFileSync(path);writeFileSync(path,Buffer.concat([bytes,Buffer.from(' changed')]));
   assert.throws(()=>verifyPersistentRuntime(c,paths,env));writeFileSync(path,bytes);
  }
  const saved=c.runtimePins.files;c.runtimePins.files=saved.filter((v:any)=>v.path!==paths.guardian);
  assert.throws(()=>verifyPersistentRuntime(c,paths,env),/pin_missing/);c.runtimePins.files=saved;
  assert.throws(()=>sealPersistentHost(c,{...paths,hostEntry:join(f.root,'source.ts')},env),/bundled_runtime/);
  c.environment={PATH:'retained-runtime-path',NODE_OPTIONS:null};
  c.runtimePins=sealPersistentHost(c,paths,env);const controlled=verifyPersistentRuntime(c,paths,env);
  assert.equal(verifyPersistentRuntime(c,paths,{PATH:'different-manual-shell',NODE_OPTIONS:'--inspect'}),controlled);
  c.environment.SECRET_API_KEY='not-an-environment-control';
  assert.throws(()=>verifyPersistentRuntime(c,paths,env),/host_environment_invalid/);
 }finally{await f.cleanup();}
});

test('Desktop schemas do not attach; actual call metadata still passes App and current authority gates',async()=>{
 const f=fixture('full');let reader:any,client:any;
 try{
  assert.throws(()=>f.owner.catalog(),/not_online/);assert.equal(f.counts().connects,0);
  await f.owner.startManual({});
  const port={catalog:async()=>f.owner.catalog(),attach:(env:any)=>f.owner.attach(env),listTools:(id:string)=>f.owner.listTools(id),callTool:(id:string,r:any)=>f.owner.callTool(id,r),detach:(id:string)=>f.owner.detach(id)};
  reader=await createDesktopPersistentReader(port,{CODEX_APP_TOOLS_PIPE_PATH:f.env.CODEX_APP_TOOLS_PIPE_PATH});
  client=new Client({name:'desktop-host-fixture',version:'1'},{capabilities:{}});
  const [left,right]=InMemoryTransport.createLinkedPair();await reader.server.connect(right);await client.connect(left);
  assert.deepEqual((await client.listTools()).tools.map((t:any)=>t.name),names);assert.equal(f.owner.status().attached,false);assert.equal(f.calls.length,1);
  await assert.rejects(client.callTool({name:'terminal_read',arguments:{terminal_id:'main',thread_id:'caller'}}),/missing_desktop_thread_metadata/);
  await assert.rejects(client.callTool({name:'terminal_read',arguments:{terminal_id:'main'},_meta:{'openai/threadId':'other'}}),/bootstrap_caller_mismatch/);
  assert.equal(f.calls.length,1);assert.equal(f.owner.status().attached,false);
  for(let i=0;i<3;i++){
   await client.callTool({name:'terminal_read',arguments:{terminal_id:'main'},_meta:{'x-codex-turn-metadata':{thread_id:'caller',turn_id:'fixture-turn'}}});
   assert.equal(f.owner.status().attached,false);
  }
  const before=f.calls.length;f.db.prepare('UPDATE controller_authority SET epoch=2').run();
  await assert.rejects(client.callTool({name:'terminal_read',arguments:{terminal_id:'main'},_meta:{'openai/threadId':'caller'}}),/authority_stale/);
  assert.equal(f.calls.length,before);assert.equal(f.owner.status().attached,false);assert.equal(f.counts().connects,1);
 }finally{await reader?.close();await client?.close();await f.cleanup();}
});

test('Desktop metadata rejects missing, malformed, ambiguous and executor-conflicting identity',()=>{
 const env={CODEX_APP_TOOLS_PIPE_PATH:'synthetic-pipe'};
 for(const meta of [undefined,{},[],{'thread_id':3},{'x-codex-turn-metadata':'{'},{'x-codex-turn-metadata':[]},{'thread_id':'a','openai/threadId':'b'},{thread:{id:''}}])assert.throws(()=>desktopCallerContext(env,meta),/desktop_thread_metadata/);
 assert.throws(()=>desktopCallerContext({}, {'thread_id':'caller'}),/trusted_app_context/);
 assert.throws(()=>desktopCallerContext({...env,CODEX_THREAD_ID:'other'},{thread_id:'caller'}),/ambiguous/);
 assert.equal(desktopCallerContext(env,{'x-codex-turn-metadata':JSON.stringify({thread_id:'caller'})}).CODEX_THREAD_ID,'caller');
});

test('Desktop frontend refuses concurrent calls and never executes after transport closes during authorization',async()=>{
 let release!:(v:any)=>void,entered!:()=>void,detached!:()=>void,calls=0;
 const attached=new Promise<void>(r=>entered=r),done=new Promise<void>(r=>detached=r);
 const owner={catalog:async()=>({tools:[{name:'terminal_read',inputSchema:{type:'object' as const}}]}),
  attach:async()=>{entered();return new Promise<any>(r=>release=r);},listTools:async()=>({tools:[]}),
  callTool:async()=>{calls++;return {content:[]};},detach:async()=>{detached();}};
 const reader=await createDesktopPersistentReader(owner,{CODEX_APP_TOOLS_PIPE_PATH:'synthetic-pipe'});
 const client=new Client({name:'closing-host-fixture',version:'1'},{capabilities:{}}),[left,right]=InMemoryTransport.createLinkedPair();
 try{
  await reader.server.connect(right);await client.connect(left);
  const request={name:'terminal_read',arguments:{},_meta:{'openai/threadId':'caller'}};
  const pending=client.callTool(request).catch(()=>undefined);await attached;
  await assert.rejects(client.callTool(request),/persistent_request_pending/);
  await reader.close();release({id:'authorized-after-close',generation:'fixture'});await pending;await done;
  assert.equal(calls,0);
 }finally{await reader.close();await client.close();}
});
test('full mode retains canonical descriptors and forwards scoped commands/receipts without rewriting',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);
  assert.deepEqual((await f.owner.listTools(a.id)).tools.map(t=>t.name),names);
  const original='original-command';const request={name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command_id:original}};
  const expected={structuredContent:{ok:false,task_id:'task',command_id:original,error:{code:'writer_lease_busy'},server:{...f.peer}},content:[]};
  f.backend.callTool=async r=>{f.calls.push(r);return expected;};
  assert.deepEqual(await f.owner.callTool(a.id,request),expected);assert.deepEqual(f.calls.at(-1),request);assert.equal(f.owner.status().state,'online');
  for(const r of [{name:'terminal_exec',arguments:{task_id:'other',command_id:original}},{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'other',worker_task:'never-dispatched'}},{name:'stage_collect',arguments:{job_id:'unbound-job'}},{name:'stage_execute',arguments:{task_capsule_id:'task',work_unit_id:'unit',release_id:'wrong'}}])await assert.rejects(f.owner.callTool(a.id,r),/rejected/);
  assert.equal(f.calls.length,2);assert.equal(f.counts().connects,1);
 }finally{await f.cleanup();}
});
test('ordinary MCP refusal stays connected; uncertain transport is not replayed',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);let count=0;
  const refusal={isError:true,content:[{type:'text',text:'validation refused'}]};
  f.backend.callTool=async()=>{count++;return refusal as any;};
  assert.deepEqual(await f.owner.callTool(a.id,{name:'artifact_store',arguments:{task_id:'task'}}),refusal);assert.equal(f.owner.status().state,'online');
  f.backend.callTool=async()=>{count++;throw new McpError(ErrorCode.InvalidParams,'refused',{command_id:'original'});};
  await assert.rejects(f.owner.callTool(a.id,{name:'artifact_store',arguments:{task_id:'task'}}),(error:any)=>error.code===ErrorCode.InvalidParams&&error.data.command_id==='original');assert.equal(f.owner.status().state,'online');
  f.backend.callTool=async()=>{count++;throw Error('ack lost');};
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'original'}}),/ack lost/);
  assert.equal(count,3);assert.equal(f.counts().connects,1);assert.equal(f.owner.status().state,'offline');
 }finally{await f.cleanup();}
});
test('IPC singleton, authenticated local lease and all-tool MCP frontend reconnect without backend starts',async()=>{
 const f=fixture('full');let service:any;
 const port:number=await new Promise((yes,no)=>{const s=createNetServer();s.once('error',no);s.listen(0,'127.0.0.1',()=>{const p=(s.address() as any).port;s.close(()=>yes(p));});});
 const ipc={host:'127.0.0.1' as const,port,clientToken:randomBytes(32).toString('hex'),adminToken:randomBytes(32).toString('hex'),configurationSha256:f.owner.status().configurationSha256,runtimeSha256:'d'.repeat(64)};
 try{
  const starts=await Promise.allSettled(Array.from({length:10},()=>servePersistentHarness(f.owner,ipc,()=>f.owner.startManual({}))));
  assert.equal(starts.filter(r=>r.status==='fulfilled').length,1);service=(starts.find(r=>r.status==='fulfilled') as any).value;
  assert.ok(starts.filter(r=>r.status==='rejected').every((r:any)=>r.reason.code==='EADDRINUSE'));assert.equal(f.counts().connects,1);
  await assert.rejects(persistentRpc({...ipc,clientToken:'0'.repeat(64)},'/status'),/unauthorized/);
  await assert.rejects(persistentRpc({...ipc,clientToken:'0'.repeat(64)},'/catalog'),/unauthorized/);
  const catalog=await persistentRpc(ipc,'/catalog');assert.equal(catalog.tools.length,11);assert.equal(f.owner.status().attached,false);assert.equal(f.calls.length,1);
  await assert.rejects(persistentRpc(ipc,'/stop',{}),/admin_required/);
  await assert.rejects(persistentRpc(ipc,'/rebind',{}),/admin_required/);
  await assert.rejects(persistentRpc(ipc,'/stop',{expected:{generation:'stale'}},true),/stop_conflict/);
  assert.throws(()=>persistentRemotePort({...ipc,runtimeSha256:undefined}),/pins_required/);
  await assert.rejects(persistentRemotePort({...ipc,runtimeSha256:'e'.repeat(64)}).catalog(),/configuration_mismatch/);
  const origin=await fetch(`http://127.0.0.1:${port}/status`,{headers:{origin:'https://example.invalid',authorization:`Bearer ${ipc.clientToken}`}});assert.equal(origin.status,403);
  // Desktop/SDK clients remain alive between calls; idle leases must coexist.
  const remotes=Array.from({length:3},()=>persistentRemotePort(ipc));
  try{
   const attached=await Promise.all(remotes.map(r=>r.attach(f.env)));
   assert.equal(f.owner.status().attachments,3);
   await Promise.all(remotes.map((r,i)=>r.callTool(attached[i].id,{name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:1}})));
   await remotes[0].close();assert.equal(f.owner.status().attachments,2);
   assert.equal((await remotes[1].listTools(attached[1].id)).tools.length,11);
   await assert.rejects(remotes[0].listTools(attached[0].id),/stale/);
   assert.equal(f.counts().connects,1);
  }finally{await Promise.all(remotes.map(r=>r.close()));}
  for(let i=0;i<5;i++){
   const remote=persistentRemotePort(ipc),reader=await createPersistentReader(remote,f.env),client=new Client({name:'ipc-fixture',version:'1'},{capabilities:{}}),[left,right]=InMemoryTransport.createLinkedPair();
   try{await reader.server.connect(right);await client.connect(left);assert.equal((await client.listTools()).tools.length,11);await client.callTool({name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:1}});
    if(i===4){f.backend.callTool=async()=>{throw new McpError(ErrorCode.InvalidParams,'original refusal',{command_id:'original'});};await assert.rejects(client.callTool({name:'terminal_exec',arguments:{task_id:'task',command_id:'original'}}),(e:any)=>e.code===ErrorCode.InvalidParams&&e.data.command_id==='original');}}
   finally{await reader.close();await remote.close();await client.close();}
  }
  assert.equal(f.counts().connects,1);assert.equal(f.counts().closes,0);
 }finally{await service?.stop();await f.cleanup();}
});
test('every exact peer field and workplace mismatch blocks attachment',async()=>{
 for(const field of ['protocol_version','release_id','source_root_sha256','mcp_schema_sha256','workplace_sha256']){
  const f=fixture();try{(f.peer as any)[field]='wrong';await assert.rejects(f.owner.startManual({}),/peer_/);await assert.rejects(f.owner.attach(f.env),/not_online/);assert.equal(f.counts().connects,1);assert.ok(f.counts().closes>=1);}finally{await f.cleanup();}
 }
});
test('missing context, wrong caller, host and cwd fail real NativeAppPort bootstrap path',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});await assert.rejects(f.owner.attach({CODEX_THREAD_ID:'caller'}),/trusted/);
  for(const [field,value] of [['chat','other'],['hostId','remote'],['cwd',tmpdir()]]){
   const old=(f.observation as any)[field];(f.observation as any)[field]=value;
   await assert.rejects(f.owner.attach(f.env));(f.observation as any)[field]=old;
  }
  assert.equal(f.counts().connects,1);assert.equal(f.owner.status().attached,false);
 }finally{await f.cleanup();}
});
test('authority and App are revalidated per call; no reconnect or tool execution after revocation',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env),before=f.calls.length;
  f.db.prepare('UPDATE controller_authority SET epoch=2').run();await assert.rejects(f.owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main'}}),/stale/);
  f.db.prepare('UPDATE controller_authority SET epoch=1').run();f.observation.fail=true;
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main'}}),/pipe lost/);
  assert.equal(f.calls.length,before);assert.equal(f.counts().connects,1);
 }finally{await f.cleanup();}
});
test('pending/corrupt rollover, unregistered scope and wrong task fail without authority mutation',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});
  for(const value of ['bad-json',JSON.stringify({schema:'context-rollover/v1',phase:'requested'})]){
   f.db.prepare('INSERT OR REPLACE INTO meta VALUES (?,?)').run('context_rollover',value);await assert.rejects(f.owner.attach(f.env));
  }
  f.db.prepare('DELETE FROM meta').run();f.db.prepare('UPDATE publisher_routes SET binding=?').run(JSON.stringify({task_id:'other',work_unit_id:'unit'}));await assert.rejects(f.owner.attach(f.env),/scope_mismatch/);
  f.db.prepare('DELETE FROM publisher_routes').run();await assert.rejects(f.owner.attach(f.env),/not_registered/);
  assert.equal(f.counts().connects,1);
 }finally{await f.cleanup();}
});
test('read-only surface rejects writes, oversize reads and stale IDs while independent attachments coexist',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);
  const second=await f.owner.attach(f.env);assert.equal(f.owner.status().attachments,2);
  for(const request of [{name:'terminal_exec',arguments:{worker_task:'forbidden'}},{name:'terminal_read',arguments:{terminal_id:'other'}},{name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:9000}},{name:'terminal_read',arguments:{terminal_id:'main',_meta:{threadId:'other'}}}])await assert.rejects(f.owner.callTool(a.id,request),/policy/);
  await f.owner.detach(a.id);await assert.rejects(f.owner.listTools(a.id),/stale/);
  assert.deepEqual((await f.owner.listTools(second.id)).tools.map(t=>t.name),['terminal_read']);await f.owner.detach(second.id);
  assert.equal(f.calls.length,1);
 }finally{await f.cleanup();}
});
test('backend response identity drift closes the gate and never retries',async()=>{
 const f=fixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.peer.release_id='changed';
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main'}}),/identity/);
  assert.equal(f.owner.status().state,'offline');await f.owner.detach(a.id);await assert.rejects(f.owner.attach(f.env),/not_online/);assert.equal(f.counts().connects,1);
 }finally{await f.cleanup();}
});
test('real Python Harness MCP and existing protocol/workplace gates survive 20 reconstructed MCP readers',async()=>{
 const f=fixture(),repo=resolve(fileURLToPath(new URL('../..',import.meta.url)));
 const python=join(repo,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
 const script=join(f.root,'local_backend.py');
 // Real MCP registration and startup checks; only the OS/tmux terminal implementation is a fixture.
 writeFileSync(script,`import os, sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\nfrom solution_agent.harness import mcp_server as s\nfrom solution_agent.harness.remote_terminal import TerminalProfile, TerminalSlot\np=Path(sys.argv[1])\nprofile=TerminalProfile('fixture','fixture',Path(os.environ['SOLUTION_AGENT_ROOT']),p/'runs','fixture','main',{'main':TerminalSlot('main','main',p,'/bin/sh')},65536,2)\ns.load_terminal_profile=lambda root:profile\ns._terminal_manager=lambda:SimpleNamespace(read=lambda terminal_id,**kwargs:{'ok':True,'operation':'terminal_read','backend_pid':os.getpid(),'fixture_only':True})\ns.main()\n`);
 mkdirSync(join(f.root,'.solution-agent'));writeFileSync(join(f.root,'.solution-agent','workplace.id'),readFileSync(f.config.workplaceIdentityPath));
 const identity=JSON.parse(execFileSync(python,['-c',"import json,sys;from pathlib import Path;from solution_agent.harness.release_identity import build_runtime_identity;print(json.dumps(build_runtime_identity(Path(sys.argv[1]),profile_id='fixture').compact()))",repo],{cwd:repo,encoding:'utf8',env:{...process.env,PYTHONPATH:repo}}));
 const config={...f.config,peer:{protocol_version:'0.6',release_id:identity.release_id,source_root_sha256:identity.source_root_sha256,mcp_schema_sha256:identity.mcp_schema_sha256}};
 const owner=new PersistentHarnessOwner(config,{app:f.app});
 const env={...process.env,PYTHONPATH:repo,SOLUTION_AGENT_ROOT:repo,SOLUTION_AGENT_PROTOCOL_REQUIRED:'1',SOLUTION_AGENT_EXPECTED_IDENTITY_B64:Buffer.from(JSON.stringify(config.peer)).toString('base64'),SOLUTION_AGENT_WORKPLACE_IDENTITY_B64:readFileSync(f.config.workplaceIdentityPath).toString('base64')};
 const transport=new StdioClientTransport({command:python,args:[script,f.root],cwd:repo,env:env as any,stderr:'pipe'});
 try{
  await owner.startManual(transport);const launcherPid=transport.pid;assert.ok(launcherPid);let backendPid:number|undefined;
  for(let i=0;i<20;i++){
   const reader=await createPersistentReader(owner,f.env),client=new Client({name:'rebuilt-reader',version:'1'},{capabilities:{}}),[left,right]=InMemoryTransport.createLinkedPair();
   try{
    await reader.server.connect(right);await client.connect(left);
    assert.deepEqual((await client.listTools()).tools.map(t=>t.name),['terminal_read']);
    const response:any=await client.callTool({name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:8}});
    // Windows venv launchers may have a different PID from their Python interpreter.
    const pid=response.structuredContent.backend_pid;assert.ok(Number.isSafeInteger(pid)&&pid>0);
    backendPid??=pid;assert.equal(pid,backendPid);assert.equal(transport.pid,launcherPid);
    assert.equal(response.structuredContent.server.workplace_sha256,f.peer.workplace_sha256);assert.equal(response.structuredContent.server.protocol_version,'0.6');
   }finally{await reader.close();await client.close();}
  }
  assert.equal(owner.status().backendStarts,1);assert.equal(owner.status().state,'online');
 }finally{await owner.stopManual();await f.cleanup();}
});

test('multiple genuine callers and tasks reuse one backend without sharing task authority',async()=>{
 const f=fixture('full'),other=controllerScopeForTest('other-task','other-unit');
 f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'other-task',work_unit_id:'other-unit',origin_chat_id:'other-caller'}));
 f.db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(other,'other-caller',0);
 const c={...f.config,additionalScopes:[{workspace:f.root,queuePath:f.config.queuePath,binding:{chat:'other-caller',epoch:0,taskId:'other-task',workUnitId:'other-unit'},terminalIds:['main'],jobIds:['other-job']}]};
 const owner=new PersistentHarnessOwner(c,{app:f.app,backend:()=>f.backend});
 try{
  await owner.startManual({});const initial=owner.status();
  let a=await owner.attach(f.env);await owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'original-command'}});
  await assert.rejects(owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'other-task',work_unit_id:'other-unit',command:'wrong'}}),/scope_rejected/);
  await assert.rejects(owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'other-job'}}),/job_scope_rejected/);
  await owner.callTool(a.id,{name:'terminal_open',arguments:{task_id:'task',terminal_id:'main'}});await owner.detach(a.id);
  f.observation.chat='other-caller';const env={...f.env,CODEX_THREAD_ID:'other-caller'};
  a=await owner.attach(env);
  await owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'same-original-id'}});
  assert.equal(f.calls.at(-1).arguments.command_id,'same-original-id');
  await owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'other-job'}});
  const calls=f.calls.length;
  for(const request of [{name:'terminal_open',arguments:{task_id:'other-task',terminal_id:'main'}},{name:'terminal_send',arguments:{terminal_id:'main',input:'wrong'}},{name:'terminal_close',arguments:{terminal_id:'main'}}])await assert.rejects(owner.callTool(a.id,request),/terminal_scope_rejected/);
  assert.equal(f.calls.length,calls);
  f.db.prepare('UPDATE controller_authority SET epoch=1 WHERE scope=?').run(other);
  await assert.rejects(owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'other-job'}}),/authority_stale/);assert.equal(f.calls.length,calls);await owner.detach(a.id);
  f.observation.chat='caller';a=await owner.attach(f.env);await owner.callTool(a.id,{name:'terminal_read',arguments:{terminal_id:'main',max_output_bytes:1}});await owner.detach(a.id);
  assert.equal(f.counts().connects,1);assert.equal(owner.status().generation,initial.generation);assert.equal(owner.status().activeScopes.length,2);
 }finally{await owner.stopManual();await f.cleanup();}
});

const controllerScopeForTest=(task:string,unit:string)=>createHash('sha256').update(JSON.stringify([task,unit])).digest('hex');

// Synthetic actors only: the App adapter binds each connection to its own caller,
// so a surviving old lease cannot accidentally become the new caller in a test.
function handoffApp(f:ReturnType<typeof fixture>,beforeRead?:()=>Promise<void>){
 return ()=>{let caller='';return {connect:async(_path:any,env:any)=>{caller=env.CODEX_THREAD_ID;},identity:()=>({threadId:caller}),read:async()=>{await beforeRead?.();return {thread:{id:caller,hostId:'local',cwd:f.root}};},close:async()=>{}} as any;};
}

test('IPC handoff retains idle frontends and unrelated scope while revoking the old caller',async()=>{
 const f=fixture('full'),other=controllerScope('other-task','other-unit');let service:any;
 f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'other-task',work_unit_id:'other-unit',origin_chat_id:'other'}));
 const owner=new PersistentHarnessOwner({...f.config,authorityStores:[{workspace:f.root,queuePath:f.config.queuePath,terminalIds:['main']}]},{app:handoffApp(f),backend:()=>f.backend,runtimeSha256:'d'.repeat(64)});
 const port:number=await new Promise((yes,no)=>{const s=createNetServer();s.once('error',no);s.listen(0,'127.0.0.1',()=>{const p=(s.address() as any).port;s.close(()=>yes(p));});});
 const ipc={host:'127.0.0.1' as const,port,clientToken:randomBytes(32).toString('hex'),adminToken:randomBytes(32).toString('hex'),configurationSha256:owner.status().configurationSha256,runtimeSha256:'d'.repeat(64)};
 const old=persistentRemotePort(ipc),unrelated=persistentRemotePort(ipc),next=persistentRemotePort(ipc);
 try{
  service=await servePersistentHarness(owner,ipc,()=>owner.startManual({}));
  const a=await old.attach(f.env),b=await unrelated.attach({...f.env,CODEX_THREAD_ID:'other'});
  initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'ipc-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'synthetic-user-approved'},'next',()=>true);
  const before=owner.status();
  await persistentRpc(ipc,'/rebind',{expected:{generation:before.generation,epoch:1,scope},context:{...f.env,CODEX_THREAD_ID:'next'}},true);
  assert.equal(owner.status().attachments,2);
  const count=f.calls.length;
  await assert.rejects(old.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'old-must-not-forward'}}),/context_mismatch|authority_stale/);
  assert.equal(f.calls.length,count);
  await unrelated.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'other-original'}});
  const c=await next.attach({...f.env,CODEX_THREAD_ID:'next'});
  const request={name:'terminal_exec',arguments:{task_id:'task',command_id:'original-command'}};
  await next.callTool(c.id,request);assert.deepEqual(f.calls.at(-1),request);
  await assert.rejects(next.callTool(c.id,{name:'artifact_inspect',arguments:{task_id:'other-task',artifact_ref:'sha256:unrelated'}}),/scope_rejected/);
  assert.equal(owner.status().generation,before.generation);assert.equal(f.counts().connects,1);
 }finally{await Promise.all([old.close(),unrelated.close(),next.close()]);await service?.stop();await owner.stopManual();await f.cleanup();}
});

test('rebind refuses pending work and blocks call admission during genuine target verification',async()=>{
 const f=fixture('full');let readGate:Promise<void>|undefined,enteredRead:(()=>void)|undefined;
 const owner=new PersistentHarnessOwner(f.config,{app:handoffApp(f,async()=>{if(readGate){enteredRead?.();await readGate;}}),backend:()=>f.backend});
 try{
  await owner.startManual({});const a=await owner.attach(f.env);
  let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(r=>release=r),started=new Promise<void>(r=>entered=r);
  f.backend.callTool=async r=>{f.calls.push(r);entered();await gate;return {structuredContent:{ok:true,server:{...f.peer}},content:[]};};
  const active=owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'already-running'}});await started;
  initializeControllerAuthority(f.db);transferController(f.db,{transferId:'busy-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'synthetic-only'},'next',()=>true);
  const env={...f.env,CODEX_THREAD_ID:'next'},expected={generation:owner.generation,epoch:1};
  await assert.rejects(owner.rebind(env,expected),/frontend_busy/);release();await active;
  let releaseRead!:()=>void;readGate=new Promise<void>(r=>releaseRead=r);const verificationStarted=new Promise<void>(r=>enteredRead=r);
  const rebind=owner.rebind(env,expected);await verificationStarted;
  await assert.rejects(owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'racing'}}),/frontend_busy/);
  await assert.rejects(owner.attach(env),/frontend_busy/);await assert.rejects(owner.stopManual(),/request_pending/);
  releaseRead();await rebind;readGate=undefined;
  await assert.rejects(owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'revoked'}}),/context_mismatch|authority_stale/);
  assert.deepEqual(f.calls.slice(1).map(r=>r.arguments.command_id),['already-running']);assert.equal(f.counts().connects,1);
 }finally{await owner.stopManual();await f.cleanup();}
});

test('cold owner can explicitly rebind stale pinned config and recover original IDs without replay',async()=>{
 const f=fixture('full');let owner:PersistentHarnessOwner|undefined;
 try{
  const job=join(f.root,'retained-job');mkdirSync(job);const path=join(job,'focus_runtime.json');
  writeFileSync(path,JSON.stringify({schema_version:1,focus_runtime_integrated:true,capsule_id:'task',work_unit_id:'unit',release_id:'fixture-release'}));
  const config={...f.config,recoveryRefs:[{kind:'stage-focus' as const,path,sha256:createHash('sha256').update(readFileSync(path)).digest('hex')}]};
  const generation:string[]=[];
  initializeControllerAuthority(f.db);transferController(f.db,{transferId:'restart-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'synthetic-only'},'next',()=>true);
  for(let i=0;i<2;i++){
   owner=new PersistentHarnessOwner(config,{app:handoffApp(f),backend:()=>f.backend});await owner.startManual({});generation.push(owner.generation);
   await assert.rejects(owner.attach(f.env),/stale/);
   const env={...f.env,CODEX_THREAD_ID:'next'};
   await owner.rebind(env,{generation:owner.generation,epoch:1});const a=await owner.attach(env);
   for(const request of [{name:'terminal_exec',arguments:{task_id:'task',command_id:'retained-command'}},{name:'stage_status',arguments:{job_id:'retained-job'}},{name:'artifact_inspect',arguments:{task_id:'task',artifact_ref:'sha256:retained-artifact'}}]){
    await owner.callTool(a.id,request);assert.deepEqual(f.calls.at(-1),request);
   }
   await owner.stopManual();owner=undefined;
  }
  assert.notEqual(generation[0],generation[1]);assert.equal(f.counts().connects,2);
  assert.equal(f.calls.some(r=>r.name==='stage_execute'||r.arguments?.command||r.arguments?.worker_task),false);
 }finally{await owner?.stopManual();await f.cleanup();}
});

test('additional scope does not inherit base recovery receipts or job grants',async()=>{
 const f=fixture('full');let owner:PersistentHarnessOwner|undefined;const other=controllerScope('other-task','other-unit');
 try{
  f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'other-task',work_unit_id:'other-unit',origin_chat_id:'other'}));
  const job=join(f.root,'base-job');mkdirSync(job);const path=join(job,'focus_runtime.json');
  writeFileSync(path,JSON.stringify({schema_version:1,focus_runtime_integrated:true,capsule_id:'task',work_unit_id:'unit',release_id:'fixture-release'}));
  owner=new PersistentHarnessOwner({...f.config,jobIds:['base-job'],recoveryRefs:[{kind:'stage-focus',path,sha256:createHash('sha256').update(readFileSync(path)).digest('hex')}],additionalScopes:[{workspace:f.root,queuePath:f.config.queuePath,binding:{chat:'other',epoch:0,taskId:'other-task',workUnitId:'other-unit'},terminalIds:['main']}]},{app:handoffApp(f),backend:()=>f.backend});
  await owner.startManual({});const a=await owner.attach({...f.env,CODEX_THREAD_ID:'other'}),count=f.calls.length;
  await assert.rejects(owner.callTool(a.id,{name:'stage_status',arguments:{job_id:'base-job'}}),/job_scope_rejected/);assert.equal(f.calls.length,count);
 }finally{await owner?.stopManual();await f.cleanup();}
});

test('additional scope cannot override peer policy or duplicate an authority scope',async()=>{
 const f=fixture('full');try{
  const scope={workspace:f.root,queuePath:f.config.queuePath,binding:{...f.config.binding},terminalIds:['main']};
  assert.throws(()=>new PersistentHarnessOwner({...f.config,additionalScopes:[scope]}),/scope_duplicate/);
  assert.throws(()=>new PersistentHarnessOwner({...f.config,additionalScopes:[{...scope,binding:{...scope.binding,taskId:'other'},peer:{release_id:'wrong'}} as any]}),/scope_fields_rejected/);
  assert.equal(f.counts().connects,0);
 }finally{await f.cleanup();}
});

test('scope-selected committed rebind changes only that caller and retains the transport',async()=>{
 const f=fixture('full'),other=controllerScopeForTest('other-task','other-unit');
 f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'other-task',work_unit_id:'other-unit',origin_chat_id:'other-caller'}));
 f.db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(other,'other-caller',0);
 const c={...f.config,additionalScopes:[{workspace:f.root,queuePath:f.config.queuePath,binding:{chat:'other-caller',epoch:0,taskId:'other-task',workUnitId:'other-unit'},terminalIds:['other']}]};
 const owner=new PersistentHarnessOwner(c,{app:f.app,backend:()=>f.backend});
 try{
  await owner.startManual({});initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'other-transfer',taskId:'other-task',workUnitId:'other-unit',fromChat:'other-caller',toChat:'next',expectedEpoch:0,authorizationRef:'synthetic-user-grant'},'next',()=>true);
  f.observation.chat='next';const env={...f.env,CODEX_THREAD_ID:'next'};
  await assert.rejects(owner.rebind(env,{generation:owner.generation,epoch:0,scope:'unknown'}),/scope_rejected/);
  await owner.rebind(env,{generation:owner.generation,epoch:0,scope:other});
  assert.equal(owner.status().activeBinding.chat,'caller');assert.equal(owner.status().activeScopes[1].chat,'next');
  const a=await owner.attach(env);await owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'retained'}});await owner.detach(a.id);
  assert.equal(f.counts().connects,1);
 }finally{await owner.stopManual();await f.cleanup();}
});

test('new trusted publisher registrations become usable without restart or new authority state',async()=>{
 const f=fixture('full');const owner=new PersistentHarnessOwner({...f.config,authorityStores:[{workspace:f.root,queuePath:f.config.queuePath,terminalIds:['main']}]},{app:f.app,backend:()=>f.backend});
 try{
  await owner.startManual({});const generation=owner.generation;
  const other=controllerScopeForTest('registered-later','registered-unit');
  f.observation.chat='other-caller';const env={...f.env,CODEX_THREAD_ID:'other-caller'};
  await assert.rejects(owner.attach(env),/context_mismatch/);
  f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'registered-later',work_unit_id:'registered-unit',origin_chat_id:'other-caller'}));
  const before=readFileSync(f.config.queuePath);
  const a=await owner.attach(env);
  await owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'registered-later',work_unit_id:'registered-unit',command:'maintenance'}});
  await assert.rejects(owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'not-registered',work_unit_id:'registered-unit',command:'forbidden'}}),/scope_rejected/);
  assert.deepEqual(readFileSync(f.config.queuePath),before);assert.equal(owner.status().backendStarts,1);assert.equal(owner.generation,generation);
  await owner.detach(a.id);initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'late-transfer',taskId:'registered-later',workUnitId:'registered-unit',fromChat:'other-caller',toChat:'next',expectedEpoch:0,authorizationRef:'synthetic-only-approved-transfer'},'next',()=>true);
  f.observation.chat='next';const next={...env,CODEX_THREAD_ID:'next'};
  await assert.rejects(owner.attach(next),/context_mismatch/);
  await owner.rebind(next,{generation,epoch:0,scope:other});const b=await owner.attach(next);await owner.detach(b.id);
  assert.equal(f.counts().connects,1);
 }finally{await owner.stopManual();await f.cleanup();}
});

test('explicit local registration uses genuine App identity and refuses rebinding without SSH',async()=>{
 const f=fixture('full'),queuePath=join(f.root,'registration.sqlite');
 const initialized=new ReviewedStore(queuePath);initialized.close();
 const config={...f.config,queuePath};const input={taskId:'maintenance-task',workUnitId:'maintenance-unit',source:'synthetic-declared-local-source'};
 try{
  const result=await registerPersistentScope(config,f.env,input,{app:f.app});assert.equal(result.dispatched,false);
  const db=new DatabaseSync(queuePath,{readOnly:true});
  try{assert.equal(JSON.parse((db.prepare('SELECT binding FROM publisher_routes').get() as any).binding).origin_chat_id,'caller');}finally{db.close();}
  f.observation.chat='other-caller';const other={...f.env,CODEX_THREAD_ID:'other-caller'};
  await assert.rejects(registerPersistentScope(config,other,input,{app:f.app}),/rebind_rejected/);
  f.observation.cwd=dirname(f.root);
  await assert.rejects(registerPersistentScope(config,other,{...input,workUnitId:'different'},{app:f.app}),/store_rejected/);
  assert.equal(f.counts().connects,0);
 }finally{await f.cleanup();}
});

test('multiple callers serialize once and recheck queued authority without sharing task access',async()=>{
 const f=fixture('full');let owner:PersistentHarnessOwner|undefined;const other=controllerScope('other-task','other-unit');
 f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'other-task',work_unit_id:'other-unit',origin_chat_id:'other-caller'}));
 f.db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(other,'other-caller',0);
 const app=()=>{let caller='';return {connect:async(_path:any,env:any)=>{caller=env.CODEX_THREAD_ID;},identity:()=>({threadId:caller}),read:async()=>({thread:{id:caller,hostId:'local',cwd:f.root}}),close:async()=>{}} as any;};
 try{
  owner=new PersistentHarnessOwner({...f.config,authorityStores:[{workspace:f.root,queuePath:f.config.queuePath,terminalIds:['main']}]},{app,backend:()=>f.backend});
  await owner.startManual({});const a=await owner.attach(f.env),b=await owner.attach({...f.env,CODEX_THREAD_ID:'other-caller'});
  let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(r=>release=r),started=new Promise<void>(r=>entered=r);const forwarded:any[]=[];let running=0,maxRunning=0;
  f.backend.callTool=async(request:any)=>{running++;maxRunning=Math.max(maxRunning,running);forwarded.push(request);if(request.arguments.command_id==='first'){entered();await gate;}running--;return {structuredContent:{ok:true,server:{...f.peer}},content:[]};};
  const first=owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'first'}});await started;
  const second=owner.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'second'}});
  const refused=assert.rejects(owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'forbidden'}}),/scope_rejected/);
  await assert.rejects(owner.stopManual(),/request_pending/);release();await Promise.all([first,second,refused]);
  assert.deepEqual(forwarded.map(x=>x.arguments.command_id),['first','second']);assert.equal(maxRunning,1);assert.equal(owner.status().attachments,2);assert.equal(f.counts().connects,1);
  let releaseAgain!:()=>void,enteredAgain!:()=>void;const gateAgain=new Promise<void>(r=>releaseAgain=r),startedAgain=new Promise<void>(r=>enteredAgain=r);
  f.backend.callTool=async(request:any)=>{forwarded.push(request);enteredAgain();await gateAgain;return {structuredContent:{ok:true,server:{...f.peer}},content:[]};};
  const blocking=owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'blocking'}});await startedAgain;
  const stale=assert.rejects(owner.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'other-task',command_id:'must-not-submit'}}),/authority_stale/);
  f.db.prepare('UPDATE controller_authority SET chat=?,epoch=? WHERE scope=?').run('next-caller',1,other);releaseAgain();await Promise.all([blocking,stale]);
  assert.equal(forwarded.length,3);assert.equal(owner.status().pending,0);await owner.detach(a.id);assert.equal(owner.status().attachments,1);
 }finally{await owner?.stopManual();await f.cleanup();}
});

test('attachments and pending calls are bounded; queued loss never replays on a new backend',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const attached=[];for(let i=0;i<64;i++)attached.push(await f.owner.attach(f.env));
  await assert.rejects(f.owner.attach(f.env),/frontend_limit/);
  let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(r=>release=r),started=new Promise<void>(r=>entered=r);let forwarded=0;
  f.backend.callTool=async()=>{forwarded++;entered();await gate;throw Error('fixture backend lost');};
  const first=assert.rejects(f.owner.callTool(attached[0].id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'original'}}),/backend lost/);await started;
  const queued=Array.from({length:63},(_,i)=>assert.rejects(f.owner.callTool(attached[i+1].id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'queued-'+i}}),/stale|not_online/));
  await assert.rejects(f.owner.callTool(attached[0].id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'overflow'}}),/queue_full/);
  release();await Promise.all([first,...queued]);assert.equal(forwarded,1);assert.equal(f.counts().connects,1);assert.equal(f.owner.status().state,'offline');assert.equal(f.owner.status().pending,0);
 }finally{await f.cleanup();}
});

function commandMutationFixture(){
 const f=fixture('full'),other=controllerScope('task','other-unit');
 f.db.prepare('INSERT INTO publisher_routes VALUES (?,?)').run(other,JSON.stringify({task_id:'task',work_unit_id:'other-unit',origin_chat_id:'other-caller'}));
 f.db.prepare('INSERT INTO controller_authority VALUES (?,?,?)').run(other,'other-caller',0);
 const owner=new PersistentHarnessOwner({...f.config,additionalScopes:[{workspace:f.root,queuePath:f.config.queuePath,binding:{chat:'other-caller',epoch:0,taskId:'task',workUnitId:'other-unit'},terminalIds:['other']}]},{app:f.app,backend:()=>f.backend});
 return {...f,owner,other,cleanup:async()=>{await owner.stopManual();await f.cleanup();}};
}

test('original-ID read remains optional-unit; wrong-ID cancel/finish cannot mutate another unit',async()=>{
 const f=commandMutationFixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  f.backend.callTool=async(request:any)=>{f.calls.push(request);return {structuredContent:{ok:true,command_id:'saved-B',task_id:'task',work_unit_id:'other-unit',state:'running',server:{...f.peer}},content:[]};};
  await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-B'}});
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].arguments.work_unit_id,undefined);
  for(const effect of ['cancel','finish_maintenance'])for(const unit of [undefined,'unit','other-unit']){
   f.calls.length=0;
   await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-B',[effect]:true,...(unit?{work_unit_id:unit}:{})}}),/command_scope_rejected/);
   assert.equal(f.calls.length,1);assert.equal(f.calls[0].arguments.cancel,undefined);assert.equal(f.calls[0].arguments.finish_maintenance,undefined);
   assert.equal(f.owner.status().state,'online');
  }
  assert.equal(f.owner.status().backendStarts,1);
 }finally{await f.cleanup();}
});

test('command mutation infers canonical unit and follows a committed current-controller rebind',async()=>{
 const f=commandMutationFixture();try{
  await f.owner.startManual({});const generation=f.owner.generation;
  initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'synthetic-command-transfer',taskId:'task',workUnitId:'other-unit',fromChat:'other-caller',toChat:'caller',expectedEpoch:0,authorizationRef:'synthetic-approved-only'},'caller',()=>true);
  await f.owner.rebind(f.env,{generation,epoch:0,scope:f.other});const a=await f.owner.attach(f.env);f.calls.length=0;
  f.backend.callTool=async(request:any)=>{f.calls.push(request);return {structuredContent:{ok:true,command_id:'saved-B',task_id:'task',work_unit_id:'other-unit',state:request.arguments.cancel?'cancelled':'running',server:{...f.peer}},content:[]};};
  await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-B',cancel:true}});
  assert.equal(f.calls.length,2);assert.equal(f.calls[0].arguments.cancel,undefined);
  assert.equal(f.calls[1].arguments.cancel,true);assert.equal(f.calls[1].arguments.work_unit_id,'other-unit');
  assert.equal(f.owner.generation,generation);assert.equal(f.owner.status().backendStarts,1);
 }finally{await f.cleanup();}
});

test('authority changed while canonical lookup awaited blocks mutation without closing transport',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;initializeControllerAuthority(f.db);
  f.backend.callTool=async(request:any)=>{f.calls.push(request);transferController(f.db,{transferId:'synthetic-race-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'synthetic-approved-only'},'next',()=>true);return {structuredContent:{ok:true,command_id:'saved-A',task_id:'task',work_unit_id:'unit',server:{...f.peer}},content:[]};};
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-A',cancel:true}}),/authority_stale/);
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].arguments.cancel,undefined);assert.equal(f.owner.status().state,'online');
 }finally{await f.cleanup();}
});

test('missing or mismatched canonical command binding fails closed for side effects',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);
  for(const bound of [{command_id:'different',task_id:'task',work_unit_id:'unit'},{command_id:'saved-A',task_id:'different',work_unit_id:'unit'},{command_id:'saved-A',task_id:'task',work_unit_id:null}]){
   f.calls.length=0;f.backend.callTool=async(request:any)=>{f.calls.push(request);return {structuredContent:{ok:true,...bound,server:{...f.peer}},content:[]};};
   await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-A',cancel:true}}),/command_binding_required/);
   assert.equal(f.calls.length,1);assert.equal(f.owner.status().state,'online');
  }
 }finally{await f.cleanup();}
});

test('mutation flags cannot be coerced around canonical authorization; own maintenance infers its unit',async()=>{
 const f=fixture('full');try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  for(const key of ['cancel','finish_maintenance','recover_worker'])for(const value of [1,'true',null,{}])await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-A',[key]:value}}),/flags_rejected/);
  assert.equal(f.calls.length,0);
  f.backend.callTool=async(request:any)=>{f.calls.push(request);return {structuredContent:{ok:true,command_id:'saved-A',task_id:'task',work_unit_id:'unit',server:{...f.peer}},content:[]};};
  await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',command_id:'saved-A',finish_maintenance:true}});
  assert.equal(f.calls.length,2);assert.equal(f.calls[0].arguments.finish_maintenance,undefined);assert.equal(f.calls[1].arguments.finish_maintenance,true);assert.equal(f.calls[1].arguments.work_unit_id,'unit');
 }finally{await f.cleanup();}
});

function durableSubmissionFixture(){
 const f=fixture('full'),journalPath=join(f.root,'private-intents');mkdirSync(journalPath);
 f.backend.listTools=async()=>({tools:names.map(name=>({name,inputSchema:{type:'object',properties:name==='terminal_exec'?{submission_id:{type:'string'},recover_submission:{type:'boolean'},submission_cursor:{type:'integer'}}:{}}}))});
 const config={...f.config,submissionJournalPath:journalPath};
 const owner=new PersistentHarnessOwner(config,{app:f.app,backend:()=>f.backend});
 return {...f,owner,config,journalPath,journal:new PersistentSubmissions(journalPath),cleanup:async()=>{await owner.stopManual();await f.cleanup();}};
}
function submissionReply(f:any,request:any){
 const a=request.arguments,record=f.journal.list().find((r:any)=>r.commandId===a.command_id||r.submissionId===a.submission_id);
 assert.ok(record);
 return {structuredContent:{ok:true,command_id:record.commandId,task_id:record.taskId,work_unit_id:record.workUnitId,submission_id:record.submissionId,command_sha256:record.commandSha256,state:'succeeded',runtime_release_id:'original-release',server:{...f.peer}},content:[]};
}

test('new plain intents are persisted before queued delivery; frontend loss never resubmits',async()=>{
 const f=durableSubmissionFixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(r=>release=r),started=new Promise<void>(r=>entered=r);
  f.backend.callTool=async(request:any)=>{f.calls.push(request);if(request.arguments.command==='first'){entered();await gate;}return submissionReply(f,request);};
  const first=f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'first'}});await started;
  const second=assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'second'}}),/attachment_stale/);
  assert.deepEqual(f.journal.list().map(r=>r.phase).sort(),['queued','sent']);
  await f.owner.detach(a.id);release();await first;await second;
  assert.equal(f.calls.filter(r=>r.arguments.command).length,1);assert.deepEqual(f.journal.list().map(r=>r.phase).sort(),['denied','received']);
  const b=await f.owner.attach(f.env);const recovered=await f.owner.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'task',recover_submission:true}});
  assert.equal(recovered.structuredContent.lookup_only,true);assert.equal(recovered.structuredContent.receipts.length,2);
  assert.equal(f.calls.filter(r=>r.arguments.command).length,1);assert.equal(f.owner.status().backendStarts,1);
 }finally{await f.cleanup();}
});

test('lost ACK and owner restart recover the same saved plain intent without another command launch',async()=>{
 const f=durableSubmissionFixture();let next:PersistentHarnessOwner|undefined;try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  f.backend.callTool=async(request:any)=>{f.calls.push(request);throw Error('synthetic lost ACK after durable backend submit');};
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'original'}}),/lost ACK/);
  const saved=f.journal.list()[0];assert.equal(saved.phase,'unknown');assert.equal(saved.commandId,submissionCommandId('task','unit',saved.submissionId));
  await f.owner.stopManual();f.backend.callTool=async(request:any)=>request.name==='terminal_read'?{structuredContent:{ok:true,server:{...f.peer}},content:[]}:(f.calls.push(request),submissionReply(f,request));
  next=new PersistentHarnessOwner(f.config,{app:f.app,backend:()=>f.backend});await next.startManual({});const b=await next.attach(f.env);
  const result=await next.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',recover_submission:true}});
  assert.equal(result.structuredContent.receipts[0].command_id,saved.commandId);assert.equal(f.journal.list()[0].phase,'received');
  assert.equal(f.calls.filter(r=>r.arguments.command).length,1);assert.equal(result.structuredContent.receipts[0].response.structuredContent.runtime_release_id,'original-release');
 }finally{await next?.stopManual();await f.cleanup();}
});

test('journal cannot fabricate another scope or replay queued work after owner loss',async()=>{
 const f=durableSubmissionFixture();try{
  const queued=f.journal.begin(f.config.binding,'old-generation',{command:'never sent'});
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  const result=await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',recover_submission:true}});
  assert.equal(result.structuredContent.receipts[0].command_id,queued.commandId);assert.equal(result.structuredContent.receipts[0].disposition,'no_dispatch_receipt_do_not_resubmit');assert.equal(f.calls.length,0);
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'other-unit',recover_submission:true}}),/submission_scope_rejected/);
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'model nonce',submission_id:'a'.repeat(32)}}),/program_submission_rejected/);
 }finally{await f.cleanup();}
});

test('journal-enabled owner refuses an unpaired older runtime before tool dispatch',async()=>{
 const f=durableSubmissionFixture();try{
  f.backend.listTools=async()=>({tools:names.map(name=>({name,inputSchema:{type:'object'}}))});
  await assert.rejects(f.owner.startManual({}),/submission_runtime_required/);assert.equal(f.owner.status().state,'offline');
  assert.equal(f.calls.length,0);assert.equal(f.journal.list().length,0);
 }finally{await f.cleanup();}
});

test('journal recovery follows committed controller transfer without changing original intent ownership evidence',async()=>{
 const f=durableSubmissionFixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);f.calls.length=0;
  f.backend.callTool=async(request:any)=>{f.calls.push(request);return submissionReply(f,request);};
  await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'original'}});
  const original=f.journal.list()[0];initializeControllerAuthority(f.db);
  transferController(f.db,{transferId:'synthetic-journal-controller-transfer',taskId:'task',workUnitId:'unit',fromChat:'caller',toChat:'next',expectedEpoch:1,authorizationRef:'synthetic-approved-only'},'next',()=>true);
  f.observation.chat='next';const env={...f.env,CODEX_THREAD_ID:'next'};
  await f.owner.rebind(env,{generation:f.owner.generation,epoch:1});
  await assert.rejects(f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',recover_submission:true}}),/context_mismatch|authority_stale/);
  const b=await f.owner.attach(env);const recovered=await f.owner.callTool(b.id,{name:'terminal_exec',arguments:{task_id:'task',recover_submission:true}});
  assert.equal(recovered.structuredContent.receipts[0].command_id,original.commandId);assert.deepEqual(f.journal.list()[0],original);
  assert.equal(f.calls.filter(r=>r.arguments.command).length,1);assert.equal(f.owner.status().backendStarts,1);
 }finally{await f.cleanup();}
});

test('private original inputs survive client loss but are never returned in journal recovery output',async()=>{
 const f=durableSubmissionFixture();try{
  await f.owner.startManual({});const a=await f.owner.attach(f.env);
  f.backend.callTool=async(request:any)=>submissionReply(f,request);
  await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',work_unit_id:'unit',command:'never-expose-this-original-input',submission_id:null,wait_ms:0}});
  assert.equal(f.journal.list()[0].input?.command,'never-expose-this-original-input');
  const result=await f.owner.callTool(a.id,{name:'terminal_exec',arguments:{task_id:'task',recover_submission:true}});
  assert.equal(result.structuredContent.receipts[0].input_available,true);assert.equal(JSON.stringify(result).includes('never-expose-this-original-input'),false);
 }finally{await f.cleanup();}
});

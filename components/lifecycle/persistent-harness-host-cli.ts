import {spawn,execFileSync} from 'node:child_process';
import {openSync,closeSync,existsSync,readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname,join,resolve,basename} from 'node:path';
import {PersistentHarnessOwner,declaredBackendTransport} from './persistent-harness.ts';
import {loadPersistentHost,persistentRpc,servePersistentHarness} from './persistent-harness-ipc.ts';
import {verifyPersistentRuntime,persistentHostEnvironment} from './persistent-harness-pins.ts';
import {controllerScope} from './controller-authority.ts';
import {registerPersistentScope} from './persistent-harness-registration.ts';
import {PersistentOperatorPanel,servePersistentOperatorPanel} from './persistent-harness-panel.ts';
import {launchPersistentWindows} from './persistent-harness-launch.ts';
import {PersistentReconnect,persistentNetworkFingerprint} from './persistent-reconnect.ts';

const [action,path,scope]=process.argv.slice(2);
if(action==='--help')console.log('persistent-harness-host-cli <start|status|stop|reconnect|rebind|panel|host> <private-host.json> [rebind-scope-sha256] | auto-reconnect <private-host.json> <on|off> | register <private-host.json> <task> <work-unit> <declared-source> --apply; reconnect preserves owner and task IDs; automatic recovery is opt-in, bounded and latched on failure');
else{
 const c=loadPersistentHost(path);
 const hostEnv=persistentHostEnvironment(c,process.env);
 const here=dirname(fileURLToPath(import.meta.url));
 const guardScript=existsSync(join(here,'workplace_owner.py'))?join(here,'workplace_owner.py'):resolve(here,'../../plugins/solution-agent-harness/scripts/workplace_owner.py');
 const python=c.guardianPython??JSON.parse(readFileSync(c.installedBackendConfig,'utf8')).mcpServers?.solution_agent_harness?.command;
 if(typeof python!=='string'||!/^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$/i.test(basename(python)))throw Error('persistent_guardian_python_required');
 const cooldown=c.connectionCooldownSeconds??60;
 if(!Number.isFinite(cooldown)||cooldown<0||cooldown>86400)throw Error('persistent_invalid_connection_cooldown');
 const runtimeSha256=verifyPersistentRuntime(c,{hostEntry:fileURLToPath(import.meta.url),guardian:guardScript,node:process.execPath},hostEnv);
 const check=async()=>{const state=await persistentRpc(c.ipc,'/status');const expected=new PersistentHarnessOwner(c.owner).status().configurationSha256;if(state.configurationSha256!==expected||state.runtimeSha256!==runtimeSha256)throw Error('persistent_existing_configuration_mismatch');return state;};
 if(action==='panel'){
  await check();const panel=await new PersistentOperatorPanel(c,runtimeSha256,hostEnv).open();
  const service=await servePersistentOperatorPanel(panel);console.log(JSON.stringify({panelUrl:service.url,expiresInMinutes:30,receiver:'genuine launching chat; no ID input',humanButtonsOnly:true}));
  process.on('SIGINT',()=>{void service.close();});process.on('SIGTERM',()=>{void service.close();});
 }
 else if(action==='register'){
  if(!process.argv.includes('--apply'))throw Error('persistent_registration_explicit_apply_required');
  const [, ,taskId,workUnitId,source]=process.argv.slice(2);
  console.log(JSON.stringify(await registerPersistentScope(c.owner,hostEnv,{taskId,workUnitId,source})));
 }
 else if(action==='status')console.log(JSON.stringify(await check()));
 else if(action==='reconnect'||action==='auto-reconnect'){
  const state=await check(),expected={generation:state.generation,configurationSha256:state.configurationSha256,runtimeSha256:state.runtimeSha256};
  if(action==='auto-reconnect'&&!['on','off'].includes(scope))throw Error('persistent_auto_reconnect_on_off_required');
  console.log(JSON.stringify(await persistentRpc(c.ipc,action==='reconnect'?'/reconnect':'/reconnect-policy',{expected,...action==='auto-reconnect'?{enabled:scope==='on'}:{}},true)));
 }
 else if(action==='stop'){const state=await check();console.log(JSON.stringify(await persistentRpc(c.ipc,'/stop',{expected:{generation:state.generation,configurationSha256:state.configurationSha256,runtimeSha256:state.runtimeSha256}},true)));}
 else if(action==='rebind'){
  const state=await check();
  if(scope!==undefined&&!/^[a-f0-9]{64}$/.test(scope))throw Error('persistent_rebind_scope_rejected');
  const binding=scope?state.activeScopes?.find((b:any)=>controllerScope(b.taskId,b.workUnitId)===scope):state.activeBinding;
  if(!binding)throw Error('persistent_rebind_scope_rejected');
  console.log(JSON.stringify(await persistentRpc(c.ipc,'/rebind',{expected:{generation:state.generation,epoch:binding.epoch,...scope?{scope}:{}},context:{CODEX_THREAD_ID:process.env.CODEX_THREAD_ID,CODEX_APP_TOOLS_PIPE_PATH:process.env.CODEX_APP_TOOLS_PIPE_PATH}},true)));
 }
 else if(action==='start'){
  let existing;try{existing=await check();}catch(error:any){if(error.cause?.code!=='ECONNREFUSED')throw error;}
  if(existing)console.log(JSON.stringify({reused:true,...existing}));
  else{
   if(process.platform==='win32'){
    await launchPersistentWindows({python,guardian:guardScript,node:process.execPath,hostEntry:fileURLToPath(import.meta.url),hostPath:resolve(path),identity:c.owner.workplaceIdentityPath,cooldown},hostEnv);
   }else{
    const log=openSync(path+'.log','a');
    const child=spawn(python,[guardScript,'--identity',c.owner.workplaceIdentityPath,'--cooldown-seconds',String(cooldown),'--',process.execPath,...process.execArgv,fileURLToPath(import.meta.url),'host',path],{env:hostEnv,detached:true,stdio:['ignore',log,log]});closeSync(log);
    await new Promise<void>((yes,no)=>{child.once('spawn',yes);child.once('error',no);});child.unref();
   }
   const deadline=Date.now()+20000;let state:any;
   while(Date.now()<deadline){try{state=await check();if(state.state==='online')break;if(['offline','closed'].includes(state.state))throw Error('persistent_manual_start_failed');}catch(error:any){if(error.cause?.code!=='ECONNREFUSED')throw error;}await new Promise(r=>setTimeout(r,50));}
   if(state?.state!=='online')throw Error('persistent_manual_start_timeout_no_retry');console.log(JSON.stringify({startRequested:true,...state}));
  }
 }else if(action==='host'){
  const guard=JSON.parse(execFileSync(python,[guardScript,'--identity',c.owner.workplaceIdentityPath,'--verify-inherited'],{env:hostEnv,windowsHide:true,encoding:'utf8'}));
  if(guard.verified!==true)throw Error('persistent_guardian_required');
  const owner=new PersistentHarnessOwner(c.owner,{runtimeSha256});
  const policy=new PersistentReconnect(path+'.reconnect.json',Math.max(60,cooldown)*1000);
  const transport=()=>{
   // Revalidate sealed backend inputs before EVERY connection, including recovery.
   verifyPersistentRuntime(c,{hostEntry:fileURLToPath(import.meta.url),guardian:guardScript,node:process.execPath},hostEnv);
   const t=declaredBackendTransport(c.installedBackendConfig,hostEnv);t.stderr?.pipe(process.stderr);return t;
  };
  const reconnect=async()=>owner.reconnectManual(transport,async()=>{
   await new Promise<void>((yes,no)=>{
    const child=spawn(python,[guardScript,'--identity',c.owner.workplaceIdentityPath,'--verify-transport-idle'],{env:hostEnv,windowsHide:true,stdio:['ignore','pipe','pipe']});let output='';
    const timeout=setTimeout(()=>{child.kill();},10000);
    child.stdout.on('data',b=>{output+=b;});child.stderr.pipe(process.stderr);
    child.once('error',error=>{clearTimeout(timeout);no(error);});
    child.once('close',code=>{clearTimeout(timeout);try{if(code!==0||JSON.parse(output).transport_idle!==true)throw Error('persistent_previous_transport_exit_unconfirmed');yes();}catch(error){no(error);}});
   });
  });
  try{
   const service=await servePersistentHarness(owner,c.ipc,()=>{
    policy.noteStart();return owner.startManual(transport());
   },{policy,connect:reconnect});
   const observer=setInterval(()=>{void policy.observe(owner.status(),persistentNetworkFingerprint(),()=>owner.assertReconnectReady(),reconnect).catch(()=>{});},2000);observer.unref();
   const stop=()=>{clearInterval(observer);void service.stop().catch(()=>{});};
   process.on('SIGINT',stop);process.on('SIGTERM',stop);
  }catch(error:any){if(error.code!=='EADDRINUSE')throw error;}
 }else throw Error('expected_manual_start_status_stop_rebind_or_host');
}

import {readFileSync,mkdirSync,writeFileSync,renameSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {isAbsolute,resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {validateSessionStart,runSessionLifecycle} from './session-lifecycle.ts';
import {probeSessionContext} from './session-context-probe.ts';

if(process.argv.includes('--help'))console.log('session-lifecycle-cli <absolute config.json> [--probe]; SessionStart JSON on stdin, genuine App context inherited; no configuration installation');
else{
 const path=process.argv[2];if(!path||!isAbsolute(path))throw Error('absolute_lifecycle_config_required');
 const config=JSON.parse(readFileSync(path,'utf8'));
 const child=process.argv.includes('--child');let event:any;
 if(child)event={hook_event_name:'SessionStart',source:'resume',session_id:process.env.CODEX_THREAD_ID,cwd:config.workspace};
 else{
  let input='';for await(const data of process.stdin){input+=data;if(Buffer.byteLength(input)>16384)throw Error('hook_input_too_large');}
  event=JSON.parse(input);
 }
 if(process.argv.includes('--probe-app')){
  const result=await probeSessionContext(config,event,process.env);console.log(JSON.stringify({systemMessage:'Harness SessionStart read-only probe: '+result.state,state:result.state,pass:result.pass}));
 }else{
 const admission=validateSessionStart(config,event,process.env);
 if(admission.state!=='ready'||process.argv.includes('--probe')){
  console.log(JSON.stringify({systemMessage:'Harness lifecycle: '+admission.state,state:admission.state,reason:admission.reason??'context_fields_present',scope:'validation only; no App handshake, manager or lifecycle proof'}));
 }else if(child){
  const controller=new AbortController(),stop=()=>controller.abort();process.on('SIGINT',stop);process.on('SIGTERM',stop);
  const root=resolve(config.manager.stateRoot);mkdirSync(root,{recursive:true});
  const id=createHash('sha256').update(admission.caller!).digest('hex').slice(0,16),path=resolve(root,'lifecycle-'+id+'.json');
  const record=(value:any)=>{const temp=path+'.'+process.pid+'.tmp';writeFileSync(temp,JSON.stringify({...value,pid:process.pid,at:new Date().toISOString()}));renameSync(temp,path);};
  try{record({state:'waiting_or_bootstrapping'});record(await runSessionLifecycle(config,event,process.env,controller.signal));}
  catch{record({state:'requires_attention',reason:'lifecycle_child_failed'});process.exitCode=1;}
  finally{process.off('SIGINT',stop);process.off('SIGTERM',stop);}
 }else{
  // Independent OS child, with hidden Windows window and no shell interpolation.
  // Detached command exits quickly; bounded SQL owner wait and App-health exit
  // policy govern the child. Nothing is installed or registered by this script.
  const worker=spawn(process.execPath,[...process.execArgv,fileURLToPath(import.meta.url),path,'--child'],{env:admission.env,detached:true,windowsHide:true,stdio:'ignore'});
  await new Promise<void>((yes,no)=>{worker.once('spawn',yes);worker.once('error',no);});worker.unref();
  console.log(JSON.stringify({systemMessage:'Harness lifecycle child launched; readiness requires manager status',state:'candidate_started',scope:'child launch only; inspect manager status for verified App readiness'}));
 }
 }
}

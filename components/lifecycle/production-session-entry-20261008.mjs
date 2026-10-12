import {readFileSync,openSync} from 'node:fs';
import {spawn} from 'node:child_process';
const config=JSON.parse(readFileSync(process.argv[2],'utf8'));
let input='';for await(const chunk of process.stdin){input+=chunk;if(Buffer.byteLength(input)>16384)throw Error('hook_input_too_large');}
const event=JSON.parse(input);
// Admission happens before starting the service or reading its credential.
if(event.hook_event_name!=='SessionStart'||!['startup','resume'].includes(event.source))process.exit(0);
if(process.env.CODEX_THREAD_ID&&process.env.CODEX_THREAD_ID!==event.session_id)throw Error('lifecycle_caller_mismatch');
if(!process.env.CODEX_APP_TOOLS_PIPE_PATH){console.log(JSON.stringify({systemMessage:'Harness lifecycle awaits genuine App context.',state:'awaiting_app'}));process.exit(0);}
const probe=spawn(process.execPath,[config.lifecycleEntry,config.lifecycleConfig,'--probe'],{
 env:{...process.env,HARNESS_XATS_TOKEN:'validation-only-presence'},windowsHide:true,stdio:['pipe','pipe','inherit']});
let probeText='';probe.stdout.on('data',data=>{probeText+=data;});probe.stdin.end(input);
await new Promise((yes,no)=>{probe.once('error',no);probe.once('exit',code=>code===0?yes():no(Error('lifecycle_admission_failed')));});
if(JSON.parse(probeText).state!=='ready'){process.stdout.write(probeText);process.exit(0);}
const token=readFileSync(config.service.tokenFile,'utf8').trim();
const url=new URL(config.manager.url);
const healthy=async()=>{try{const result=await fetch(new URL('/health',url),{signal:AbortSignal.timeout(500)});return result.ok;}catch{return false;}};
if(!await healthy()){
 const log=openSync(config.service.logPath,'a');
 const service=spawn(process.execPath,[config.service.entry,config.service.configPath],{windowsHide:true,detached:true,stdio:['ignore',log,log]});
 await new Promise((yes,no)=>{service.once('spawn',yes);service.once('error',no);});service.unref();
 for(let n=0;n<20&&!await healthy();n++)await new Promise(r=>setTimeout(r,100));
 if(!await healthy())throw Error('harness_inbox_service_unavailable');
}
const child=spawn(process.execPath,[config.lifecycleEntry,config.lifecycleConfig],{
 env:{...process.env,HARNESS_XATS_TOKEN:token},windowsHide:true,stdio:['pipe','inherit','inherit']});
child.stdin.end(input);await new Promise((yes,no)=>{child.once('error',no);child.once('exit',code=>code===0?yes():no(Error('lifecycle_entry_failed')));});

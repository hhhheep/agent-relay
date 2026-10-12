import {spawn} from 'node:child_process';
import {mkdirSync,writeFileSync,renameSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {connectStableInbox} from './stable-inbox.ts';

export function mailboxRpc(python:string,script:string,request:any):Promise<any>{
 return new Promise((ok,fail)=>{
  const process=spawn(python,[script],{stdio:['pipe','pipe','pipe'],windowsHide:true});
  const chunks:Buffer[]=[];let bytes=0,failed=false;
  const stop=(reason:string)=>{if(failed)return;failed=true;process.kill();fail(Error(reason));};
  const timer=setTimeout(()=>stop('mailbox_operation_timeout'),20000);
  process.on('error',()=>{clearTimeout(timer);stop('mailbox_process_error');});
  process.stdout.on('data',(chunk:Buffer)=>{bytes+=chunk.length;if(bytes>12*1024*1024)stop('mailbox_output_limit');else chunks.push(chunk);});
  process.stderr.resume(); // Do not expose command/environment or remote content.
  process.stdin.on('error',()=>stop('mailbox_input_error'));
  process.on('close',code=>{clearTimeout(timer);if(failed)return;if(code!==0){fail(Error('mailbox_operation_failed'));return;}try{ok(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch{fail(Error('invalid_mailbox_receipt'));}});
  process.stdin.end(JSON.stringify(request));
 });
}

export type MailboxConfig={stateRoot:string,url:string,endpointIdentity?:string,runtimeRoot?:string,name:string,team:string,managerSource:string,python:string,rpcScript:string,mailbox:string,binding:Record<string,string>};
export async function runMailbox(config:MailboxConfig,env:NodeJS.ProcessEnv,signal:AbortSignal,once=false){
 if([config.stateRoot,config.python,config.rpcScript,config.mailbox].some(p=>!isAbsolute(p))||!config.managerSource||!env.HARNESS_XATS_TOKEN)throw Error('invalid_mailbox_driver_config');
 mkdirSync(config.stateRoot,{recursive:true});let inbox:any,cursor=0,failures=0,lastErrorCode:string|undefined;
 const status=(state:string,error?:unknown)=>{
  const code=error instanceof Error?error.message.split(':')[0]:'mailbox_unknown_error';
  if(error)lastErrorCode=/^[a-zA-Z0-9_]+$/.test(code)?code:'mailbox_operation_failed';
  const path=resolve(config.stateRoot,'status.json'),temp=path+'.tmp';
  writeFileSync(temp,JSON.stringify({state,binding:config.binding,agentId:inbox?.agentId??null,cursor,failures,
   ...(lastErrorCode?{errorCode:lastErrorCode}:{}),at:new Date().toISOString()}));renameSync(temp,path);
 };
 try{
  do{
   if(signal.aborted)break;
   try{
    inbox??=await connectStableInbox(resolve(config.stateRoot,'identity.sqlite'),{url:config.url,endpointIdentity:config.endpointIdentity,token:env.HARNESS_XATS_TOKEN,name:config.name,team:config.team});
    // Explicit cursor is read-only on XATS; authoritative cursor commits in SQLite.
    const page=await inbox.get(cursor);
    const result=await mailboxRpc(config.python,config.rpcScript,{mailbox:config.mailbox,binding:config.binding,manager_source:config.managerSource,messages:page.messages,...(config.runtimeRoot?{runtime_root:config.runtimeRoot}:{})});
    cursor=result.cursor;
    for(const item of result.outgoing){if(item.to!==config.managerSource)throw Error('mailbox_destination_mismatch');await inbox.send(item.to,item.body);}
    failures=0;
    lastErrorCode=undefined;
    status('connected');
   }catch(error){failures++;status('retrying',error);try{await inbox?.close();}catch{}inbox=undefined;if(once)throw error;}
   if(once)break;
   try{await delay(failures?Math.min(60000,1000*2**Math.min(failures,6)):2000,undefined,{signal});}catch(error){if(!signal.aborted)throw error;}
  }while(!signal.aborted);
 }finally{try{await inbox?.close();}finally{status('stopped');}}
}

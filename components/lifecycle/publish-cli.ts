import {readFileSync,mkdirSync} from 'node:fs';import {resolve,isAbsolute} from 'node:path';
import {NativeAppPort} from './native-app-port.ts';import {HarnessPort} from './harness-port.ts';
import {TaskPublisher} from './task-publisher.ts';import {ReviewedStore} from './reviewed-store.ts';
import {PublisherRoutes} from './publisher-route.ts';import {publishTask} from './publication-service.ts';
if(process.argv.includes('--help'))console.log('publish-cli <config.json> <submission.json> --execute [--resume <publication-id>] [--recover-rejected | --recover-unstarted]; requires installed candidate release, genuine App context, and a declared XATS source. No installation or service startup.');
else{
 if(!process.argv.includes('--execute'))throw Error('explicit_execute_required');
 const config=JSON.parse(readFileSync(process.argv[2],'utf8')),input=JSON.parse(readFileSync(process.argv[3],'utf8'));
 if(!isAbsolute(config.stateRoot)||!isAbsolute(config.appServerPath)||!isAbsolute(config.installedConfig)||!config.expectedReleaseId)throw Error('declared_candidate_config_required');
 mkdirSync(config.stateRoot,{recursive:true});
 const app=new NativeAppPort(),harness=new HarnessPort(new Set([input.task_id]),undefined,config.expectedReleaseId);
 const journal=new TaskPublisher(resolve(config.stateRoot,'publications.sqlite')),store=new ReviewedStore(resolve(config.stateRoot,'queue.sqlite'));
 try{
  await app.connect(config.appServerPath,process.env);await harness.connect(config.installedConfig,process.env);
  const markdownPlan=config.markdownPlan?JSON.parse(readFileSync(config.markdownPlan,'utf8')):undefined;
  const existingId=process.argv.includes('--resume')?process.argv[process.argv.indexOf('--resume')+1]:undefined;
  if(existingId){const binding=JSON.parse(journal.inspect(existingId).body).binding;if(!config.authorizedOriginChats?.includes(binding.origin_chat_id))throw Error('origin_chat_management_not_authorized');}
  if(process.argv.includes('--recover-unstarted')&&config.legacyAuthorityRoot!==config.workerProfile.runtimeRoot)throw Error('legacy_original_runtime_authority_required');
  if(process.argv.includes('--recover-rejected')&&process.argv.includes('--recover-unstarted'))throw Error('choose_one_recovery');
  const row=await publishTask(journal,new PublisherRoutes(store),input,app,config.workerProfile,harness,{markdownPlan,existingId,recoverRejected:process.argv.includes('--recover-rejected'),recoverUnstarted:process.argv.includes('--recover-unstarted'),deploymentProfile:config.deploymentProfile});
  console.log(JSON.stringify({id:row.id,state:row.state,command_id:row.command_id}));
 }finally{journal.close();store.close();await app.close();await harness.close();}
}

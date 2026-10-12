import {readFileSync} from 'node:fs';
import {runMailbox} from './mailbox-driver.ts';
if(process.argv.includes('--help'))console.log('mailbox-cli <config.json> [--once] [--ipc]; token comes from environment; --ipc accepts stop from its spawning supervisor');
else{
 const path=process.argv[2];if(!path)throw Error('config path required');
 const controller=new AbortController(),stop=()=>controller.abort();process.on('SIGINT',stop);process.on('SIGTERM',stop);
 const ipc=process.argv.includes('--ipc'),onMessage=(message:any)=>{if(message?.type==='stop')stop();};
 if(ipc){if(!process.send)throw Error('mailbox_ipc_supervisor_required');process.on('message',onMessage);process.on('disconnect',stop);}
 try{await runMailbox(JSON.parse(readFileSync(path,'utf8')),process.env,controller.signal,process.argv.includes('--once'));}
 finally{process.off('SIGINT',stop);process.off('SIGTERM',stop);if(ipc){process.off('message',onMessage);process.off('disconnect',stop);if(process.connected)process.send?.({type:'stopped'});process.disconnect?.();}}
}

import {readFileSync} from 'node:fs';
import {runManager} from './manager-driver.ts';
if(process.argv.includes('--help'))console.log('manager-cli <config.json> [--once] [--ipc]; credentials and App context come from environment; --ipc accepts stop from its spawning supervisor');
else{
 const path=process.argv[2];if(!path)throw Error('config path required');
 const controller=new AbortController(),stop=()=>controller.abort();process.on('SIGINT',stop);process.on('SIGTERM',stop);
 const ipc=process.argv.includes('--ipc'),onMessage=(message:any)=>{if(message?.type==='stop')stop();};
 if(ipc){if(!process.send)throw Error('manager_ipc_supervisor_required');process.on('message',onMessage);process.on('disconnect',stop);}
 try{await runManager(JSON.parse(readFileSync(path,'utf8')),process.env,controller.signal,{once:process.argv.includes('--once'),onStatus:ipc?s=>{if(process.connected)process.send?.({type:'status',status:s});}:undefined});}
 finally{process.off('SIGINT',stop);process.off('SIGTERM',stop);if(ipc){process.off('message',onMessage);process.off('disconnect',stop);process.disconnect?.();}}
}

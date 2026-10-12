// Isolated crash test only. Never used by the background service.
import { Engine, readConfig, acquireLock } from './service.mjs';
const config=readConfig(process.argv[2]);
const release=acquireLock(config.state);
const engine=new Engine(config,{now:()=>Date.now()+60000,generate:async({receipt,atomic})=>{
  atomic(receipt,JSON.stringify({status:'requesting',usage:null,finish_reason:null}));
  console.log('PROCESSING');await new Promise(resolve=>setTimeout(resolve,100000));
}});
try{await engine.runBatch();}finally{engine.close();release();}

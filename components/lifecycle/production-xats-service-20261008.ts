import {readFileSync,writeFileSync} from 'node:fs';
import {startServer} from '../xats-routing-lab/upstream/src/daemon/server.ts';
const config=JSON.parse(readFileSync(process.argv[2],'utf8'));
const token=readFileSync(config.tokenFile,'utf8').trim();
const service=await startServer({dbPath:config.dbPath,host:'127.0.0.1',port:config.port,token});
writeFileSync(config.receiptPath,JSON.stringify({pid:process.pid,port:service.port,startedAt:new Date().toISOString(),scope:'local Harness inbox; original database retained'}));
let closing=false;
const close=async()=>{if(closing)return;closing=true;await service.app.close();};
process.on('SIGTERM',close);process.on('SIGINT',close);

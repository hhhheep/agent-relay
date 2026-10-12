import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Engine, readConfig, acquireLock, atomic, safeError } from './service.mjs';

const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  if (!args[index+1]) throw new Error('MISSING_OPTION_VALUE');
  const value = args[index+1]; args.splice(index,2); return value;
};
const configFile = option('--config',fileURLToPath(new URL('./config.json',import.meta.url)));
const command = args.shift() ?? 'status';
let engine, release;
try {
  const c = readConfig(configFile);
  if (!['status','find','stop'].includes(command)) release = acquireLock(c.state);
  if (command === 'stop') {
    atomic(path.join(c.state,'STOP'),'Stop requested\n');
    console.log('Stop requested; active request is aborted and retained for startup recovery.');
  } else {
    engine = new Engine(c);
    if (command === 'baseline') console.log(JSON.stringify(engine.meta('baseline_at') ?
      {already_baselined:true,baselined:engine.status().baseline_files,api_calls:0} : engine.baseline()));
    else if (command === 'status') {
      const runtime = path.join(c.state,'runtime.json');
      console.log(JSON.stringify({...engine.status(),runtime:fs.existsSync(runtime)?JSON.parse(fs.readFileSync(runtime)):null},null,2));
    } else if (command === 'find') {
      const query = args.join(' ').toLocaleLowerCase();
      if (!query) throw new Error('QUERY_REQUIRED');
      const found=[];
      for (const job of engine.db.prepare("SELECT * FROM jobs WHERE status='success' ORDER BY updated DESC").all()) {
        const file = path.join(engine.safe(job.output),'summary.md');
        if (job.source.toLocaleLowerCase().includes(query) || fs.readFileSync(file,'utf8').toLocaleLowerCase().includes(query)) {
          found.push({source:engine.safe(job.source),sha256:job.sha,summary:file});
          if (found.length>=50) break;
        }
      }
      console.log(JSON.stringify(found,null,2));
    } else if (command === 'backfill') {
      const limit = Number(option('--limit',0));
      console.log(JSON.stringify(engine.backfill(args,limit)));
    } else if (command === 'once') {
      engine.recover(); engine.scan(); await engine.runBatch(); console.log(JSON.stringify(engine.status(),null,2));
    } else if (command === 'run') {
      const controller = new AbortController();
      const stopFile = path.join(c.state,'STOP'), runtime = path.join(c.state,'runtime.json');
      const report = state => atomic(runtime,JSON.stringify({state,pid:process.pid,
        updated_at:new Date().toISOString(),poll_seconds:c.poll_seconds},null,2));
      process.on('SIGINT',()=>controller.abort()); process.on('SIGTERM',()=>controller.abort());
      const stopCheck = setInterval(()=>{if(fs.existsSync(stopFile)) controller.abort();},1000);
      const wait = ms => new Promise(resolve=>{
        if (controller.signal.aborted) return resolve();
        const finish=()=>{clearTimeout(timer);controller.signal.removeEventListener('abort',finish);resolve();};
        const timer=setTimeout(finish,ms);controller.signal.addEventListener('abort',finish,{once:true});
      });
      try {
        if (fs.existsSync(stopFile)) controller.abort();
        if (!controller.signal.aborted) engine.recover();
        while (!controller.signal.aborted) {
          report('running');
          try { engine.scan(); await engine.runBatch(controller.signal); }
          catch (e) {
            // Persist only a bounded error code; continue independent polling.
            atomic(path.join(c.state,'attention.json'),JSON.stringify({code:safeError(e),at:new Date().toISOString()}));
          }
          await wait(c.poll_seconds*1000);
        }
      } finally { clearInterval(stopCheck); report('stopped'); }
    } else throw new Error('UNKNOWN_COMMAND');
  }
} catch (error) {
  console.error(safeError(error)); process.exitCode = error.message==='ALREADY_RUNNING'?2:1;
} finally { engine?.close(); release?.(); }

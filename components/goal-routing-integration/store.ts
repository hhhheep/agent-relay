import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {parseCodexThreadGoal} from './vendor/paperclip-goal.ts';
export const digest=(value:string)=>createHash('sha256').update(value).digest('hex');
export class Store {
  db:DatabaseSync;
  constructor(path:string){
    this.db=new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS routes(source TEXT PRIMARY KEY,thread TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,source TEXT NOT NULL,thread TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',objective TEXT,result TEXT,sent TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,source TEXT NOT NULL,state TEXT NOT NULL,failures INTEGER NOT NULL DEFAULT 0,proposal TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS rejected(event_id INTEGER PRIMARY KEY,reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS goal_completions(job_id TEXT PRIMARY KEY,thread TEXT NOT NULL,objective TEXT NOT NULL,receipt TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_thread ON events(thread) WHERE state='running';`);
    try{this.tx(()=>{
      const owner=this.db.prepare("SELECT value FROM meta WHERE key='owner'").get();
      if(owner){let alive=true;try{process.kill(Number(owner.value),0);}catch(e:any){if(e.code==='ESRCH')alive=false;}
        if(alive)throw Error('controller_owner_alive');}
      this.db.prepare("INSERT INTO meta VALUES ('owner',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(String(process.pid));
    });}catch(e){this.db.close();throw e;}
  }
  tx<T>(fn:()=>T):T{this.db.exec('BEGIN IMMEDIATE');try{const r=fn();this.db.exec('COMMIT');return r;}catch(e){this.db.exec('ROLLBACK');throw e;}}
  bind(source:string,thread:string){
    const old=this.db.prepare('SELECT thread FROM routes WHERE source=?').get(source);
    if(old&&old.thread!==thread)throw Error('route_rebind_requires_migration');
    this.db.prepare('INSERT OR IGNORE INTO routes VALUES (?,?)').run(source,thread);
  }
  cursor(){return Number(this.db.prepare("SELECT value FROM meta WHERE key='cursor'").get()?.value??0);}
  observeGoal(params:any){
    const goal=parseCodexThreadGoal(params?.goal);
    if(!goal||goal.status!=='complete')return false;
    if(params.threadId!==goal.threadId||typeof params.turnId!=='string'||!params.turnId)throw Error('invalid_completion_event');
    const job=this.db.prepare("SELECT id FROM events WHERE thread=? AND objective=? AND state='running'").get(goal.threadId,goal.objective);
    if(!job)return false;
    const receipt=JSON.stringify({goal,turnId:params.turnId});
    const old=this.db.prepare('SELECT receipt FROM goal_completions WHERE job_id=?').get(job.id);
    if(old&&old.receipt!==receipt)throw Error('conflicting_completion_event');
    this.db.prepare('INSERT OR IGNORE INTO goal_completions VALUES (?,?,?,?)').run(job.id,goal.threadId,goal.objective,receipt);
    return true;
  }
  completion(thread:string,objective:string){
    const row=this.db.prepare('SELECT receipt FROM goal_completions WHERE thread=? AND objective=?').get(thread,objective);
    return row?JSON.parse(String(row.receipt)):null;
  }
  ingest(messages:any[],lastEvent:number){return this.tx(()=>{
    for(const m of messages){
      let body:any;
      try{
        if(typeof m.body!=='string'||m.body.length>2400)throw Error('invalid_envelope');
        body=JSON.parse(m.body);
        const route=this.db.prepare('SELECT thread FROM routes WHERE source=?').get(m.from_agent_id);
        if(!route)throw Error('unbound_source');
        if(!body||typeof body!=='object'||Array.isArray(body)||body.v!==1||typeof body.id!=='string'||!body.id.length||body.id.length>128||typeof body.incident!=='string'||!body.incident.length||body.incident.length>128||!['request','verification'].includes(body.kind)||!body.payload||typeof body.payload!=='object'||Array.isArray(body.payload))throw Error('invalid_envelope');
        const id=digest(m.from_agent_id+'\0'+body.id), old=this.db.prepare('SELECT source,body FROM events WHERE id=?').get(id);
        if(old){if(old.body!==m.body)throw Error('idempotency_conflict');continue;}
        const pending=this.db.prepare("SELECT count(*) n FROM events WHERE state NOT IN ('done','human')").get()!.n as number;
        if(pending>=128)throw Error('capacity_retry');
        this.db.prepare('INSERT INTO events(id,source,thread,body) VALUES (?,?,?,?)').run(id,m.from_agent_id,route.thread,m.body);
      }catch(e){
        if(!(e instanceof SyntaxError)&&!['unbound_source','invalid_envelope','idempotency_conflict'].some(reason=>String(e)==='Error: '+reason))throw e;
        this.db.prepare('INSERT OR IGNORE INTO rejected VALUES (?,?)').run(m.event_id,String(e));
      }
    }
    this.db.prepare("INSERT INTO meta VALUES ('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(String(lastEvent));
  });}
  next(thread:string):any{return this.db.prepare("SELECT * FROM events WHERE thread=? AND state IN ('queued','running','reply_ready') ORDER BY seq LIMIT 1").get(thread);}
  mark(id:string,state:string,objective?:string){this.db.prepare('UPDATE events SET state=?,objective=coalesce(?,objective) WHERE id=?').run(state,objective??null,id);}
  validatePhase(job:any){
    const e=JSON.parse(job.body),old=this.db.prepare('SELECT * FROM incidents WHERE id=?').get(digest(job.source+'\0'+e.incident)) as any;
    if(e.kind==='verification' && (typeof e.payload.pass!=='boolean'||!old||old.state!=='awaiting_verification'||e.payload.proposal_id!==old.proposal))throw Error('unexpected_verification');
    if(e.kind==='request' && old && old.state!=='retry_allowed')throw Error('incident_not_ready_for_request');
  }
  human(job:any,reason:string){const e=JSON.parse(job.body);this.db.prepare("UPDATE events SET state='reply_ready',result=? WHERE id=?").run(JSON.stringify({v:1,delivery_id:job.id,request_id:e.id,incident:e.incident,kind:'human_required',reason}),job.id);}
  ready(job:any,evidence:any,onSaved:()=>void=()=>{}){this.tx(()=>{
    const envelope=JSON.parse(job.body), incident=digest(job.source+'\0'+envelope.incident);
    const old=this.db.prepare('SELECT * FROM incidents WHERE id=?').get(incident) as any;
    let state='awaiting_verification',failures=Number(old?.failures??0);
    if(envelope.kind==='verification'){
      // This is lifecycle evidence, not independent proof of a research result.
      if(typeof envelope.payload.pass!=='boolean'||!old||old.state!=='awaiting_verification'||envelope.payload.proposal_id!==old.proposal)throw Error('unexpected_verification');
      failures=envelope.payload.pass?0:failures+1;
      state=envelope.payload.pass?'closed':failures>=2?'human_required':'retry_allowed';
    }else if(old&& !['retry_allowed'].includes(old.state))throw Error('incident_not_ready_for_request');
    const result={v:1,delivery_id:job.id,request_id:envelope.id,incident:envelope.incident,kind:state,evidence};
    this.db.prepare('INSERT INTO incidents VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,failures=excluded.failures,proposal=excluded.proposal').run(incident,job.source,state,failures,envelope.kind==='request'?envelope.id:old.proposal);
    this.db.prepare("UPDATE events SET state='reply_ready',result=? WHERE id=?").run(JSON.stringify(result),job.id);
    onSaved();
  });}
  rows(){return this.db.prepare('SELECT * FROM events ORDER BY seq').all() as any[];}
  close(){this.db.prepare("DELETE FROM meta WHERE key='owner' AND value=?").run(String(process.pid));this.db.close();}
}

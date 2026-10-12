import {PublisherRoutes} from './publisher-route.ts';
import {Store} from '../goal-routing-integration/store.ts';

export class InboxPump {
 constructor(readonly store:Store,readonly routes:PublisherRoutes,readonly inbox:any,readonly fault:(stage:string)=>void=()=>{}){
  store.db.exec(`CREATE TABLE IF NOT EXISTS help_ack_outbox (
    id TEXT PRIMARY KEY,source TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS help_rejections(event_id INTEGER PRIMARY KEY,reason TEXT NOT NULL);`);
 }
 cursor(){return Number(this.store.db.prepare("SELECT value FROM meta WHERE key='help_cursor'").get()?.value??0);}
 saveCursor(cursor:number){this.store.db.prepare("INSERT INTO meta VALUES ('help_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(String(cursor));}
 async ingest(){
  const box=await this.inbox.get(this.cursor());let count=0;
  for(const message of [...box.messages].sort((a,b)=>a.event_id-b.event_id)){
   if(message.event_id<=this.cursor())continue;
   let ack:any;
   try{
    if(typeof message.body!=='string'||message.body.length>18000)throw Error('invalid_help_envelope');
    ack=this.routes.ingest(message.from_agent_id,JSON.parse(message.body));
   }catch(error){
    if(String(error)==='Error: capacity_retry')return {count,capacity:true};
    const allowed=['unverified_saved_answer','verification_answer_mismatch','invalid_help_envelope','untrusted_help_source','delivery_binding_mismatch','invalid_help_payload','missing_question_fields','invalid_verification','unbound_verification_proposal','help_payload_requires_artifact_reference','help_content_conflict'];
    if(!(error instanceof SyntaxError)&&!allowed.some(reason=>String(error)==='Error: '+reason))throw error;
    this.store.tx(()=>{this.store.db.prepare('INSERT OR IGNORE INTO help_rejections VALUES (?,?)').run(message.event_id,String(error));this.saveCursor(message.event_id);});
    continue;
   }
   // If interrupted here, durable ingress deduplicates replay; cursor is not advanced.
   this.fault('after_ingress');
   this.store.tx(()=>{
    const id=message.from_agent_id+':'+(ack.kind??'help_accepted')+':'+ack.delivery_id;
    const body=JSON.stringify({kind:'help_accepted',...ack});
    this.store.db.prepare("INSERT INTO help_ack_outbox(id,source,body) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state='pending'").run(id,message.from_agent_id,body);
    this.saveCursor(message.event_id);
   });count++;
  }
  if(Number.isSafeInteger(box.last_event_id)&&box.last_event_id>this.cursor())this.saveCursor(box.last_event_id);
  return {count,capacity:false};
 }
 async flush(){
  const pending=this.store.db.prepare("SELECT * FROM help_ack_outbox WHERE state='pending'").all() as any[];
  let failure:unknown;
  for(const row of pending){
   try{
   await this.inbox.send(row.source,row.body);
   this.fault('after_ack_send');
   this.store.db.prepare("UPDATE help_ack_outbox SET state='sent' WHERE id=?").run(row.id);
   }catch(error){failure??=error;}
  }
  if(failure)throw failure;
  return pending.length;
 }
}

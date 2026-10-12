import {Store,digest} from '../goal-routing-integration/store.ts';
import {ResumeController} from './resume-controller.ts';
import {remoteDelivery} from './publisher-route.ts';

export class ResumeScheduler {
 constructor(readonly store:Store,readonly controller:ResumeController,readonly authorize:(scope:string)=>()=>void=()=>()=>{}){
  store.db.exec('CREATE TABLE IF NOT EXISTS help_resume_context(scope TEXT PRIMARY KEY,body TEXT NOT NULL);');
 }
 register(binding:any,context:any){
  const scope=digest(JSON.stringify([binding.task_id,binding.work_unit_id]));
  const route=this.store.db.prepare('SELECT binding FROM publisher_routes WHERE scope=?').get(scope) as any;
  if(!route||Object.entries(JSON.parse(route.binding)).some(([k,v])=>binding[k]!==v)||Object.keys(binding).length!==4)throw Error('resume_context_binding_mismatch');
  const body=JSON.stringify(context),old=this.store.db.prepare('SELECT body FROM help_resume_context WHERE scope=?').get(scope) as any;
  if(old&&old.body!==body)throw Error('resume_context_rebind_rejected');
  this.store.db.prepare('INSERT OR IGNORE INTO help_resume_context VALUES (?,?)').run(scope,body);
 }
 async tick(){
  const rows=this.store.db.prepare(`SELECT h.delivery,h.body,e.result,a.request_id,a.answer_sha256,r.binding,c.body context
   FROM help_remote_answers a JOIN help_ingress h ON a.delivery=h.delivery
   JOIN events e ON e.id=h.job JOIN publisher_routes r ON r.scope=h.scope
   JOIN help_resume_context c ON c.scope=h.scope`).all() as any[];
  const results=[];
  for(const row of rows){
   try{
   if(digest(row.result)!==row.answer_sha256)throw Error('saved_answer_changed');
   const input=JSON.parse(row.body),answer=JSON.parse(row.result);
   if(input.payload.kind!=='question'||answer.kind!=='awaiting_verification')continue;
   const binding=JSON.parse(row.binding),context=JSON.parse(row.context);
   const guard=this.authorize(digest(JSON.stringify([binding.task_id,binding.work_unit_id])));guard();
   const question={request_id:row.request_id,delivery_id:row.delivery,state:'answered',body:JSON.stringify(input.payload),answer:row.result};
   const {id}=await this.controller.plan(binding,question,context);
   const parent=await this.controller.tick(id,guard);results.push(parent);
   if(parent.state!=='finished')continue;
   const reviewRow=rows.find(r=>r.delivery===remoteDelivery(binding,'verify-'+id.slice(0,24)));
   if(!reviewRow)continue; // The remote must persist the review before another worker starts.
   if(digest(reviewRow.result)!==reviewRow.answer_sha256)throw Error('saved_review_changed');
   const review=JSON.parse(reviewRow.result),verification=JSON.parse(reviewRow.body);
   if(review.kind!=='retry_allowed')continue;
   if(verification.payload.proposal_id!==row.delivery||verification.payload.answer_sha256!==row.answer_sha256)throw Error('retry_proposal_mismatch');
   guard();const retry=await this.controller.plan(binding,question,{...context,previous_command_id:parent.command_id,parent_id:id,
    retry_review:{answer:review,previous_verification:verification.payload}});
   results.push(await this.controller.tick(retry.id,guard));
   }catch(error){
    const code=error instanceof Error?error.message.split(':')[0]:'resume_failed';
    results.push({delivery:row.delivery,state:'requires_attention',errorCode:/^[a-zA-Z0-9_]+$/.test(code)?code:'resume_failed'});
   }
  }
  return results;
 }
}

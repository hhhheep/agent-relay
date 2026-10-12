import {Store,digest} from '../goal-routing-integration/store.ts';

// Integration policy: a remote pass is evidence to review, not closure authority.
// Preserve the immutable input; record local judgment explicitly in result evidence.
export class ReviewedStore extends Store {
 override validatePhase(job:any){
  const input=JSON.parse(job.body),old=this.db.prepare('SELECT * FROM incidents WHERE id=?').get(digest(job.source+'\0'+input.incident)) as any;
  if(input.kind==='verification'&&old?.state==='retry_allowed'&&input.payload.proposal_id===old.proposal&&typeof input.payload.pass==='boolean')return;
  super.validatePhase(job);
 }
 override ready(job:any,evidence:any,onSaved:()=>void=()=>{}){
  const input=JSON.parse(job.body);
  let decision:any;
  try{decision=JSON.parse(evidence.text);}catch{throw Error('structured_manager_decision_required');}
  if(!decision||typeof decision.summary!=='string'||!decision.summary.trim())throw Error('manager_summary_required');
  const valid=input.kind==='verification'?['accept','reject','human_required']:['propose','human_required'];
  if(!valid.includes(decision.decision))throw Error('invalid_manager_decision');
  if(decision.decision==='propose'&&!Object.hasOwn(decision,'proposal'))throw Error('manager_proposal_required');
  if(job.objective?.startsWith('Harness help problem ')){
   const resolved=input.kind==='verification'&&input.payload.pass===true&&decision.decision==='accept';
   // A verified controller handoff can record interim review without taking
   // over an unavailable old chat's Goal. It cannot claim problem completion.
   const interimTransferredReview=evidence.controllerReview===true&&!resolved;
   if(!interimTransferredReview&&(resolved?evidence.goal?.status!=='complete':evidence.goal?.status!=='active'))throw Error('problem_goal_closed_before_resolution');
  }
  const reviewed={...evidence,managerDecision:decision};
  this.tx(()=>{
   this.validatePhase(job);
   const incident=digest(job.source+'\0'+input.incident),old=this.db.prepare('SELECT * FROM incidents WHERE id=?').get(incident) as any;
   let state='awaiting_verification',failures=Number(old?.failures??0);
   if(input.kind==='verification'){
    const accepted=input.payload.pass===true&&decision.decision==='accept';
    if(!accepted)failures++;
    state=accepted?'closed':failures>=2?'human_required':'retry_allowed';
   }
   if(decision.decision==='human_required')state='human_required';
   this.db.prepare('INSERT INTO incidents VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,failures=excluded.failures,proposal=excluded.proposal')
    .run(incident,job.source,state,failures,input.kind==='request'?input.id:old.proposal);
   const result={v:1,delivery_id:job.id,request_id:input.id,incident:input.incident,kind:state,evidence:reviewed};
   this.db.prepare("UPDATE events SET state='reply_ready',result=? WHERE id=?").run(JSON.stringify(result),job.id);
   onSaved();
  });
 }
}

import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {ReviewedStore} from './reviewed-store.ts';
for(const [passed,decision,expected] of [[true,'accept','closed'],[true,'reject','retry_allowed'],[false,'accept','retry_allowed'],[true,'human_required','human_required']] as const){
 test(`remote pass ${passed} plus manager ${decision} yields ${expected}`,()=>{
  const root=mkdtempSync(join(tmpdir(),'reviewed-')),s=new ReviewedStore(join(root,'state.db'));
  try{
   s.bind('source','chat');s.ingest([{event_id:1,from_agent_id:'source',body:JSON.stringify({v:1,id:'q',incident:'issue',kind:'request',payload:{question:'problem'}})}],1);
   s.ready(s.rows()[0],{text:JSON.stringify({decision:'propose',summary:'reason',proposal:'candidate'})});
   s.ingest([{event_id:2,from_agent_id:'source',body:JSON.stringify({v:1,id:'v',incident:'issue',kind:'verification',payload:{proposal_id:'q',pass:passed,evidence:'test output'}})}],2);
   const job=s.rows()[1];
   assert.throws(()=>s.ready(job,{text:'looks fine'}),/structured/);
   s.ready(job,{text:JSON.stringify({decision,summary:'local evidence review'})});
   const row=s.rows()[1];assert.equal(JSON.parse(row.result).kind,expected);
   assert.equal(JSON.parse(row.body).payload.pass,passed);
   assert.equal(JSON.parse(row.result).evidence.managerDecision.decision,decision);
  }finally{s.close();rmSync(root,{recursive:true,force:true});}
 });
}
for(const decision of ['accept','reject'])test(`evidence retry ${decision} closes or escalates without an endless loop`,()=>{
 const root=mkdtempSync(join(tmpdir(),'review-retry-')),s=new ReviewedStore(join(root,'state.db'));
 try{
  s.bind('source','chat');
  const ingest=(id:string,kind:string,payload:any)=>s.ingest([{event_id:s.rows().length+1,from_agent_id:'source',body:JSON.stringify({v:1,id,incident:'issue',kind,payload})}],s.rows().length+1);
  ingest('q','request',{question:'problem'});s.ready(s.rows()[0],{text:JSON.stringify({decision:'propose',summary:'test',proposal:2})});
  ingest('v1','verification',{proposal_id:'q',pass:true,evidence:'missing command'});
  s.ready(s.rows()[1],{text:JSON.stringify({decision:'reject',summary:'need command'})});
  ingest('v2','verification',{proposal_id:'q',pass:true,evidence:'command and output'});
  s.validatePhase(s.rows()[2]);s.ready(s.rows()[2],{text:JSON.stringify({decision,summary:'review second evidence'})});
  assert.equal(JSON.parse(s.rows()[2].result).kind,decision==='accept'?'closed':'human_required');
  ingest('v3','verification',{proposal_id:'q',pass:true,evidence:'repeat'});
  assert.throws(()=>s.validatePhase(s.rows()[3]),/unexpected_verification/);
 }finally{s.close();rmSync(root,{recursive:true,force:true});}
});

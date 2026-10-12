import {mailboxRpc} from './mailbox-driver.ts';

// Uses the installed Harness adapter supplied by the manager. No direct Codex
// execution, no automatic resubmission when a network acknowledgment is lost.
export class ResumeController {
 constructor(readonly python:string,readonly script:string,readonly ledger:string,
  readonly harness:{recover:(task:string,command:string)=>Promise<any>,submit:(request:any)=>Promise<any>}){}
 rpc(request:any){return mailboxRpc(this.python,this.script,{...request,ledger:this.ledger});}
 async plan(binding:any,question:any,context:any){return this.rpc({action:'plan',binding,question,context});}
 async tick(id:string,guard:()=>void=()=>{}){
  const row=await this.rpc({action:'inspect',id});const body=JSON.parse(row.body);
  if(row.state==='finished')return row;
  if(row.state==='prepared')return {state:'submission_uncertain',id};
  if(row.state==='running'){
   const receipt=await this.harness.recover(row.task,row.command_id);
   return this.rpc({action:'reconcile',id,receipt});
  }
  const previous=await this.harness.recover(row.task,body.previous_command_id);
  guard();
  const claim=await this.rpc({action:'claim',id,receipt:previous});
  if(claim.state!=='claimed')return claim;
  // Durable claim is committed by the Python process before the network call.
  guard();
  const receipt=await this.harness.submit(claim.request);
  return this.rpc({action:'reconcile',id,receipt});
 }
}

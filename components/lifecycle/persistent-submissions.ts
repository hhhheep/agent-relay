import {createHash,randomUUID} from 'node:crypto';
import {closeSync,fsyncSync,lstatSync,openSync,readFileSync,readdirSync,realpathSync,renameSync,unlinkSync,writeFileSync} from 'node:fs';
import {isAbsolute,join,resolve} from 'node:path';
import type {PersistentBinding} from './persistent-harness.ts';

type Phase='queued'|'sent'|'received'|'unknown'|'denied';
type SubmissionInput={command:string,access_mode:string,timeout_seconds:number|null,finish_maintenance:boolean,wait_ms:number,max_preview_bytes:number};
export type SubmissionRecord={schema:'persistent-submission/v1'|'persistent-submission/v2',submissionId:string,commandId:string,
 taskId:string,workUnitId:string,originChat:string,originEpoch:number,generation:string,
 commandSha256:string,requestSha256:string,createdAt:string,phase:Phase,input?:SubmissionInput};
const sha=(value:string|Buffer)=>createHash('sha256').update(value).digest('hex');
const phases:Phase[]=['queued','sent','received','unknown','denied'];
export function submissionCommandId(taskId:string,workUnitId:string,submissionId:string){
 if(!taskId?.trim()||!workUnitId?.trim()||!/^[a-f0-9]{32}$/.test(submissionId))throw Error('persistent_submission_binding_required');
 return sha(JSON.stringify(['plain-submission/v1',taskId,workUnitId,submissionId])).slice(0,32);
}

// A private intent receipt beside the manual owner, not a new authority store.
// Exact original inputs stay in this operator-protected private store. Recovery
// lists return IDs/hashes, never these inputs. The journal cannot grant authority.
export class PersistentSubmissions {
 private root:string;
 constructor(directory:string){
  if(!isAbsolute(directory)||lstatSync(directory).isSymbolicLink()||!lstatSync(directory).isDirectory())throw Error('persistent_submission_directory_rejected');
  this.root=realpathSync.native(directory);
  const normalize=(path:string)=>process.platform==='win32'?path.toLowerCase():path;
  if(normalize(resolve(directory))!==normalize(this.root))throw Error('persistent_submission_directory_rejected');
 }
 private validate(value:any):SubmissionRecord{
  if(!['persistent-submission/v1','persistent-submission/v2'].includes(value?.schema)||!phases.includes(value.phase)||typeof value.originChat!=='string'||!value.originChat||typeof value.generation!=='string'||!value.generation||!Number.isSafeInteger(value.originEpoch)||value.originEpoch<0||!Number.isFinite(Date.parse(value.createdAt))||!['commandSha256','requestSha256'].every(k=>typeof value[k]==='string'&&/^[a-f0-9]{64}$/.test(value[k]))||submissionCommandId(value.taskId,value.workUnitId,value.submissionId)!==value.commandId)throw Error('persistent_submission_receipt_corrupt');
  if(value.schema==='persistent-submission/v2'){
   const i=value.input;
   if(typeof i?.command!=='string'||Buffer.byteLength(i.command)>1048576||sha(i.command)!==value.commandSha256||!['read_only','writer'].includes(i.access_mode)||!(i.timeout_seconds===null||typeof i.timeout_seconds==='number'&&Number.isFinite(i.timeout_seconds))||typeof i.finish_maintenance!=='boolean'||!Number.isSafeInteger(i.wait_ms)||!Number.isSafeInteger(i.max_preview_bytes)||sha(JSON.stringify([value.taskId,value.workUnitId,i]))!==value.requestSha256)throw Error('persistent_submission_input_corrupt');
  }
  return value;
 }
 private path(id:string){if(!/^[a-f0-9]{32}$/.test(id))throw Error('persistent_submission_receipt_corrupt');return join(this.root,id+'.json');}
 private read(id:string){
  const path=this.path(id);if(lstatSync(path).isSymbolicLink()||!lstatSync(path).isFile())throw Error('persistent_submission_receipt_corrupt');
  const value=this.validate(JSON.parse(readFileSync(path,'utf8')));
  if(value.commandId!==id)throw Error('persistent_submission_receipt_corrupt');return value;
 }
 private writeExclusive(path:string,value:SubmissionRecord){
  const fd=openSync(path,'wx',0o600);try{writeFileSync(fd,JSON.stringify(value));fsyncSync(fd);}finally{closeSync(fd);}
 }
 private files(){
  const names=readdirSync(this.root).filter(name=>name.endsWith('.json'));
  if(names.length>4096)throw Error('persistent_submission_journal_full');
  let bytes=0;for(const name of names){const stat=lstatSync(this.path(name.slice(0,-5)));if(stat.isSymbolicLink()||!stat.isFile()||stat.size>3145728)throw Error('persistent_submission_receipt_corrupt');bytes+=stat.size;}
  if(bytes>67108864)throw Error('persistent_submission_journal_full');return {names,bytes};
 }
 list(taskId?:string,workUnitId?:string){
  const {names}=this.files();
  return names.map(name=>this.read(name.slice(0,-5))).filter(r=>(taskId===undefined||r.taskId===taskId)&&(workUnitId===undefined||r.workUnitId===workUnitId)).sort((a,b)=>a.createdAt.localeCompare(b.createdAt)||a.commandId.localeCompare(b.commandId));
 }
 begin(binding:PersistentBinding,generation:string,args:any){
  const inventory=this.files();if(inventory.names.length>=4096)throw Error('persistent_submission_journal_full');
  if(typeof args.command!=='string'||!args.command.trim()||args.command_id||args.worker_task||args.submission_id!=null)throw Error('persistent_program_submission_required');
  const submissionId=randomUUID().replaceAll('-',''),commandSha256=sha(args.command);
  const input={command:args.command,access_mode:args.access_mode??'writer',timeout_seconds:args.timeout_seconds??null,finish_maintenance:args.finish_maintenance??false,wait_ms:args.wait_ms??30000,max_preview_bytes:args.max_preview_bytes??4096};
  const record=this.validate({schema:'persistent-submission/v2',submissionId,commandId:submissionCommandId(binding.taskId,binding.workUnitId,submissionId),
   taskId:binding.taskId,workUnitId:binding.workUnitId,originChat:binding.chat,originEpoch:binding.epoch,generation,commandSha256,
   requestSha256:sha(JSON.stringify([binding.taskId,binding.workUnitId,input])),createdAt:new Date().toISOString(),phase:'queued',input});
  if(inventory.bytes+Buffer.byteLength(JSON.stringify(record))>67108864)throw Error('persistent_submission_journal_full');
  this.writeExclusive(this.path(record.commandId),record);return record;
 }
 transition(expected:SubmissionRecord,phase:Phase){
  const current=this.read(expected.commandId);
  if(JSON.stringify(current)!==JSON.stringify(expected))throw Error('persistent_submission_receipt_conflict');
  const allowed:Record<Phase,Phase[]>={queued:['sent','denied'],sent:['received','unknown'],received:['received'],unknown:['received','unknown'],denied:['denied']};
  if(!allowed[current.phase].includes(phase))throw Error('persistent_submission_phase_rejected');
  const next=this.validate({...current,phase}),temporary=join(this.root,'.pending-'+randomUUID()+'.tmp');
  this.writeExclusive(temporary,next);
  try{if(JSON.stringify(this.read(expected.commandId))!==JSON.stringify(expected))throw Error('persistent_submission_receipt_conflict');renameSync(temporary,this.path(next.commandId));}
  finally{try{unlinkSync(temporary);}catch(error:any){if(error.code!=='ENOENT')throw error;}}
  return next;
 }
}

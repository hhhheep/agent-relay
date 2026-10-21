import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,readdirSync,rmSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {PersistentSubmissions,submissionCommandId} from './persistent-submissions.ts';

const binding={chat:'original-chat',epoch:2,taskId:'task',workUnitId:'unit'};
function fixture(){const root=mkdtempSync(join(tmpdir(),'submission-journal-'));return {root,journal:new PersistentSubmissions(root),close:()=>rmSync(root,{recursive:true,force:true})};}

test('queued original input is durable and hash-bound; identical legitimate requests remain distinct',()=>{
 const f=fixture();try{
  const first=f.journal.begin(binding,'generation',{command:'sensitive synthetic command',access_mode:'read_only'});
  const second=f.journal.begin(binding,'generation',{command:'sensitive synthetic command',access_mode:'read_only'});
  assert.notEqual(first.commandId,second.commandId);assert.equal(first.commandSha256,second.commandSha256);
  const reopened=new PersistentSubmissions(f.root);assert.deepEqual(reopened.list('task','unit'),[first,second].sort((a,b)=>a.createdAt.localeCompare(b.createdAt)||a.commandId.localeCompare(b.commandId)));
  assert.equal(first.input?.command,'sensitive synthetic command');assert.equal(first.schema,'persistent-submission/v2');
  assert.equal(first.commandId,submissionCommandId('task','unit',first.submissionId));assert.equal(first.phase,'queued');
 }finally{f.close();}
});

test('sent and lost-ACK receipts survive reopen without a replay transition',()=>{
 const f=fixture();try{
  const queued=f.journal.begin(binding,'generation',{command:'synthetic'});
  const sent=f.journal.transition(queued,'sent');assert.equal(new PersistentSubmissions(f.root).list()[0].phase,'sent');
  const unknown=f.journal.transition(sent,'unknown');assert.equal(new PersistentSubmissions(f.root).list()[0].phase,'unknown');
  assert.throws(()=>f.journal.transition(unknown,'sent'),/phase_rejected/);
  assert.throws(()=>f.journal.transition(sent,'received'),/receipt_conflict/);
  const received=f.journal.transition(unknown,'received');assert.equal(received.commandId,queued.commandId);
  assert.throws(()=>f.journal.transition(received,'queued'),/phase_rejected/);
 }finally{f.close();}
});

test('journal is not authority; original controller/epoch remain unchanged and reads filter exact scope',()=>{
 const f=fixture();try{
  const queued=f.journal.begin(binding,'generation',{command:'synthetic'});
  assert.equal(f.journal.list('other-task').length,0);assert.equal(f.journal.list('task','other-unit').length,0);
  const denied=f.journal.transition(queued,'denied');assert.equal(denied.originChat,'original-chat');assert.equal(denied.originEpoch,2);
  assert.equal(submissionCommandId('task','unit',queued.submissionId)===submissionCommandId('task','other-unit',queued.submissionId),false);
 }finally{f.close();}
});

test('corrupt receipt and model-supplied intent fail closed without replacing original data',()=>{
 const f=fixture();try{
  assert.throws(()=>f.journal.begin(binding,'generation',{command:'synthetic',submission_id:'a'.repeat(32)}),/program_submission_required/);
  assert.equal(readdirSync(f.root).length,0);
  const first=f.journal.begin(binding,'generation',{command:'synthetic'}),path=join(f.root,first.commandId+'.json');
  writeFileSync(path,JSON.stringify({...first,submissionId:'b'.repeat(32)}));const before=readFileSync(path);
  assert.throws(()=>f.journal.list(),/receipt_corrupt/);assert.deepEqual(readFileSync(path),before);
 }finally{f.close();}
});

test('private original input changes are detected before any recovery transition',()=>{
 const f=fixture();try{
  const record=f.journal.begin(binding,'generation',{command:'synthetic input',finish_maintenance:true,timeout_seconds:9,wait_ms:0,max_preview_bytes:1});
  assert.equal(record.input?.finish_maintenance,true);assert.equal(record.input?.timeout_seconds,9);
  const path=join(f.root,record.commandId+'.json');writeFileSync(path,JSON.stringify({...record,input:{...record.input,command:'changed'}}));
  assert.throws(()=>f.journal.list(),/input_corrupt/);
 }finally{f.close();}
});

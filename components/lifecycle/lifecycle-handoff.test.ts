import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';import {join} from 'node:path';import {tmpdir} from 'node:os';
import {requestLifecycleHandoff,lifecycleHandoffRequested} from './lifecycle-handoff.ts';
import {DatabaseSync} from 'node:sqlite';
test('handoff addresses exact owner generation, expires and never contains App capability',()=>{
 const root=mkdtempSync(join(tmpdir(),'handoff-'));
 try{
  const db=new DatabaseSync(join(root,'queue.sqlite'));db.exec('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');
  db.prepare('INSERT INTO meta VALUES (?,?)').run('owner',String(process.pid));
  db.prepare('INSERT INTO meta VALUES (?,?)').run('manager_generation','original');db.close();
  writeFileSync(join(root,'status.json'),JSON.stringify({lifecycle:'active',ownerPid:process.pid,managerGeneration:'original',at:new Date(1000).toISOString()}));
  assert.equal(requestLifecycleHandoff(root,'verified-caller',1000).state,'handoff_requested');
  assert.equal(lifecycleHandoffRequested(root,'original',process.pid,1001),true);
  assert.equal(lifecycleHandoffRequested(root,'replacement',123,1001),false);
  assert.equal(lifecycleHandoffRequested(root,'original',456,1001),false);
  assert.equal(lifecycleHandoffRequested(root,'original',123,62000),false);
  assert.equal(lifecycleHandoffRequested(root,'original',123,999),false);
  const request=JSON.parse(readFileSync(join(root,'handoff.json'),'utf8'));
  assert.equal(Object.keys(request).sort().join(','),'expiresAt,managerGeneration,ownerPid,requestId,requestedAt,schema,verifiedCaller');
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('missing, stopped or old unversioned owner uses normal owner wait',()=>{
 const root=mkdtempSync(join(tmpdir(),'handoff-'));
 try{
  assert.equal(requestLifecycleHandoff(root,'caller').state,'owner_not_observed');
  for(const status of [{lifecycle:'stopped',ownerPid:123,managerGeneration:'old'},{lifecycle:'active',ownerPid:123}]){
   writeFileSync(join(root,'status.json'),JSON.stringify(status));
   assert.equal(requestLifecycleHandoff(root,'caller').state,'owner_not_observed');
  }
  assert.equal(lifecycleHandoffRequested(root,'old'),false);
 }finally{rmSync(root,{recursive:true,force:true});}
});

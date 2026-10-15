import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,existsSync,rmSync,readFileSync} from 'node:fs';
import {join} from 'node:path';import {tmpdir} from 'node:os';
import {requestLifecycleHandoff} from './lifecycle-handoff.ts';
import {inspectManagerStatus} from './manager-health.ts';
import {DatabaseSync} from 'node:sqlite';
import {execFileSync,spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {runManager} from './manager-driver.ts';

function fixture(){
 const root=mkdtempSync(join(tmpdir(),'manager-health-')),now=Date.now();
 const status={lifecycle:'active',state:'running',transport:'connected',appAvailability:'online',resumeState:'ready',pending:0,
  ownerPid:process.pid,managerGeneration:'current',at:new Date(now).toISOString()};
 const db=new DatabaseSync(join(root,'queue.sqlite'));db.exec('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');
 db.prepare('INSERT INTO meta VALUES (?,?)').run('owner',String(process.pid));
 db.prepare('INSERT INTO meta VALUES (?,?)').run('manager_generation','current');db.close();
 const write=()=>writeFileSync(join(root,'status.json'),JSON.stringify(status));write();
 return {root,now,status,write};
}

test('55-minute-old connected snapshot cannot claim a live handoff owner',()=>{
 const root=mkdtempSync(join(tmpdir(),'manager-stale-')),now=Date.now();
 try{
  writeFileSync(join(root,'status.json'),JSON.stringify({lifecycle:'active',state:'running',transport:'connected',pending:0,
   ownerPid:123,managerGeneration:'historical',at:new Date(now-55*60000).toISOString()}));
  assert.equal(requestLifecycleHandoff(root,'verified-caller',now).state,'owner_not_observed');
  assert.equal(existsSync(join(root,'handoff.json')),false);
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('online requires fresh live matching owner generation; reads never rewrite evidence',()=>{
 const f=fixture();try{
  const status=readFileSync(join(f.root,'status.json')),db=readFileSync(join(f.root,'queue.sqlite'));
  assert.equal(inspectManagerStatus(f.root,{now:f.now}).online,true);
  assert.equal(inspectManagerStatus(f.root,{now:f.now+55*60000}).state,'stale');
  assert.equal(inspectManagerStatus(f.root,{now:f.now,probeOwner:()=> 'exited'}).reason,'owner_exited');
  assert.equal(inspectManagerStatus(f.root,{now:f.now,probeOwner:()=> 'unknown'}).online,false);
  assert.equal(inspectManagerStatus(f.root,{now:f.now-1}).reason,'invalid_observation_time');
  assert.deepEqual(readFileSync(join(f.root,'status.json')),status);assert.deepEqual(readFileSync(join(f.root,'queue.sqlite')),db);
  f.status.managerGeneration='stale-generation';f.write();
  assert.equal(inspectManagerStatus(f.root,{now:f.now}).reason,'owner_generation_not_current');
  assert.equal(requestLifecycleHandoff(f.root,'caller',f.now).state,'owner_not_observed');
  assert.equal(existsSync(join(f.root,'handoff.json')),false);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test('stopped, degraded, missing and malformed evidence cannot report online',()=>{
 const f=fixture();try{
  f.status.lifecycle='stopped';f.write();assert.equal(inspectManagerStatus(f.root,{now:f.now}).state,'stopped');
  f.status.lifecycle='active';f.status.transport='disconnected';f.write();assert.equal(inspectManagerStatus(f.root,{now:f.now}).state,'degraded');
  writeFileSync(join(f.root,'status.json'),'null');assert.equal(inspectManagerStatus(f.root).online,false);
  writeFileSync(join(f.root,'status.json'),'{');assert.equal(inspectManagerStatus(f.root).online,false);
  f.write();rmSync(join(f.root,'queue.sqlite'));
  assert.equal(inspectManagerStatus(f.root,{now:f.now}).reason,'ownership_evidence_unavailable');
  assert.equal(existsSync(join(f.root,'queue.sqlite')),false);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test('manager CLI status inspects evidence without token, App context or service startup',()=>{
 const f=fixture();try{
  const config=join(f.root,'config.json');writeFileSync(config,JSON.stringify({stateRoot:f.root}));
  const cli=fileURLToPath(new URL('./manager-cli.ts',import.meta.url));
  const result=JSON.parse(execFileSync(process.execPath,['--import','tsx',cli,config,'--status'],{cwd:fileURLToPath(new URL('.',import.meta.url)),env:{},encoding:'utf8'}));
  assert.equal(result.state,'online');assert.equal(result.lastObserved.pending,0);
  assert.equal(existsSync(join(f.root,'handoff.json')),false);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test('real owner normal exit invalidates even a fresh connected zero-pending snapshot',async()=>{
 const f=fixture();
 const child=spawn(process.execPath,['-e',"console.log('ready');process.stdin.resume();process.stdin.on('end',()=>process.exit(0));"],{stdio:['pipe','pipe','pipe'],windowsHide:true});
 try{
  await once(child.stdout,'data');f.status.ownerPid=child.pid!;f.write();
  const db=new DatabaseSync(join(f.root,'queue.sqlite'));db.prepare("UPDATE meta SET value=? WHERE key='owner'").run(String(child.pid));db.close();
  assert.equal(inspectManagerStatus(f.root).online,true);
  const exited=once(child,'exit');child.stdin.end();await exited;
  const result=inspectManagerStatus(f.root);
  assert.equal(result.online,false);assert.equal(result.reason,'owner_exited');
  assert.equal(result.lastObserved?.pending,0);
 }finally{if(child.exitCode===null){const exited=once(child,'exit');child.stdin.end();await exited;}rmSync(f.root,{recursive:true,force:true});}
});

test('production Manager publishes current SQL generation and stopped status is never online',async()=>{
 const root=mkdtempSync(join(tmpdir(),'manager-health-tick-'));let during:any;
 const app:any={connect:async()=>{},close:async()=>{},identity:()=>({threadId:'synthetic'}),canReconnect:()=>false,diagnostics:()=>({})};
 try{
  await runManager({stateRoot:root,url:'http://unused/mcp',name:'manager',team:'synthetic',appServerPath:join(root,'app.mjs')},
   {HARNESS_XATS_TOKEN:'synthetic'},new AbortController().signal,{once:true,onStatus:s=>{if(s.lifecycle==='active')during=inspectManagerStatus(root);}},
   {app:()=>app,connectInbox:(async()=>({agentId:'synthetic',get:async()=>({messages:[]}),send:async()=>({ok:true}),close:async()=>{}})) as any});
  assert.equal(during.online,true);assert.equal(inspectManagerStatus(root).state,'stopped');
 }finally{rmSync(root,{recursive:true,force:true});}
});

import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {PersistentReconnect} from './persistent-reconnect.ts';

test('one automatic attempt after settled network; failure survives restart and toggle',async()=>{
 const root=mkdtempSync(join(tmpdir(),'reconnect-policy-'));let now=1000,calls=0;
 try{
  const path=join(root,'policy.json'),p=new PersistentReconnect(path,60000,()=>now),offline={state:'offline',pending:0};
  assert.equal(p.status().enabled,false);p.setEnabled(true);
  const fail=async()=>{calls++;assert.equal(JSON.parse(readFileSync(path,'utf8')).latched,true);throw Error('network refused');};
  await p.observe(offline,'WLAN-A',()=>{},fail);now+=9000;await p.observe(offline,'WLAN-B',()=>{},fail);assert.equal(calls,0);
  now+=11000;await p.observe({...offline,pending:1},'WLAN-B',()=>{},fail);assert.equal(calls,0);
  await p.observe(offline,'WLAN-B',()=>{},fail);assert.equal(calls,1);
  now+=120000;const restarted=new PersistentReconnect(path,60000,()=>now);restarted.setEnabled(false);restarted.setEnabled(true);
  await restarted.observe(offline,'WLAN-C',()=>{},fail);now+=11000;await restarted.observe(offline,'WLAN-C',()=>{},fail);assert.equal(calls,1);assert.equal(restarted.status().latched,true);
  await restarted.attempt('manual',()=>{},async()=>{calls++;return 'connected';});assert.equal(restarted.status().latched,false);
  await assert.rejects(restarted.attempt('manual',()=>{},async()=>calls++),/cooldown/);assert.equal(calls,2);
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('parallel manual/automatic calls reserve one attempt; healthy transport never reconnects',async()=>{
 const root=mkdtempSync(join(tmpdir(),'reconnect-policy-'));let now=1000,calls=0,release!:()=>void;
 try{
  const p=new PersistentReconnect(join(root,'policy.json'),60000,()=>now);p.setEnabled(true);
  const connect=async()=>{calls++;await new Promise<void>(r=>release=r);};
  await p.observe({state:'online',pending:0},'network-A',()=>{},connect);now+=120000;await p.observe({state:'online',pending:0},'network-B',()=>{},connect);assert.equal(calls,0);
  const attempt=p.attempt('manual',()=>{},connect);await assert.rejects(p.attempt('manual',()=>{},connect),/busy/);assert.throws(()=>p.setEnabled(false),/busy/);assert.equal(calls,1);release();await attempt;
  now+=61000;await assert.rejects(p.attempt('manual',()=>{throw Error('pending');},connect),/pending/);assert.equal(p.status().latched,false);
  assert.throws(()=>p.setEnabled('yes' as any),/boolean/);
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('successful network flapping cannot exceed the persisted automatic hourly budget',async()=>{
 const root=mkdtempSync(join(tmpdir(),'reconnect-budget-'));let now=1000,starts=0;
 try{
  const path=join(root,'policy.json'),p=new PersistentReconnect(path,60000,()=>now);p.setEnabled(true);
  for(let i=0;i<3;i++){await p.attempt('automatic',()=>{},async()=>{starts++;});now+=61000;}
  const next=new PersistentReconnect(path,60000,()=>now);
  await assert.rejects(next.attempt('automatic',()=>{},async()=>{starts++;}),/hourly_budget/);
  assert.equal(starts,3);assert.equal(next.status().latched,true);assert.match(next.status().lastReason,/budget/);
  next.setEnabled(false);next.setEnabled(true);await assert.rejects(next.attempt('automatic',()=>{},async()=>{starts++;}),/latched/);
 }finally{rmSync(root,{recursive:true,force:true});}
});

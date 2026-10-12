import test from 'node:test';import assert from 'node:assert/strict';
import {bindManagerReceipt} from './manager-task-protocol.ts';
test('only v2 permits omitted routing fields; legacy identity and contradictory fields remain checked',()=>{
 const expected={threadId:'chat',requestId:'request',episodeId:'episode',objective:'objective',turnId:'turn'};
 assert.deepEqual(bindManagerReceipt({text:'answer'},expected,true),{text:'answer',...expected});
 assert.throws(()=>bindManagerReceipt({text:'answer'},expected,false),/binding_missing/);
 assert.deepEqual(bindManagerReceipt({...expected,text:'answer'},expected,false),{...expected,text:'answer'});
 for(const key of Object.keys(expected))assert.throws(()=>bindManagerReceipt({...expected,[key]:'wrong'},expected,true),/binding_mismatch/);
 for(const invalid of [null,[],42])assert.throws(()=>bindManagerReceipt(invalid,expected,true),/invalid_model_receipt/);
});

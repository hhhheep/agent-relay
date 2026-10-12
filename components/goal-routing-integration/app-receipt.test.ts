import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {acceptAppCompletion} from './app-receipt.ts';

const raw=JSON.parse(readFileSync(new URL('./app-chat-phase2.json',import.meta.url),'utf8'));
const expected={threadId:raw.chatId,requestId:raw.marker,episodeId:'phase-2',objective:raw.steps.find((x:any)=>x.step==='create_goal').response.goal.objective};
const turn={threadId:raw.chatId,turnId:'synthetic-turn',status:'completed'};
// Retrospective fixture normalization: request/episode/turn fields were not in
// the original child receipt. They are test bindings, not claimed live routing.
const receipt={...expected,turnId:turn.turnId,started:raw.steps.find((x:any)=>x.step==='create_goal').response,completed:raw.steps.find((x:any)=>x.step==='update_goal_complete').response,text:JSON.stringify({answer:raw.answer})};
test('synthetic App response shapes: explicit completion survives subsequent null get',()=>{
  assert.equal(raw.steps.at(-1).response.goal,null);
  assert.equal(acceptAppCompletion(expected,receipt,turn).goal.status,'complete');
  assert.throws(()=>acceptAppCompletion(expected,{...receipt,completed:raw.steps.at(-1).response},turn),/explicit_app_completion_required/);
});
test('reject wrong chat, request, episode, objective and observed turn',()=>{
  for(const key of ['threadId','requestId','episodeId','objective']){
    assert.throws(()=>acceptAppCompletion(expected,{...receipt,[key]:'wrong'},turn),/binding_mismatch/);
  }
  assert.throws(()=>acceptAppCompletion(expected,receipt,{...turn,threadId:'wrong'}),/turn_binding/);
  assert.throws(()=>acceptAppCompletion(expected,receipt,{...turn,turnId:'old-turn'}),/turn_binding/);
  assert.throws(()=>acceptAppCompletion(expected,receipt,{...turn,status:'inProgress'}),/not_completed/);
});
test('reject stale Goal, forged objective, stopped Goal and missing output',()=>{
  for(const patch of [{createdAt:receipt.completed.goal.createdAt+1},{objective:'other'},{threadId:'other'},{status:'paused'},{createdAt:0},{updatedAt:0}]){
    assert.throws(()=>acceptAppCompletion(expected,{...receipt,completed:{goal:{...receipt.completed.goal,...patch}}},turn));
  }
  assert.throws(()=>acceptAppCompletion(expected,{...receipt,text:''},turn),/missing_app_result/);
});

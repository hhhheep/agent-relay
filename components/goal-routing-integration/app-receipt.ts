import {parseCodexThreadGoal} from './vendor/paperclip-goal.ts';

export type AppEpisode = {threadId:string; requestId:string; episodeId:string; objective:string};
export type AppTurn = {threadId:string; turnId:string; status:string};

// Local adapter boundary only. The caller must obtain the receipt from the
// explicitly bound App chat, not from an untrusted remote worker's payload.
// A receipt's fields do not authenticate its sender. No wake transport is
// implemented here, and a null get_goal is never a completion receipt.
export function acceptAppCompletion(expected:AppEpisode, receipt:any, turn:AppTurn){
  for(const key of ['threadId','requestId','episodeId','objective'] as const){
    if(typeof expected[key]!=='string'||!expected[key].length)throw Error('invalid_expected_episode');
    if(receipt?.[key]!==expected[key])throw Error('app_receipt_binding_mismatch');
  }
  if(turn.threadId!==expected.threadId||!turn.turnId||receipt.turnId!==turn.turnId)
    throw Error('app_turn_binding_mismatch');
  if(turn.status!=='completed')throw Error('app_turn_not_completed');
  const active=parseCodexThreadGoal(receipt.started?.goal);
  const complete=parseCodexThreadGoal(receipt.completed?.goal);
  if(!active||active.status!=='active'||!complete||complete.status!=='complete')
    throw Error('explicit_app_completion_required');
  for(const goal of [active,complete]){
    if(goal.threadId!==expected.threadId||goal.objective!==expected.objective)
      throw Error('app_goal_binding_mismatch');
    if(!goal.createdAt||goal.updatedAt<goal.createdAt)throw Error('invalid_app_goal_timestamp');
  }
  if(active.createdAt!==complete.createdAt||complete.updatedAt<active.updatedAt)
    throw Error('app_goal_episode_mismatch');
  if(typeof receipt.text!=='string'||!receipt.text.trim())throw Error('missing_app_result');
  return {goal:complete,turnId:turn.turnId,text:receipt.text,requestId:expected.requestId,episodeId:expected.episodeId};
}

// An explicitly authorized problem Goal may span several completed App turns.
// This confirms a bounded reply, never completion of the ongoing problem.
export function acceptAppProgress(expected:AppEpisode,receipt:any,turn:AppTurn){
 if(!expected.objective.startsWith('Harness help problem ')||receipt.problemResolved!==false||receipt.completed!==undefined)throw Error('explicit_app_progress_required');
 for(const key of ['threadId','requestId','episodeId','objective'] as const){
  if(typeof expected[key]!=='string'||!expected[key]||receipt[key]!==expected[key])throw Error('app_receipt_binding_mismatch');
 }
 if(turn.threadId!==expected.threadId||!turn.turnId||receipt.turnId!==turn.turnId)throw Error('app_turn_binding_mismatch');
 if(turn.status!=='completed')throw Error('app_turn_not_completed');
 const started=parseCodexThreadGoal(receipt.started?.goal),after=parseCodexThreadGoal(receipt.after?.goal);
 if(!started||!after||started.status!=='active'||after.status!=='active')throw Error('active_problem_goal_required');
 for(const goal of [started,after]){
  if(goal.threadId!==expected.threadId||goal.objective!==expected.objective)throw Error('app_goal_binding_mismatch');
  if(!goal.createdAt||goal.updatedAt<goal.createdAt)throw Error('invalid_app_goal_timestamp');
 }
 if(started.createdAt!==after.createdAt||after.updatedAt<started.updatedAt)throw Error('app_goal_episode_mismatch');
 if(typeof receipt.text!=='string'||!receipt.text.trim())throw Error('missing_app_result');
 return {goal:after,turnId:turn.turnId,text:receipt.text,requestId:expected.requestId,episodeId:expected.episodeId,problemResolved:false};
}

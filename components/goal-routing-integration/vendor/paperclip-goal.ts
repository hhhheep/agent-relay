// Extracted from Paperclip c8f874311c02eff7cdf5ab9e85dc37d80ed50147.
// Copyright (c) 2025 Paperclip AI. MIT; see PAPERCLIP-LICENSE.
// Adaptations: local structural type; RPC mapping lifted into a pure function.
type HarnessThreadGoal = {threadId:string; objective:string; status:string; tokenBudget:number|null; tokensUsed:number; timeUsedSeconds:number; createdAt:number; updatedAt:number};
function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

export function parseCodexThreadGoal(value: unknown): HarnessThreadGoal | null {
  const goal = record(value);
  const threadId = text(goal.threadId);
  const objective = text(goal.objective);
  const status = text(goal.status);
  if (
    threadId.length === 0 ||
    objective.length === 0 ||
    ![
      "active",
      "paused",
      "blocked",
      "usageLimited",
      "budgetLimited",
      "complete",
    ].includes(status)
  ) {
    return null;
  }
  const tokenBudget = goal.tokenBudget === null || goal.tokenBudget === undefined
    ? null
    : nonNegativeInteger(goal.tokenBudget);
  const tokensUsed = goal.tokensUsed === undefined
    ? 0
    : nonNegativeInteger(goal.tokensUsed);
  const timeUsedSeconds = goal.timeUsedSeconds === undefined
    ? 0
    : nonNegativeInteger(goal.timeUsedSeconds);
  const createdAt = goal.createdAt === undefined
    ? 0
    : nonNegativeInteger(goal.createdAt);
  const updatedAt = goal.updatedAt === undefined
    ? 0
    : nonNegativeInteger(goal.updatedAt);
  if (
    (goal.tokenBudget !== null && goal.tokenBudget !== undefined && tokenBudget === null) ||
    tokensUsed === null ||
    timeUsedSeconds === null ||
    createdAt === null ||
    updatedAt === null
  ) return null;
  return {
    threadId,
    objective,
    status: status as HarnessThreadGoal["status"],
    tokenBudget,
    tokensUsed,
    timeUsedSeconds,
    createdAt,
    updatedAt,
  };
}


export function goalCommand(threadId:string,input:{action:"get"|"set"|"clear"|"pause"|"resume";objective?:string;status?:string;tokenBudget?:number|null}) {
    let method: string;
    let params: Record<string, unknown> = { threadId: threadId };
    if (input.action === "get") {
      method = "thread/goal/get";
    } else if (input.action === "clear") {
      method = "thread/goal/clear";
    } else {
      method = "thread/goal/set";
      if (input.action === "set") {
        params = {
          ...params,
          objective: input.objective,
          status: input.status ?? "active",
          ...(input.tokenBudget !== undefined
            ? { tokenBudget: input.tokenBudget }
            : {}),
        };
      } else {
        params = {
          ...params,
          status: input.action === "pause" ? "paused" : "active",
        };
      }
    }
  return {method,params};
}

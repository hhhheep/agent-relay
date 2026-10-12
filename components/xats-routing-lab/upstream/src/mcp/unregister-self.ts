import type Database from 'better-sqlite3'
import type { AgentsRepo } from '../storage/agents-repo.js'

export type UnregisterSelfResult =
  | { ok: true; team: string; name: string; agent_id: string }
  | { error: 'unknown_agent' }

// Single removal code path, shared by the unregister_self tool and the
// DELETE /api/agents/:agent_id REST route. Removal deletes the registry row
// only — it does not stop the process, pane, or runtime-side session behind
// it, and for kimi-code the session keeps running and keeps accepting prompts.
export function removeAgentRow(
  db: Database.Database,
  agents: AgentsRepo,
  agent_id: string
): UnregisterSelfResult {
  const row = agents.findById(agent_id)
  if (!row) return { error: 'unknown_agent' }

  let removed = false
  const tx = db.transaction(() => {
    removed = agents.deleteById(row.agent_id)
  })
  tx()

  if (!removed) return { error: 'unknown_agent' }
  return { ok: true, team: row.team, name: row.name, agent_id: row.agent_id }
}

export class UnregisterSelfService {
  constructor(
    private readonly db: Database.Database,
    private readonly agents: AgentsRepo
  ) {}

  unregister(args: { caller: string }): UnregisterSelfResult {
    return removeAgentRow(this.db, this.agents, args.caller)
  }
}

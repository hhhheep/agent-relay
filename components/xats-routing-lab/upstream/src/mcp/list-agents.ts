import type Database from 'better-sqlite3'
import { AgentsRepo } from '../storage/agents-repo.js'
import { listTmuxPaneIds } from '../daemon/tmux-pane-list.js'
import { toPublicAgentRow, type PublicAgentListRow } from './agent-public-row.js'

// Shared team-scoped agent listing used by BOTH the list_agents MCP tool and the
// GET /api/agents REST route. Kept as a single helper so the two stay in lockstep
// and produce identical output (public rows + process-liveness `online` flag).
export async function listAgentsForTeam(
  db: Database.Database,
  team: string,
  localDevice: string
): Promise<{ agents: PublicAgentListRow[] }> {
  const agents = new AgentsRepo(db)
  const livePanes = await listTmuxPaneIds()
  return {
    agents: agents
      .list({
        team,
        excludeRoles: ['__channel_proxy__'],
        localDevice,
        livePanes,
      })
      .map(toPublicAgentRow),
  }
}

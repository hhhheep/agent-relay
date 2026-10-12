import type Database from 'better-sqlite3'
import type { AgentsRepo } from '../storage/agents-repo.js'

export interface InboxMessage {
  id: string
  event_id: number
  from_team: string
  to_team: string
  from_agent_id: string
  from_name: string | null
  from_device: string | null
  from_role: string | null
  to_agent_id: string | null
  to_role: string | null
  subject: string | null
  body: string
  need_reply: boolean
  sent_at: string
}

export interface InboxResult {
  messages: InboxMessage[]
  has_more: boolean
  last_event_id: number
}

export class GetInboxService {
  constructor(private db: Database.Database, private agents: AgentsRepo) {}

  get(args: { caller: string; since_event_id?: number; limit?: number }): InboxResult {
    const caller = this.agents.findById(args.caller)
    if (!caller) return { messages: [], has_more: false, last_event_id: args.since_event_id ?? 0 }
    const callerTeam = caller.team
    const callerRoleRow = this.db.prepare('SELECT role, last_processed_event_id FROM agents WHERE agent_id=?')
      .get(args.caller) as { role: string; last_processed_event_id: number } | undefined
    const callerRole = callerRoleRow?.role
    const storedCursor = callerRoleRow?.last_processed_event_id ?? 0
    const limit = Math.min(args.limit ?? 50, 200)
    // D1: explicit since_event_id (any number including 0) is read-only; omitted
    // arg (undefined) reads + advances the stored cursor.
    const implicit = args.since_event_id === undefined
    const effectiveSince = implicit ? storedCursor : args.since_event_id!

    // D2: SELECT and the conditional cursor advance happen in the same SQLite
    // transaction so two concurrent default calls cannot both see the same
    // unread tail.
    const tx = this.db.transaction(() => {
      const rows = this.db.prepare(
        `SELECT m.id, m.event_id, m.from_team, m.to_team, m.from_agent_id, m.to_agent_id, m.to_role, m.subject, m.body, m.need_reply, m.sent_at,
                a.role as from_role,
                a.name as from_name,
                a.device as from_device
           FROM messages m
           LEFT JOIN agents a ON a.agent_id = m.from_agent_id
          WHERE m.to_team = ?
            AND m.event_id > ?
            AND ( m.to_agent_id = ? OR (m.to_role IS NOT NULL AND m.to_role = ?) )
          ORDER BY m.event_id ASC
          LIMIT ?`
      ).all(callerTeam, effectiveSince, args.caller, callerRole ?? '__none__', limit + 1) as Array<
        Omit<InboxMessage, 'need_reply'> & { need_reply: number }
      >
      const has_more = rows.length > limit
      const trimmed = (has_more ? rows.slice(0, limit) : rows).map(row => ({
        ...row,
        need_reply: row.need_reply === 1,
      }))
      const last_event_id = trimmed.length > 0 ? trimmed[trimmed.length - 1].event_id : effectiveSince
      if (implicit && last_event_id > storedCursor) {
        this.db.prepare(
          `UPDATE agents
              SET last_processed_event_id = ?
            WHERE agent_id = ? AND last_processed_event_id < ?`
        ).run(last_event_id, args.caller, last_event_id)
      }
      return { messages: trimmed, has_more, last_event_id }
    })
    return tx()
  }
}

import type Database from 'better-sqlite3'

export interface EventRow {
  event_id: number
  from_team: string
  to_team: string
  event_type: string
  actor_agent_id: string | null
  payload: string
  created_at: string
}

export class EventsOutbox {
  constructor(private db: Database.Database) {}

  append(args: {
    from_team: string
    to_team: string
    event_type: string
    actor_agent_id?: string | null
    payload: unknown
  }): number {
    const stmt = this.db.prepare(
      `INSERT INTO events (from_team, to_team, event_type, actor_agent_id, payload, created_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    )
    const info = stmt.run(
      args.from_team,
      args.to_team,
      args.event_type,
      args.actor_agent_id ?? null,
      JSON.stringify(args.payload),
      new Date().toISOString()
    )
    return Number(info.lastInsertRowid)
  }

  since(args: { team: string; since_event_id: number; limit?: number }): EventRow[] {
    const limit = Math.min(args.limit ?? 100, 500)
    return this.db.prepare(
      `SELECT * FROM events WHERE to_team = ? AND event_id > ? ORDER BY event_id ASC LIMIT ?`
    ).all(args.team, args.since_event_id, limit) as EventRow[]
  }
}

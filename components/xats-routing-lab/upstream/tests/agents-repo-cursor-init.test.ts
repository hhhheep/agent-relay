import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-cursor-init-'))

function seedEvents(db: import('better-sqlite3').Database, count: number): number {
  const ts = new Date().toISOString()
  const stmt = db.prepare(
    `INSERT INTO events (from_team, to_team, event_type, actor_agent_id, payload, created_at) VALUES (?,?,?,?,?,?)`
  )
  for (let i = 0; i < count; i++) stmt.run('default', 'default', 'message_sent', null, '{}', ts)
  const max = db.prepare(`SELECT MAX(event_id) AS m FROM events`).get() as { m: number | null }
  return max.m ?? 0
}

function readCursor(db: import('better-sqlite3').Database, agentId: string): number {
  const row = db.prepare(`SELECT last_processed_event_id AS c FROM agents WHERE agent_id=?`).get(agentId) as { c: number }
  return row.c
}

describe('AgentsRepo.register cursor initialisation', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function fresh(): { db: ReturnType<typeof openDb>; repo: AgentsRepo } {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return { db, repo: new AgentsRepo(db) }
  }

  it('fresh INSERT into a non-empty events table starts cursor at MAX(event_id)', () => {
    const { db, repo } = fresh()
    const max = seedEvents(db, 13)
    const r = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    expect('agent_id' in r).toBe(true)
    const id = (r as { agent_id: string }).agent_id
    expect(readCursor(db, id)).toBe(max)
  })

  it('fresh INSERT into an empty events table starts cursor at 0', () => {
    const { db, repo } = fresh()
    const r = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id = (r as { agent_id: string }).agent_id
    expect(readCursor(db, id)).toBe(0)
  })

  it('reuse path preserves an existing non-zero cursor', () => {
    const { db, repo } = fresh()
    const r1 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id = (r1 as { agent_id: string }).agent_id
    db.prepare(`UPDATE agents SET last_processed_event_id=? WHERE agent_id=?`).run(42, id)
    seedEvents(db, 5)
    const r2 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    expect((r2 as { agent_id: string }).agent_id).toBe(id)
    expect(readCursor(db, id)).toBe(42)
  })

  it('role-change path preserves cursor', () => {
    const { db, repo } = fresh()
    const r1 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id = (r1 as { agent_id: string }).agent_id
    db.prepare(`UPDATE agents SET last_processed_event_id=? WHERE agent_id=?`).run(7, id)
    repo.register({ name: 'alice', role: 'frontend', team: 'default', model: 'opus' })
    expect(readCursor(db, id)).toBe(7)
  })
})

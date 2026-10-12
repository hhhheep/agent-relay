import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type Database from 'better-sqlite3'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-mig-ui-pid-'))

function createLegacyAgents(db: Database.Database): void {
  // Legacy schema without claude_ui_pid. Mirrors the columns pre-change.
  db.exec(`CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    client TEXT,
    client_name TEXT,
    team TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    model TEXT,
    registered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_processed_event_id INTEGER NOT NULL DEFAULT 0,
    tmux_pane_id TEXT,
    runtime_ui_pid INTEGER,
    runtime_tty TEXT,
    runtime_verification_mode TEXT,
    runtime_bound_at TEXT,
    channel_session_id TEXT,
    delivery_kind TEXT NOT NULL DEFAULT 'none',
    delivery_payload TEXT
  )`)
  db.exec(`CREATE UNIQUE INDEX agents_identity_idx ON agents(team, name)`)
}

describe('startup migration: claude_ui_pid (6.15)', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('adds claude_ui_pid column to legacy schema', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    const before = (db.pragma('table_info(agents)') as Array<{ name: string }>).map(c => c.name)
    expect(before).not.toContain('claude_ui_pid')

    applySchema(db)

    const after = db.pragma('table_info(agents)') as Array<{ name: string; type: string; notnull: number }>
    const col = after.find(c => c.name === 'claude_ui_pid')
    expect(col).toBeDefined()
    expect(col?.type).toBe('INTEGER')
    expect(col?.notnull).toBe(0)
    db.close()
  })

  it('is idempotent: running applySchema twice does not error or re-ALTER', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    applySchema(db)

    const execSpy = (arg: string) => /ADD COLUMN claude_ui_pid/i.test(arg)
    let altered = 0
    const originalExec = db.exec.bind(db)
    db.exec = ((sql: string) => {
      if (execSpy(sql)) altered += 1
      return originalExec(sql)
    }) as typeof db.exec

    applySchema(db)
    expect(altered).toBe(0)
    db.close()
  })

  it('existing rows have claude_ui_pid=NULL after migration, data preserved', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    db.prepare(
      `INSERT INTO agents (agent_id, team, role, name, model, registered_at, last_seen_at)
       VALUES ('x1', 'default', 'worker', 'alice', 'opus', ?, ?)`
    ).run(new Date().toISOString(), new Date().toISOString())

    applySchema(db)

    const row = db.prepare(
      `SELECT name, claude_ui_pid FROM agents WHERE agent_id='x1'`
    ).get() as { name: string; claude_ui_pid: number | null }
    expect(row.name).toBe('alice')
    expect(row.claude_ui_pid).toBeNull()
    db.close()
  })
})

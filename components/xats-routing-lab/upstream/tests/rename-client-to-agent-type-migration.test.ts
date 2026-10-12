import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type Database from 'better-sqlite3'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-mig-rename-'))

function createPreRenameAgents(db: Database.Database): void {
  // Pre-rename schema: has `client` and `client_name` columns. Mirrors the
  // shape that legacy databases (0.4.x) carry on disk.
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
    claude_ui_pid INTEGER,
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

describe('startup migration: client -> agent_type rename', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('renames client and client_name on a legacy schema while preserving data', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createPreRenameAgents(db)
    const now = new Date().toISOString()
    db.prepare(
      `INSERT INTO agents (agent_id, client, client_name, team, role, name, model, registered_at, last_seen_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run('a1', 'claude-code', null, 'default', 'worker', 'alice', 'opus', now, now)
    db.prepare(
      `INSERT INTO agents (agent_id, client, client_name, team, role, name, model, registered_at, last_seen_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run('a2', 'custom', 'cursor', 'default', 'worker', 'bob', 'gpt', now, now)

    applySchema(db)

    const cols = (db.pragma('table_info(agents)') as Array<{ name: string }>).map(c => c.name)
    expect(cols).toContain('agent_type')
    expect(cols).toContain('agent_type_name')
    expect(cols).not.toContain('client')
    expect(cols).not.toContain('client_name')

    const rowAlice = db.prepare(
      `SELECT agent_type, agent_type_name FROM agents WHERE agent_id='a1'`
    ).get() as { agent_type: string | null; agent_type_name: string | null }
    expect(rowAlice.agent_type).toBe('claude-code')
    expect(rowAlice.agent_type_name).toBeNull()

    const rowBob = db.prepare(
      `SELECT agent_type, agent_type_name FROM agents WHERE agent_id='a2'`
    ).get() as { agent_type: string | null; agent_type_name: string | null }
    expect(rowBob.agent_type).toBe('custom')
    expect(rowBob.agent_type_name).toBe('cursor')

    db.close()
  })

  it('is idempotent on already-renamed schema (no extra ALTER issued)', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createPreRenameAgents(db)
    applySchema(db)

    let renameCount = 0
    const originalExec = db.exec.bind(db)
    db.exec = ((sql: string) => {
      if (/RENAME COLUMN/i.test(sql)) renameCount += 1
      return originalExec(sql)
    }) as typeof db.exec

    applySchema(db)
    expect(renameCount).toBe(0)
    db.close()
  })

  it('fresh database starts with renamed columns and no rename ALTER runs', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))

    let renameCount = 0
    const originalExec = db.exec.bind(db)
    db.exec = ((sql: string) => {
      if (/RENAME COLUMN/i.test(sql)) renameCount += 1
      return originalExec(sql)
    }) as typeof db.exec

    applySchema(db)
    expect(renameCount).toBe(0)

    const cols = (db.pragma('table_info(agents)') as Array<{ name: string }>).map(c => c.name)
    expect(cols).toContain('agent_type')
    expect(cols).toContain('agent_type_name')
    expect(cols).not.toContain('client')
    expect(cols).not.toContain('client_name')
    db.close()
  })
})

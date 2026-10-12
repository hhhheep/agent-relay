import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type Database from 'better-sqlite3'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-device-mig-'))

function createLegacyAgents(db: Database.Database): void {
  db.exec(`CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    agent_type TEXT,
    agent_type_name TEXT,
    team TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    model TEXT,
    registered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_processed_event_id INTEGER NOT NULL DEFAULT 0,
    tmux_pane_id TEXT,
    channel_session_id TEXT,
    delivery_kind TEXT NOT NULL DEFAULT 'none',
    delivery_payload TEXT
  )`)
  db.exec(`CREATE UNIQUE INDEX agents_identity_idx ON agents(team, name)`)
}

describe('agents device startup migration', () => {
  const dirs: string[] = []
  afterEach(() => {
    dirs.forEach(d => rmSync(d, { recursive: true, force: true }))
    dirs.length = 0
  })

  it('adds device and remote_addr, backfills, and rebuilds identity index', () => {
    const dir = tmp()
    dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    db.prepare(
      `INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    ).run('a1', 'default', 'worker', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')

    applySchema(db, { localDevice: 'jt-laptop' })

    const row = db.prepare(
      `SELECT device, remote_addr FROM agents WHERE agent_id='a1'`
    ).get() as { device: string; remote_addr: string | null }
    expect(row).toEqual({ device: 'jt-laptop', remote_addr: null })
    const info = db.pragma('index_info(agents_identity_idx)') as Array<{ seqno: number; name: string }>
    expect(info.sort((a, b) => a.seqno - b.seqno).map(row => row.name))
      .toEqual(['device', 'team', 'name'])
  })

  it('is idempotent once device and remote_addr exist with the widened index', () => {
    const dir = tmp()
    dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    applySchema(db, { localDevice: 'jt' })

    let altered = 0
    let dropped = 0
    const originalExec = db.exec.bind(db)
    db.exec = ((sql: string) => {
      if (/ADD COLUMN (device|remote_addr)/i.test(sql)) altered += 1
      if (/DROP INDEX IF EXISTS agents_identity_idx/i.test(sql)) dropped += 1
      return originalExec(sql)
    }) as typeof db.exec

    applySchema(db, { localDevice: 'jt' })
    expect(altered).toBe(0)
    expect(dropped).toBe(0)
  })

  it('aborts before backfill when legacy names contain colon', () => {
    const dir = tmp()
    dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    db.prepare(
      `INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    ).run('a1', 'default', 'worker', 'odd:name', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')

    expect(() => applySchema(db, { localDevice: 'jt' })).toThrow(
      /offending row \(default, odd:name\)/
    )
    const cols = db.pragma('table_info(agents)') as Array<{ name: string }>
    expect(cols.map(col => col.name)).not.toContain('device')
  })
})

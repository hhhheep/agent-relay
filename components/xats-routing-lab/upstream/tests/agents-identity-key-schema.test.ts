import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type Database from 'better-sqlite3'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-identity-key-'))

function createLegacyAgents(db: Database.Database): void {
  db.exec(`CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    agent_type TEXT,
    agent_type_name TEXT,
    device TEXT NOT NULL,
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
    delivery_payload TEXT,
    remote_addr TEXT
  )`)
  db.exec(`CREATE UNIQUE INDEX agents_identity_idx ON agents(device, team, name)`)
}

function insertRow(db: Database.Database, agent_id: string, name: string): void {
  db.prepare(
    `INSERT INTO agents (agent_id, device, team, role, name, registered_at, last_seen_at)
     VALUES (?, 'local', 'default', 'worker', ?, ?, ?)`
  ).run(agent_id, name, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
}

function identityKeyIndex(
  db: Database.Database
): { unique: number; columns: string[] } | undefined {
  const indexes = db.pragma('index_list(agents)') as Array<{
    name: string
    unique: number
  }>
  const found = indexes.find(i => i.name === 'agents_identity_key_idx')
  if (!found) return undefined
  const info = db.pragma('index_info(agents_identity_key_idx)') as Array<{
    seqno: number
    name: string
  }>
  return {
    unique: found.unique,
    columns: info.sort((a, b) => a.seqno - b.seqno).map(i => i.name),
  }
}

describe('agents identity_key column and index', () => {
  const dirs: string[] = []
  afterEach(() => {
    dirs.forEach(d => rmSync(d, { recursive: true, force: true }))
    dirs.length = 0
  })

  it('a fresh database carries the column and the unique index', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const cols = db.pragma('table_info(agents)') as Array<{
      name: string
      type: string
      notnull: number
    }>
    const col = cols.find(c => c.name === 'identity_key')
    expect(col).toBeDefined()
    expect(col?.type).toBe('TEXT')
    expect(col?.notnull).toBe(0)
    expect(identityKeyIndex(db)).toEqual({
      unique: 1,
      columns: ['device', 'identity_key'],
    })
    db.close()
  })

  it('heals a legacy database and leaves pre-existing rows at NULL', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    insertRow(db, 'a1', 'alice')

    applySchema(db, { localDevice: 'local' })

    expect(identityKeyIndex(db)).toEqual({
      unique: 1,
      columns: ['device', 'identity_key'],
    })
    const row = db.prepare(
      `SELECT identity_key FROM agents WHERE agent_id='a1'`
    ).get() as { identity_key: string | null }
    expect(row.identity_key).toBeNull()
    db.close()
  })

  it('is idempotent on a second startup against the same database', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    createLegacyAgents(db)
    applySchema(db, { localDevice: 'local' })

    let altered = 0
    let created = 0
    const originalExec = db.exec.bind(db)
    db.exec = ((sql: string) => {
      if (/ADD COLUMN identity_key/i.test(sql)) altered += 1
      if (/CREATE UNIQUE INDEX agents_identity_key_idx/i.test(sql)) created += 1
      return originalExec(sql)
    }) as typeof db.exec

    expect(() => applySchema(db, { localDevice: 'local' })).not.toThrow()
    expect(altered).toBe(0)
    expect(created).toBe(0)
    db.close()
  })

  it('lets many rows on one device hold a null key', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    insertRow(db, 'a1', 'alice')
    insertRow(db, 'a2', 'bob')
    expect(() => insertRow(db, 'a3', 'carol')).not.toThrow()
    const count = db.prepare(
      `SELECT COUNT(*) AS c FROM agents WHERE identity_key IS NULL`
    ).get() as { c: number }
    expect(count.c).toBe(3)
    db.close()
  })

  it('rejects the same key twice on one device but allows it across devices', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const insert = db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, registered_at, last_seen_at, identity_key)
       VALUES (?, ?, 'default', 'worker', ?, ?, ?, ?)`
    )
    insert.run('a1', 'local', 'alice', 'T', 'T', 'K')
    expect(() => insert.run('a2', 'local', 'bob', 'T', 'T', 'K')).toThrow()
    expect(() => insert.run('a3', 'gx', 'carol', 'T', 'T', 'K')).not.toThrow()
    db.close()
  })
})

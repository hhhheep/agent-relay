import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import Database from 'better-sqlite3'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-mig-'))

function createOldSchemaAgents(db: Database.Database): void {
  db.exec(`CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    model TEXT,
    registered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_processed_event_id INTEGER NOT NULL DEFAULT 0,
    tmux_pane_id TEXT,
    channel_session_id TEXT
  )`)
  db.exec(`CREATE UNIQUE INDEX agents_identity_idx ON agents(team, name)`)
}

describe('migration: delivery columns', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('adds delivery_kind and delivery_payload to a pre-existing old-schema agents table', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)

    const pre = db.pragma('table_info(agents)') as Array<{ name: string }>
    const preNames = pre.map(c => c.name)
    expect(preNames).not.toContain('delivery_kind')
    expect(preNames).not.toContain('delivery_payload')

    applySchema(db)

    const cols = db.pragma('table_info(agents)') as Array<{ name: string; type: string; notnull: number; dflt_value: string | null }>
    const deliveryKind = cols.find(c => c.name === 'delivery_kind')
    expect(deliveryKind).toBeDefined()
    expect(deliveryKind?.type).toBe('TEXT')
    expect(deliveryKind?.notnull).toBe(1)
    expect(deliveryKind?.dflt_value).toBe("'none'")
    const deliveryPayload = cols.find(c => c.name === 'delivery_payload')
    expect(deliveryPayload).toBeDefined()
    expect(deliveryPayload?.type).toBe('TEXT')
    expect(deliveryPayload?.notnull).toBe(0)
    db.close()
  })

  it('is idempotent: running applySchema twice does not error and leaves columns intact', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)

    applySchema(db)
    const firstCols = (db.pragma('table_info(agents)') as Array<{ name: string }>).map(c => c.name).sort()

    expect(() => applySchema(db)).not.toThrow()

    const secondCols = (db.pragma('table_info(agents)') as Array<{ name: string }>).map(c => c.name).sort()
    expect(secondCols).toEqual(firstCols)
    expect(secondCols).toContain('delivery_kind')
    expect(secondCols).toContain('delivery_payload')
    db.close()
  })

  it('preserves existing row data when migrating (no data loss)', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a1', 'teamA', 'dev', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'csid-abc')`).run()

    applySchema(db)

    const row = db.prepare(`SELECT agent_id, team, name, channel_session_id FROM agents WHERE agent_id = ?`).get('a1') as { agent_id: string; team: string; name: string; channel_session_id: string | null }
    expect(row.agent_id).toBe('a1')
    expect(row.team).toBe('teamA')
    expect(row.name).toBe('alice')
    expect(row.channel_session_id).toBe('csid-abc')
    db.close()
  })

  it('backfills delivery_kind/delivery_payload from channel_session_id during migration', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a1', 'teamA', 'dev', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'csid-abc')`).run()
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a2', 'teamA', 'dev', 'bob', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)`).run()

    applySchema(db)

    const row1 = db.prepare(`SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id = ?`).get('a1') as { delivery_kind: string; delivery_payload: string | null }
    expect(row1.delivery_kind).toBe('claude-channel')
    expect(row1.delivery_payload).not.toBeNull()
    expect(JSON.parse(row1.delivery_payload as string)).toEqual({ channel_session_id: 'csid-abc' })

    const row2 = db.prepare(`SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id = ?`).get('a2') as { delivery_kind: string; delivery_payload: string | null }
    expect(row2.delivery_kind).toBe('none')
    expect(row2.delivery_payload).toBeNull()
    db.close()
  })

  it('leaves legacy channel_session_id column and its values untouched after migration and backfill', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a1', 'teamA', 'dev', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'csid-abc')`).run()
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a2', 'teamA', 'dev', 'bob', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)`).run()

    applySchema(db)

    const cols = db.pragma('table_info(agents)') as Array<{ name: string; type: string; notnull: number; dflt_value: string | null }>
    const legacy = cols.find(c => c.name === 'channel_session_id')
    expect(legacy).toBeDefined()
    expect(legacy?.type).toBe('TEXT')
    expect(legacy?.notnull).toBe(0)

    const row1 = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id = ?`).get('a1') as { channel_session_id: string | null }
    expect(row1.channel_session_id).toBe('csid-abc')
    const row2 = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id = ?`).get('a2') as { channel_session_id: string | null }
    expect(row2.channel_session_id).toBeNull()

    applySchema(db)
    const row1After = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id = ?`).get('a1') as { channel_session_id: string | null }
    expect(row1After.channel_session_id).toBe('csid-abc')
    db.close()
  })

  it('backfill is idempotent: running applySchema again does not overwrite existing delivery data', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a1', 'teamA', 'dev', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'csid-abc')`).run()

    applySchema(db)
    const first = db.prepare(`SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id = ?`).get('a1') as { delivery_kind: string; delivery_payload: string | null }
    // Simulate: user later sets delivery to 'none' manually (or different kind); second applySchema must not re-backfill
    db.prepare(`UPDATE agents SET delivery_kind = 'none', delivery_payload = NULL WHERE agent_id = 'a1'`).run()

    applySchema(db)
    const second = db.prepare(`SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id = ?`).get('a1') as { delivery_kind: string; delivery_payload: string | null }
    // backfill only runs as part of the ADD COLUMN migration; second run must not re-trigger
    expect(first.delivery_kind).toBe('claude-channel')
    expect(second.delivery_kind).toBe('none')
    expect(second.delivery_payload).toBeNull()
    db.close()
  })

  it('second startup issues no ALTER for delivery columns and preserves existing values', () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    createOldSchemaAgents(db)
    db.prepare(`INSERT INTO agents (agent_id, team, role, name, registered_at, last_seen_at, channel_session_id)
      VALUES ('a1', 'teamA', 'dev', 'alice', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'csid-abc')`).run()

    applySchema(db)
    db.prepare(
      `UPDATE agents
       SET delivery_kind='claude-channel',
           delivery_payload='{"channel_session_id":"csid-custom"}'
       WHERE agent_id='a1'`
    ).run()

    const execSpy = vi.spyOn(db, 'exec')
    applySchema(db)

    const alterCalls = execSpy.mock.calls
      .map(([sql]) => sql)
      .filter(sql => sql.includes('ALTER TABLE agents ADD COLUMN'))
    expect(alterCalls).toEqual([])

    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id='a1'`
    ).get() as {
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(row.delivery_payload).toBe('{"channel_session_id":"csid-custom"}')
    db.close()
  })
})

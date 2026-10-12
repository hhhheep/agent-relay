import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-schema-fromto-'))

interface ColInfo { name: string; notnull: number; type: string }
interface IndexInfo { name: string }

describe('events + messages schema uses from_team and to_team', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function freshDb(): ReturnType<typeof openDb> {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return db
  }

  it('events table has from_team and to_team NOT NULL and no legacy team column', () => {
    const db = freshDb()
    const cols = db.prepare(`PRAGMA table_info('events')`).all() as ColInfo[]
    const names = cols.map(c => c.name)
    expect(names).toContain('from_team')
    expect(names).toContain('to_team')
    expect(names).not.toContain('team')
    expect(cols.find(c => c.name === 'from_team')!.notnull).toBe(1)
    expect(cols.find(c => c.name === 'to_team')!.notnull).toBe(1)
  })

  it('events has idx_events_from_team_eventid and idx_events_to_team_eventid, not legacy index', () => {
    const db = freshDb()
    const idx = db.prepare(`PRAGMA index_list('events')`).all() as IndexInfo[]
    const names = idx.map(i => i.name)
    expect(names).toContain('idx_events_from_team_eventid')
    expect(names).toContain('idx_events_to_team_eventid')
    expect(names).not.toContain('idx_events_team_eventid')
  })

  it('messages table has from_team and to_team NOT NULL and no legacy team column', () => {
    const db = freshDb()
    const cols = db.prepare(`PRAGMA table_info('messages')`).all() as ColInfo[]
    const names = cols.map(c => c.name)
    expect(names).toContain('from_team')
    expect(names).toContain('to_team')
    expect(names).not.toContain('team')
    expect(cols.find(c => c.name === 'from_team')!.notnull).toBe(1)
    expect(cols.find(c => c.name === 'to_team')!.notnull).toBe(1)
  })

  it('messages table has need_reply NOT NULL with default true', () => {
    const db = freshDb()
    const cols = db.prepare(`PRAGMA table_info('messages')`).all() as
      Array<ColInfo & { dflt_value: string | null }>
    const col = cols.find(c => c.name === 'need_reply')
    expect(col).toBeTruthy()
    expect(col!.notnull).toBe(1)
    expect(col!.dflt_value).toBe('1')
  })

  it('INSERT without from_team/to_team throws NOT NULL constraint error', () => {
    const db = freshDb()
    expect(() => {
      db.prepare(`INSERT INTO events (event_type, actor_agent_id, payload, created_at) VALUES (?,?,?,?)`)
        .run('x', null, '{}', new Date().toISOString())
    }).toThrow(/NOT NULL constraint failed/)
  })

  it('messages.event_id has foreign key reference to events.event_id', () => {
    const db = freshDb()
    const fks = db.prepare(`PRAGMA foreign_key_list('messages')`).all() as
      Array<{ table: string; from: string; to: string }>
    const evFk = fks.find(f => f.table === 'events' && f.from === 'event_id')
    expect(evFk).toBeTruthy()
    expect(evFk!.to).toBe('event_id')
  })
})

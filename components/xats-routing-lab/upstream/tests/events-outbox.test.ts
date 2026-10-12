import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('events outbox', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('creates events table and composite index', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const cols = db.pragma('table_info(events)') as Array<{ name: string }>
    const names = cols.map(c => c.name).sort()
    expect(names).toEqual(['actor_agent_id','created_at','event_id','event_type','from_team','payload','to_team'])
    const idx = db.pragma('index_list(events)') as Array<{ name: string }>
    expect(idx.some(i => i.name === 'idx_events_to_team_eventid')).toBe(true)
    db.close()
  })

  it('append returns monotonically increasing ids within a team', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const out = new EventsOutbox(db)
    const a = out.append({ from_team: 'default', to_team: 'default', event_type: 'x', payload: {} })
    const b = out.append({ from_team: 'default', to_team: 'default', event_type: 'x', payload: {} })
    expect(b).toBeGreaterThan(a)
    db.close()
  })

  it('since filters by to_team and cursor', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const out = new EventsOutbox(db)
    for (let i = 0; i < 5; i++) out.append({ from_team: 'default', to_team: 'default', event_type: 'a', payload: { i } })
    for (let i = 0; i < 5; i++) out.append({ from_team: 'other', to_team: 'other', event_type: 'b', payload: { i } })
    const rows = out.since({ team: 'default', since_event_id: 2, limit: 10 })
    expect(rows.map(r => r.event_id)).toEqual([3, 4, 5])
    db.close()
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-append-'))

describe('EventsOutbox.append with from_team and to_team', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function fresh(): { db: ReturnType<typeof openDb>; outbox: EventsOutbox } {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return { db, outbox: new EventsOutbox(db) }
  }

  it('two same-team appends return strictly increasing ids', () => {
    const { outbox } = fresh()
    const a = outbox.append({ from_team: 'default', to_team: 'default', event_type: 'x', payload: {} })
    const b = outbox.append({ from_team: 'default', to_team: 'default', event_type: 'x', payload: {} })
    expect(b).toBeGreaterThan(a)
  })

  it('cross-team append writes differing from_team and to_team', () => {
    const { db, outbox } = fresh()
    const id = outbox.append({
      from_team: 'alpha', to_team: 'beta',
      event_type: 'message_sent', actor_agent_id: 'sess-A', payload: { hi: 1 }
    })
    const row = db.prepare(`SELECT from_team, to_team, actor_agent_id FROM events WHERE event_id=?`)
      .get(id) as { from_team: string; to_team: string; actor_agent_id: string }
    expect(row.from_team).toBe('alpha')
    expect(row.to_team).toBe('beta')
    expect(row.actor_agent_id).toBe('sess-A')
  })
})

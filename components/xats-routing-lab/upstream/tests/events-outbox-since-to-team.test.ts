import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-since-'))

describe('EventsOutbox.since filters by to_team', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  it('returns only events with to_team matching, excluding outbound cross-team', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const outbox = new EventsOutbox(db)

    outbox.append({ from_team: 'alpha', to_team: 'alpha', event_type: 'a', payload: {} })  // 1
    outbox.append({ from_team: 'alpha', to_team: 'alpha', event_type: 'a', payload: {} })  // 2
    outbox.append({ from_team: 'alpha', to_team: 'beta',  event_type: 'message_sent', payload: {} })  // 3 outbound
    outbox.append({ from_team: 'beta',  to_team: 'alpha', event_type: 'message_sent', payload: {} })  // 4 inbound
    outbox.append({ from_team: 'alpha', to_team: 'alpha', event_type: 'a', payload: {} })  // 5

    const rows = outbox.since({ team: 'alpha', since_event_id: 0, limit: 10 })
    const ids = rows.map(r => r.event_id)
    expect(ids).toEqual([1, 2, 4, 5])
  })

  it('does not leak events targeting other teams', () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const outbox = new EventsOutbox(db)

    for (let i = 0; i < 5; i++) {
      outbox.append({ from_team: 'beta', to_team: 'beta', event_type: 'x', payload: {} })
    }
    const rows = outbox.since({ team: 'default', since_event_id: 0, limit: 10 })
    expect(rows.length).toBe(0)
  })
})

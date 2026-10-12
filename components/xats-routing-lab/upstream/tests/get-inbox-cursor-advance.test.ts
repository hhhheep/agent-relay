import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { GetInboxService } from '../src/mcp/get-inbox.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-inbox-cursor-'))

function readCursor(db: import('better-sqlite3').Database, agentId: string): number {
  const row = db.prepare(`SELECT last_processed_event_id AS c FROM agents WHERE agent_id=?`).get(agentId) as { c: number }
  return row.c
}

describe('get_inbox stateful cursor', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  async function setup(n: number): Promise<{ db: ReturnType<typeof openDb>; svc: GetInboxService }> {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'backend', name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'frontend', name: 'B' })
    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    for (let i = 0; i < n; i++) await send.send({ from: 'A', to_agent_id: 'B', body: `msg-${i}`, auto_poke: false })
    return { db, svc: new GetInboxService(db, agents) }
  }

  it('default call advances stored cursor to last returned event_id', async () => {
    const { db, svc } = await setup(5)
    expect(readCursor(db, 'B')).toBe(0)
    const r = svc.get({ caller: 'B' })
    expect(r.messages.length).toBe(5)
    expect(r.last_event_id).toBe(r.messages[r.messages.length - 1].event_id)
    expect(readCursor(db, 'B')).toBe(r.last_event_id)
  })

  it('two consecutive default calls return new tail only', async () => {
    const { db, svc } = await setup(3)
    const r1 = svc.get({ caller: 'B' })
    expect(r1.messages.length).toBe(3)
    const after = readCursor(db, 'B')
    // Send two more
    const agents = new AgentsRepo(db)
    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    await send.send({ from: 'A', to_agent_id: 'B', body: 'x4', auto_poke: false })
    await send.send({ from: 'A', to_agent_id: 'B', body: 'x5', auto_poke: false })
    const r2 = svc.get({ caller: 'B' })
    expect(r2.messages.length).toBe(2)
    expect(r2.messages[0].event_id).toBeGreaterThan(after)
  })

  it('default call with no new mail leaves cursor unchanged', async () => {
    const { db, svc } = await setup(2)
    svc.get({ caller: 'B' })
    const cursor = readCursor(db, 'B')
    const r = svc.get({ caller: 'B' })
    expect(r.messages).toHaveLength(0)
    expect(r.last_event_id).toBe(cursor)
    expect(readCursor(db, 'B')).toBe(cursor)
  })

  it('explicit since_event_id: 0 returns history without advancing the stored cursor', async () => {
    const { db, svc } = await setup(4)
    // Pre-set cursor to a non-zero so we can detect advancement
    db.prepare(`UPDATE agents SET last_processed_event_id=? WHERE agent_id=?`).run(50, 'B')
    const r = svc.get({ caller: 'B', since_event_id: 0 })
    expect(r.messages.length).toBe(4)
    expect(readCursor(db, 'B')).toBe(50)
  })

  it('explicit since_event_id: N higher than stored cursor returns from N without regressing or advancing the stored cursor', async () => {
    const { db, svc } = await setup(5)
    db.prepare(`UPDATE agents SET last_processed_event_id=? WHERE agent_id=?`).run(10, 'B')
    // Pick a since_event_id higher than current stored cursor but lower than middle of messages
    const all = await Promise.resolve(svc.get({ caller: 'B', since_event_id: 0 }))
    const mid = all.messages[2].event_id
    const r = svc.get({ caller: 'B', since_event_id: mid })
    expect(r.messages.length).toBe(2)
    expect(r.messages[0].event_id).toBeGreaterThan(mid)
    expect(readCursor(db, 'B')).toBe(10)
  })

  it('pagination (limit < total) advances cursor to the last returned event_id', async () => {
    const { db, svc } = await setup(120)
    const r = svc.get({ caller: 'B', limit: 50 })
    expect(r.messages.length).toBe(50)
    expect(r.has_more).toBe(true)
    const lastReturned = r.messages[49].event_id
    expect(r.last_event_id).toBe(lastReturned)
    expect(readCursor(db, 'B')).toBe(lastReturned)
  })
})

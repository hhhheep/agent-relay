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

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('get_inbox', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  async function setup(n: number) {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'backend' , name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'frontend' , name: 'B' })
    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    for (let i = 0; i < n; i++) await send.send({ from: 'A', to_agent_id: 'B', body: `msg-${i}`, auto_poke: false })
    return new GetInboxService(db, agents)
  }

  it('returns all messages with has_more=false when under limit', async () => {
    const svc = await setup(5)
    const r = svc.get({ caller: 'B', since_event_id: 0 })
    expect(r.messages.length).toBe(5)
    expect(r.has_more).toBe(false)
    expect(r.last_event_id).toBe(r.messages[r.messages.length - 1].event_id)
  })

  it('sets has_more=true when more rows exist beyond limit', async () => {
    const svc = await setup(120)
    const r = svc.get({ caller: 'B', since_event_id: 0, limit: 50 })
    expect(r.messages.length).toBe(50)
    expect(r.has_more).toBe(true)
  })

  it('surfaces from_name and from_device on each message so the reader can reconstruct name:device replies', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'backend', name: 'alice', device: 'jt' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'frontend', name: 'bob', device: 'mb-neo' })
    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    await send.send({ from: 'A', to_agent_id: 'B', body: 'hi', auto_poke: false })
    const svc = new GetInboxService(db, agents)
    const r = svc.get({ caller: 'B', since_event_id: 0 })
    expect(r.messages.length).toBe(1)
    const msg = r.messages[0]
    expect(msg.from_agent_id).toBe('A')
    expect(msg.from_name).toBe('alice')
    expect(msg.from_device).toBe('jt')
    expect(msg.from_role).toBe('backend')
  })

  it('applies since_event_id cursor', async () => {
    const svc = await setup(10)
    const first = svc.get({ caller: 'B', since_event_id: 0, limit: 3 })
    const cursor = first.last_event_id
    const next = svc.get({ caller: 'B', since_event_id: cursor, limit: 3 })
    expect(next.messages.length).toBe(3)
    expect(next.messages[0].event_id).toBeGreaterThan(cursor)
  })

  it('returns need_reply as boolean for each message', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'backend' , name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'frontend' , name: 'B' })
    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    await send.send({
      from: 'A',
      to_agent_id: 'B',
      body: 'FYI',
      auto_poke: false,
      need_reply: false,
    })
    const svc = new GetInboxService(db, agents)
    const r = svc.get({ caller: 'B', since_event_id: 0 })
    expect(r.messages).toHaveLength(1)
    expect(r.messages[0].need_reply).toBe(false)
  })

  function setupInbox() {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    const svc = new GetInboxService(db, agents)
    return { svc, db, cleanup: () => {} }
  }

  it('returns cross-team inbound messages by to_team filter', async () => {
    const { svc, db, cleanup } = setupInbox()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'beta' })
    const event_id = new EventsOutbox(db).append({
      from_team: 'alpha', to_team: 'beta',
      event_type: 'message_sent', actor_agent_id: 'A',
      payload: { recipients: ['B'], subject: null, to_role: null }
    })
    db.prepare(
      `INSERT INTO messages (id, event_id, from_team, to_team, from_agent_id, to_agent_id, to_role, subject, body, sent_at)
       VALUES (?, ?, 'alpha', 'beta', 'A', 'B', null, null, 'cross', ?)`
    ).run('mid-1', event_id, new Date().toISOString())

    const resp = await svc.get({ caller: 'B', since_event_id: 0, limit: 50 })
    expect(resp.messages).toHaveLength(1)
    expect(resp.messages[0].from_team).toBe('alpha')
    expect(resp.messages[0].to_team).toBe('beta')
    expect(resp.messages[0].from_agent_id).toBe('A')
    cleanup()
  })

  it('does not return a message whose to_team does not match caller team', async () => {
    const { svc, db, cleanup } = setupInbox()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'beta' })
    const event_id = new EventsOutbox(db).append({
      from_team: 'alpha', to_team: 'gamma', event_type: 'message_sent', actor_agent_id: 'A', payload: {}
    })
    db.prepare(
      `INSERT INTO messages (id, event_id, from_team, to_team, from_agent_id, to_agent_id, to_role, subject, body, sent_at)
       VALUES ('m', ?, 'alpha', 'gamma', 'A', 'B', null, null, 'leaked?', ?)`
    ).run(event_id, new Date().toISOString())
    const resp = await svc.get({ caller: 'B', since_event_id: 0, limit: 50 })
    expect(resp.messages).toHaveLength(0)
    cleanup()
  })
})

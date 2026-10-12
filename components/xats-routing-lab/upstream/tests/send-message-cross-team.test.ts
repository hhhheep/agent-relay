import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-sm-cross-'))

describe('send_message cross-team', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function setup(): { svc: SendMessageService; db: ReturnType<typeof openDb> } {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const svc = new SendMessageService(db, new AgentsRepo(db), new EventsOutbox(db))
    return { svc, db }
  }

  it('cross-team delivery succeeds when to_team matches recipient team', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'beta' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'B', to_team: 'beta', body: 'hi', auto_poke: false })
    expect('error' in resp).toBe(false)
    const m = db.prepare(`SELECT from_team, to_team, event_id FROM messages WHERE to_agent_id='B'`).get() as
      { from_team: string; to_team: string; event_id: number }
    expect(m.from_team).toBe('alpha')
    expect(m.to_team).toBe('beta')
    const e = db.prepare(`SELECT from_team, to_team FROM events WHERE event_id=?`).get(m.event_id) as
      { from_team: string; to_team: string }
    expect(e.from_team).toBe('alpha')
    expect(e.to_team).toBe('beta')
  })

  it('to_team equal to caller team is identical to omitted', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'alpha' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'B', to_team: 'alpha', body: 'hi', auto_poke: false })
    expect('error' in resp).toBe(false)
    const m = db.prepare(`SELECT from_team, to_team FROM messages WHERE to_agent_id='B'`).get() as
      { from_team: string; to_team: string }
    expect(m.from_team).toBe('alpha')
    expect(m.to_team).toBe('alpha')
  })

  it('returns unknown_recipient when to_team does not match recipient actual team', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'beta' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'B', to_team: 'gamma', body: 'hi' })
    expect(resp).toEqual({ error: 'unknown_recipient' })
    const rows = db.prepare(`SELECT * FROM events WHERE event_type='message_sent'`).all()
    expect(rows.length).toBe(0)
  })

  it('returns unknown_recipient when to_agent_id does not exist anywhere', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'ghost', to_team: 'beta', body: 'hi' })
    expect(resp).toEqual({ error: 'unknown_recipient' })
  })
})

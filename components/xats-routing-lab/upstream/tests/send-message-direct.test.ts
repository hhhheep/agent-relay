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

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-'))

describe('send_message direct', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  function setupService() {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    const svc = new SendMessageService(db, agents, new EventsOutbox(db))
    return { db, svc, cleanup: () => { /* cleanup handled by afterEach */ } }
  }

  it('rejects when to_agent_id is unknown in caller team', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'default', role: 'backend', name: 'A' })
    const r = await svc.send({ from: 'A', to_agent_id: 'Z', body: 'x' })
    expect(r).toEqual({ error: 'unknown_recipient' })
    cleanup()
  })

  it('creates paired event and message rows on success', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'default', role: 'backend', name: 'A' })
    insertAgent(db, { agent_id: 'B', team: 'default', role: 'frontend', name: 'B' })
    const r = await svc.send({ from: 'A', to_agent_id: 'B', body: 'hi', auto_poke: false })
    if ('error' in r) throw new Error('expected success')
    expect(r.recipients).toEqual(['B'])
    expect(r.event_id).toBeGreaterThan(0)
    const ev = db.prepare('SELECT event_type, event_id FROM events WHERE event_id=?').get(r.event_id) as
      { event_type: string; event_id: number }
    expect(ev.event_type).toBe('message_sent')
    const msg = db.prepare('SELECT event_id, body FROM messages WHERE id=?').get(r.message_id) as
      { event_id: number; body: string }
    expect(msg.event_id).toBe(r.event_id)
    expect(msg.body).toBe('hi')
    cleanup()
  })

  it('defaults need_reply to true', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'default', role: 'backend', name: 'A' })
    insertAgent(db, { agent_id: 'B', team: 'default', role: 'frontend', name: 'B' })
    const r = await svc.send({ from: 'A', to_agent_id: 'B', body: 'question', auto_poke: false })
    if ('error' in r) throw new Error('expected success')
    const msg = db.prepare('SELECT need_reply FROM messages WHERE id=?').get(r.message_id) as
      { need_reply: number }
    expect(msg.need_reply).toBe(1)
    cleanup()
  })

  it('persists explicit need_reply=false', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'default', role: 'backend', name: 'A' })
    insertAgent(db, { agent_id: 'B', team: 'default', role: 'frontend', name: 'B' })
    const r = await svc.send({
      from: 'A',
      to_agent_id: 'B',
      body: 'FYI',
      auto_poke: false,
      need_reply: false,
    })
    if ('error' in r) throw new Error('expected success')
    const msg = db.prepare('SELECT need_reply FROM messages WHERE id=?').get(r.message_id) as
      { need_reply: number }
    expect(msg.need_reply).toBe(0)
    cleanup()
  })

  it('same-team send writes from_team=to_team=caller.team, paired events row matches', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'default', role: 'r' })
    insertAgent(db, { agent_id: 'B', team: 'default', role: 'r' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'B', body: 'hi', auto_poke: false })
    expect('error' in resp).toBe(false)
    const m = db.prepare(`SELECT from_team, to_team, event_id FROM messages WHERE to_agent_id='B'`).get() as
      { from_team: string; to_team: string; event_id: number }
    expect(m.from_team).toBe('default')
    expect(m.to_team).toBe('default')
    const e = db.prepare(`SELECT from_team, to_team FROM events WHERE event_id=?`).get(m.event_id) as
      { from_team: string; to_team: string }
    expect(e.from_team).toBe('default')
    expect(e.to_team).toBe('default')
    cleanup()
  })

  it('returns unknown_recipient when to_agent_id belongs to another team and to_team is omitted', async () => {
    const { db, svc, cleanup } = setupService()
    insertAgent(db, { agent_id: 'A', team: 'alpha' })
    insertAgent(db, { agent_id: 'B', team: 'beta' })
    const resp = await svc.send({ from: 'A', to_agent_id: 'B', body: 'hi', auto_poke: false })
    expect(resp).toEqual({ error: 'unknown_recipient' })
    cleanup()
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import {
  SendMessageService,
  parseToAgentName,
} from '../src/mcp/send-message.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-sm-byname-'))

describe('send_message by name — same-team resolution', () => {
  const dirs: string[] = []
  const databases: ReturnType<typeof openDb>[] = []
  afterEach(() => { databases.forEach(db => { if (db.open) db.close() }); databases.length = 0; dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function setup() {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    databases.push(db)
    const svc = new SendMessageService(db, new AgentsRepo(db), new EventsOutbox(db))
    return { svc, db }
  }

  it('resolves to_agent_name to UUID and writes message row', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B', team: 'default', role: 'frontend', name: 'bob' })
    const resp = await svc.send({ from: 'uuid-A', to_agent_name: 'bob', body: 'hi', auto_poke: false })
    if ('error' in resp) throw new Error(`expected success, got ${resp.error}`)
    expect(resp.recipients).toEqual(['uuid-B'])
    const m = db.prepare(`SELECT to_agent_id, body FROM messages WHERE id=?`).get(resp.message_id) as
      { to_agent_id: string; body: string }
    expect(m.to_agent_id).toBe('uuid-B')
    expect(m.body).toBe('hi')
  })

  it('bare name resolves on caller device when duplicate names exist', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', device: 'jt', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B-jt', device: 'jt', team: 'default', role: 'frontend', name: 'bob' })
    insertAgent(db, { agent_id: 'uuid-B-gx', device: 'gx', team: 'default', role: 'frontend', name: 'bob' })

    const resp = await svc.send({
      from: 'uuid-A',
      to_agent_name: 'bob',
      body: 'hi',
      auto_poke: false,
    })
    if ('error' in resp) throw new Error(`expected success, got ${resp.error}`)
    expect(resp.recipients).toEqual(['uuid-B-jt'])
  })

  it('name:device resolves the specified device', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', device: 'jt', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B-jt', device: 'jt', team: 'default', role: 'frontend', name: 'bob' })
    insertAgent(db, { agent_id: 'uuid-B-gx', device: 'gx', team: 'default', role: 'frontend', name: 'bob' })

    const resp = await svc.send({
      from: 'uuid-A',
      to_agent_name: 'bob:gx',
      body: 'hi',
      auto_poke: false,
    })
    if ('error' in resp) throw new Error(`expected success, got ${resp.error}`)
    expect(resp.recipients).toEqual(['uuid-B-gx'])
  })

  it('invalid name:device empty halves return invalid_to_agent_name', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', device: 'jt', team: 'default', role: 'backend', name: 'alice' })

    expect(await svc.send({
      from: 'uuid-A',
      to_agent_name: ':gx',
      body: 'hi',
      auto_poke: false,
    })).toEqual({ error: 'invalid_to_agent_name' })
    expect(await svc.send({
      from: 'uuid-A',
      to_agent_name: 'bob:',
      body: 'hi',
      auto_poke: false,
    })).toEqual({ error: 'invalid_to_agent_name' })
  })

  it('returns missing_recipient when neither to_agent_id nor to_agent_name given', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    const resp = await svc.send({ from: 'uuid-A', body: 'hi', auto_poke: false } as unknown as Parameters<typeof svc.send>[0])
    expect(resp).toEqual({ error: 'missing_recipient' })
    const count = db.prepare(`SELECT COUNT(*) AS c FROM events`).get() as { c: number }
    expect(count.c).toBe(0)
  })

  it('returns unknown_recipient when to_agent_name does not exist in resolved team', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    const resp = await svc.send({ from: 'uuid-A', to_agent_name: 'ghost', body: 'hi', auto_poke: false })
    expect(resp).toEqual({ error: 'unknown_recipient' })
  })

  it('returns unknown_recipient when to_agent_name misses in explicit cross-team', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'alpha', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B', team: 'alpha', role: 'frontend', name: 'bob' })
    const resp = await svc.send({ from: 'uuid-A', to_agent_name: 'bob', to_team: 'beta', body: 'hi', auto_poke: false })
    expect(resp).toEqual({ error: 'unknown_recipient' })
    const evCount = db.prepare(`SELECT COUNT(*) AS c FROM events`).get() as { c: number }
    expect(evCount.c).toBe(0)
  })

  it('lookup is case-sensitive — Bob !== bob', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B', team: 'default', role: 'frontend', name: 'Bob' })
    const resp = await svc.send({ from: 'uuid-A', to_agent_name: 'bob', body: 'hi', auto_poke: false })
    expect(resp).toEqual({ error: 'unknown_recipient' })
  })

  it('cross-team send via to_agent_name persists correct from_team/to_team', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'alpha', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B-beta', team: 'beta', role: 'frontend', name: 'bob' })
    const resp = await svc.send({
      from: 'uuid-A', to_agent_name: 'bob', to_team: 'beta', body: 'hi', auto_poke: false
    })
    if ('error' in resp) throw new Error(`expected success, got ${resp.error}`)
    expect(resp.recipients).toEqual(['uuid-B-beta'])
    const m = db.prepare(`SELECT from_team, to_team, to_agent_id, event_id FROM messages WHERE id=?`).get(resp.message_id) as
      { from_team: string; to_team: string; to_agent_id: string; event_id: number }
    expect(m).toEqual({ from_team: 'alpha', to_team: 'beta', to_agent_id: 'uuid-B-beta', event_id: resp.event_id })
    const e = db.prepare(`SELECT from_team, to_team FROM events WHERE event_id=?`).get(resp.event_id) as
      { from_team: string; to_team: string }
    expect(e).toEqual({ from_team: 'alpha', to_team: 'beta' })
  })

  it('recipients always holds resolved UUID regardless of input path', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B', team: 'default', role: 'frontend', name: 'bob' })
    const r1 = await svc.send({ from: 'uuid-A', to_agent_id: 'uuid-B', body: 'via-id', auto_poke: false })
    const r2 = await svc.send({ from: 'uuid-A', to_agent_name: 'bob', body: 'via-name', auto_poke: false })
    if ('error' in r1) throw new Error('r1 expected success')
    if ('error' in r2) throw new Error('r2 expected success')
    expect(r1.recipients).toEqual(['uuid-B'])
    expect(r2.recipients).toEqual(['uuid-B'])
  })

  it('returns ambiguous_recipient when both to_agent_id and to_agent_name given', async () => {
    const { svc, db } = setup()
    insertAgent(db, { agent_id: 'uuid-A', team: 'default', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'uuid-B', team: 'default', role: 'frontend', name: 'bob' })
    const resp = await svc.send({
      from: 'uuid-A', to_agent_id: 'uuid-B', to_agent_name: 'bob', body: 'hi', auto_poke: false
    })
    expect(resp).toEqual({ error: 'ambiguous_recipient' })
    const count = db.prepare(`SELECT COUNT(*) AS c FROM messages`).get() as { c: number }
    expect(count.c).toBe(0)
  })
})

describe('parseToAgentName', () => {
  it('parses bare names on the caller device', () => {
    expect(parseToAgentName('creator', 'jt')).toEqual({
      ok: { name: 'creator', device: 'jt' },
    })
  })

  it('splits name:device on the first colon', () => {
    expect(parseToAgentName('creator:gx', 'jt')).toEqual({
      ok: { name: 'creator', device: 'gx' },
    })
  })

  it('rejects empty halves', () => {
    expect(parseToAgentName(':gx', 'jt')).toEqual({ error: 'invalid_to_agent_name' })
    expect(parseToAgentName('creator:', 'jt')).toEqual({ error: 'invalid_to_agent_name' })
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import { SubscribeChannelWakeService } from '../src/mcp/subscribe-channel-wake.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

function setup() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const repo = new AgentsRepo(db)
  const fanout = new ChannelWakeFanout()
  const svc = new SubscribeChannelWakeService(db, fanout)
  return { dir, db, repo, fanout, svc }
}

describe('subscribe_channel_wake service', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('succeeds for __channel_proxy__ caller and attaches a sink', () => {
    const { dir, db, repo, fanout, svc } = setup(); cleanups.push(dir)
    const proxy = repo.register({ model: 'proxy', role: '__channel_proxy__', name: 'p1' })
    const sink = () => {}
    const res = svc.subscribe({
      callerAgentId: proxy.agent_id,
      channel_session_id: 'csid-abc',
      sessionId: 'sess-1',
      sink
    })
    expect(res).toEqual({ ok: true })
    expect(fanout.has('csid-abc')).toBe(true)
    db.close()
  })

  it('rejects non-proxy caller with forbidden_role and leaves fanout untouched', () => {
    const { dir, db, repo, fanout, svc } = setup(); cleanups.push(dir)
    const be = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    const sink = () => {}
    const res = svc.subscribe({
      callerAgentId: be.agent_id,
      channel_session_id: 'csid-abc',
      sessionId: 'sess-1',
      sink
    })
    expect(res).toEqual({ error: 'forbidden_role' })
    expect(fanout.has('csid-abc')).toBe(false)
    db.close()
  })

  it('returns unknown_agent when caller not registered', () => {
    const { dir, db, fanout, svc } = setup(); cleanups.push(dir)
    const sink = () => {}
    const res = svc.subscribe({
      callerAgentId: 'ghost',
      channel_session_id: 'csid-abc',
      sessionId: 'sess-1',
      sink
    })
    expect(res).toEqual({ error: 'unknown_agent' })
    expect(fanout.has('csid-abc')).toBe(false)
    db.close()
  })

  it('session close detaches all sinks from fanout via detachBySession', () => {
    const { dir, db, repo, fanout, svc } = setup(); cleanups.push(dir)
    const proxy = repo.register({ model: 'proxy', role: '__channel_proxy__', name: 'p1' })
    svc.subscribe({ callerAgentId: proxy.agent_id, channel_session_id: 'csid-1', sessionId: 'sess-P', sink: () => {} })
    svc.subscribe({ callerAgentId: proxy.agent_id, channel_session_id: 'csid-2', sessionId: 'sess-P', sink: () => {} })
    fanout.detachBySession('sess-P')
    expect(fanout.has('csid-1')).toBe(false)
    expect(fanout.has('csid-2')).toBe(false)
    db.close()
  })
})

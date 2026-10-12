import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { toPublicAgentRow } from '../src/mcp/agent-public-row.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('AgentsRepo.list channel_session_id', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('list() returns derived channel_session_id and delivery for each agent', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const a = repo.register({
      model: 'opus',
      role: 'backend',
      name: 'alice',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
    })
    const b = repo.register({ model: 'sonnet', role: 'backend', name: 'bob' })
    const rows = repo.list({ team: 'default' })
    const aRow = rows.find(r => r.agent_id === a.agent_id)
    const bRow = rows.find(r => r.agent_id === b.agent_id)
    expect(aRow?.channel_session_id).toBe('csid-abc')
    expect(aRow?.delivery).toEqual({
      kind: 'claude-channel',
      channel_session_id: 'csid-abc',
    })
    expect(bRow?.channel_session_id).toBeNull()
    expect(bRow?.delivery).toEqual({ kind: 'none' })
    db.close()
  })
})

describe('toPublicAgentRow projection', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('projects claude-channel to {kind, channel_session_id} only and preserves top-level channel_session_id', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const a = repo.register({
      model: 'opus',
      role: 'backend',
      name: 'alice',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
    })
    const rows = repo.list({ team: 'default' }).map(toPublicAgentRow)
    const aRow = rows.find(r => r.agent_id === a.agent_id)!
    expect(aRow.delivery).toEqual({
      kind: 'claude-channel',
      channel_session_id: 'csid-abc',
    })
    expect(aRow.channel_session_id).toBe('csid-abc')
    db.close()
  })

  it('projects none to {kind: none} and keeps channel_session_id null', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const b = repo.register({ model: 'sonnet', role: 'backend', name: 'bob' })
    const rows = repo.list({ team: 'default' }).map(toPublicAgentRow)
    const bRow = rows.find(r => r.agent_id === b.agent_id)!
    expect(bRow.delivery).toEqual({ kind: 'none' })
    expect(bRow.channel_session_id).toBeNull()
    db.close()
  })

  it('projects codex-appserver to {kind} only, hiding thread_id / ws_url / auth_token_ref', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const c = repo.register({ model: 'gpt-5', role: 'backend', name: 'carol' })
    repo.setDelivery(c.agent_id, {
      kind: 'codex-appserver',
      thread_id: '11111111-1111-4111-8111-111111111111',
      ws_url: 'ws://127.0.0.1:8799',
      auth_token_ref: 'env:TOKEN',
    })
    const rows = repo.list({ team: 'default' }).map(toPublicAgentRow)
    const cRow = rows.find(r => r.agent_id === c.agent_id)!
    expect(cRow.delivery).toEqual({ kind: 'codex-appserver' })
    expect(cRow.channel_session_id).toBeNull()
    const delivery = cRow.delivery as Record<string, unknown>
    expect(Object.prototype.hasOwnProperty.call(delivery, 'thread_id')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(delivery, 'ws_url')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(delivery, 'auth_token_ref')).toBe(false)
    db.close()
  })
})

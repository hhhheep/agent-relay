import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-list-exclude-roles-'))

// RED tests for `AgentsRepo.list({excludeRoles})` — change `clean-channel-proxy-noise`,
// task 4.1. Until storage gains the optional `excludeRoles?: string[]` argument
// (per design D1), these assertions fail because `list()` rejects the extra key
// (TS) and / or returns proxy rows unfiltered (runtime).
describe('AgentsRepo.list excludeRoles', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function fresh(): { db: ReturnType<typeof openDb>; repo: AgentsRepo } {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return { db, repo: new AgentsRepo(db) }
  }

  it('returns all rows when excludeRoles is omitted (default behaviour unchanged)', () => {
    const { db, repo } = fresh()
    insertAgent(db, { agent_id: 'biz-1', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'proxy-1', role: '__channel_proxy__', name: 'channel-proxy-1' })
    const out = repo.list({ team: 'default' })
    const ids = out.map(a => a.agent_id).sort()
    expect(ids).toEqual(['biz-1', 'proxy-1'])
    db.close()
  })

  it('filters channel proxy rows when excludeRoles=[__channel_proxy__]', () => {
    const { db, repo } = fresh()
    insertAgent(db, { agent_id: 'biz-1', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'biz-2', role: 'frontend', name: 'bob' })
    insertAgent(db, { agent_id: 'proxy-1', role: '__channel_proxy__', name: 'channel-proxy-1' })
    insertAgent(db, { agent_id: 'proxy-2', role: '__channel_proxy__', name: 'channel-proxy-2' })
    // Cast through unknown to keep RED before the API exists; this is the call
    // shape spec'd by design D1.
    const list = repo.list as unknown as (args: { team: string; excludeRoles?: string[] }) => Array<{ agent_id: string; role: string }>
    const out = list({ team: 'default', excludeRoles: ['__channel_proxy__'] })
    const ids = out.map(a => a.agent_id).sort()
    expect(ids).toEqual(['biz-1', 'biz-2'])
    expect(out.find(a => a.role === '__channel_proxy__')).toBeUndefined()
    db.close()
  })

  it('treats empty excludeRoles=[] the same as omitting it (no filtering)', () => {
    const { db, repo } = fresh()
    insertAgent(db, { agent_id: 'biz-1', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'proxy-1', role: '__channel_proxy__', name: 'channel-proxy-1' })
    const list = repo.list as unknown as (args: { team: string; excludeRoles?: string[] }) => Array<{ agent_id: string }>
    const out = list({ team: 'default', excludeRoles: [] })
    const ids = out.map(a => a.agent_id).sort()
    expect(ids).toEqual(['biz-1', 'proxy-1'])
    db.close()
  })

  it('still scopes by team when excludeRoles is set (cross-team rows never leak)', () => {
    const { db, repo } = fresh()
    insertAgent(db, { agent_id: 'a-biz', team: 'alpha', role: 'backend', name: 'alice' })
    insertAgent(db, { agent_id: 'a-proxy', team: 'alpha', role: '__channel_proxy__', name: 'channel-proxy-a' })
    insertAgent(db, { agent_id: 'b-biz', team: 'beta', role: 'backend', name: 'bob' })
    insertAgent(db, { agent_id: 'b-proxy', team: 'beta', role: '__channel_proxy__', name: 'channel-proxy-b' })
    const list = repo.list as unknown as (args: { team: string; excludeRoles?: string[] }) => Array<{ agent_id: string }>
    const out = list({ team: 'alpha', excludeRoles: ['__channel_proxy__'] })
    expect(out.map(a => a.agent_id)).toEqual(['a-biz'])
    db.close()
  })
})

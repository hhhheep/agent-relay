import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { RegisterAgentService } from '../src/mcp/register-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-reg-service-identity-'))

describe('RegisterAgentService identity is (device, team, name)', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function fresh(deps: ConstructorParameters<typeof RegisterAgentService>[1] = {}): RegisterAgentService {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return new RegisterAgentService(db, deps)
  }

  it('second session with same (device, team, name) different role takes over (no collision error)', () => {
    const closes: string[] = []
    const svc = fresh({ closeSessionByConnectionId: (cid) => { closes.push(cid); return true } })
    const r1 = svc.register({ connection_id: 'sess-A', model: 'opus', name: 'alice', role: 'backend', team: 'default' })
    expect('agent_id' in r1).toBe(true)
    const r2 = svc.register({ connection_id: 'sess-B', model: 'opus', name: 'alice', role: 'frontend', team: 'default' })
    expect('agent_id' in r2).toBe(true)
    expect((r2 as { agent_id: string }).agent_id).toBe((r1 as { agent_id: string }).agent_id)
    expect(closes).toEqual(['sess-A'])
  })

  it('same session re-registering same (device, team, name) with new role is a reuse, not collision', () => {
    const svc = fresh()
    const r1 = svc.register({ connection_id: 'sess-A', model: 'opus', name: 'alice', role: 'backend', team: 'default' })
    const id1 = (r1 as { agent_id: string }).agent_id
    const r2 = svc.register({ connection_id: 'sess-A', model: 'opus', name: 'alice', role: 'frontend', team: 'default' })
    expect('agent_id' in r2).toBe(true)
    expect((r2 as { agent_id: string }).agent_id).toBe(id1)
  })

  it('after releaseConnection, a different session can take over (device, team, name)', () => {
    const svc = fresh()
    const r1 = svc.register({ connection_id: 'sess-A', model: 'opus', name: 'alice', role: 'backend', team: 'default' })
    const id1 = (r1 as { agent_id: string }).agent_id
    svc.releaseConnection(id1, 'sess-A')
    const r2 = svc.register({ connection_id: 'sess-B', model: 'opus', name: 'alice', role: 'frontend', team: 'default' })
    expect('agent_id' in r2).toBe(true)
    expect((r2 as { agent_id: string }).agent_id).toBe(id1)
  })
})

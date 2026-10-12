import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { ensureCallerMatches, IdentityMismatchError } from '../src/mcp/identity.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('identity guard and touch', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('ensureCallerMatches throws identity_mismatch on disagreement', () => {
    expect(() => ensureCallerMatches('sess-A', 'sess-B')).toThrow(IdentityMismatchError)
  })

  it('ensureCallerMatches passes when equal or claim is undefined', () => {
    expect(() => ensureCallerMatches('sess-A', 'sess-A')).not.toThrow()
    expect(() => ensureCallerMatches('sess-A', undefined)).not.toThrow()
  })

  it('touch updates last_seen_at', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const repo = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'sess-A', model: 'm', role: 'r' , name: 'sess-A' })
    const old = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    db.prepare('UPDATE agents SET last_seen_at=? WHERE agent_id=?').run(old, 'sess-A')
    repo.touch('sess-A')
    const row = db.prepare('SELECT last_seen_at FROM agents WHERE agent_id=?').get('sess-A') as { last_seen_at: string }
    expect(Date.now() - new Date(row.last_seen_at).getTime()).toBeLessThan(2000)
  })
})

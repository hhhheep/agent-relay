import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

describe('AgentsRepo channel_session_id column default', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('register() creates rows with channel_session_id=NULL (bind_channel is the only writer)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    const row = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id=?`).get(r.agent_id) as
      { channel_session_id: string | null }
    expect(row.channel_session_id).toBeNull()
    db.close()
  })

  it('re-register() leaves channel_session_id untouched (bind_channel owns that column)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r1 = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    // Simulate bind_channel having written a value.
    db.prepare(`UPDATE agents SET channel_session_id=? WHERE agent_id=?`).run('csid-abc', r1.agent_id)
    // Re-register — should not clear or overwrite.
    repo.register({ model: 'sonnet', role: 'backend', name: 'alice' })
    const row = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id=?`).get(r1.agent_id) as
      { channel_session_id: string | null }
    expect(row.channel_session_id).toBe('csid-abc')
    db.close()
  })
})

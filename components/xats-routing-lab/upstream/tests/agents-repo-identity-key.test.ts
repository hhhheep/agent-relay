import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-repo-identity-key-'))

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

describe('AgentsRepo identity_key', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('writes the key on register and exposes it on the row', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
      identity_key: 'K',
    })
    expect(repo.getById(r.agent_id)?.identity_key).toBe('K')
    db.close()
  })

  it('preserves an existing key when re-registering without one', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const first = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
      identity_key: 'K',
    })
    const second = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
    })
    expect(second.agent_id).toBe(first.agent_id)
    expect(repo.getById(first.agent_id)?.identity_key).toBe('K')
    db.close()
  })

  it('reverse-looks-up the holder with its runtime pid', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
      runtime_ui_pid: 4242,
      identity_key: 'K',
    })
    const matches = repo.findByIdentityKey('K', 'local')
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({
      agent_id: r.agent_id,
      device: 'local',
      team: 'aoe',
      name: 'tester',
      runtime_ui_pid: 4242,
    })
    db.close()
  })

  it('does not match another device or a channel proxy row', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      device: 'gx',
      name: 'remote',
      team: 'aoe',
      identity_key: 'K',
    })
    repo.register({
      agent_type: 'custom',
      role: '__channel_proxy__',
      name: 'channel-proxy-1',
      team: 'default',
      identity_key: 'P',
    })
    expect(repo.findByIdentityKey('K', 'local')).toEqual([])
    expect(repo.findByIdentityKey('P', 'local')).toEqual([])
    db.close()
  })

  it('clears the key on the named row only', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const keyed = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
      identity_key: 'K',
    })
    const other = repo.register({
      agent_type: 'claude-code',
      name: 'reviewer',
      team: 'aoe',
      identity_key: 'L',
    })
    repo.clearIdentityKey(keyed.agent_id)
    expect(repo.getById(keyed.agent_id)?.identity_key).toBeNull()
    expect(repo.getById(other.agent_id)?.identity_key).toBe('L')
    expect(repo.findByIdentityKey('K', 'local')).toEqual([])
    db.close()
  })

  it('leaves the key NULL when register omits it', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({
      agent_type: 'claude-code',
      name: 'tester',
      team: 'aoe',
    })
    expect(repo.getById(r.agent_id)?.identity_key).toBeNull()
    const listed = repo.list({ team: 'aoe' })
    expect(listed[0].identity_key).toBeNull()
    db.close()
  })
})

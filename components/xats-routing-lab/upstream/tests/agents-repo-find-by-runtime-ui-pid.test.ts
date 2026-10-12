import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-find-by-uipid-'))

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

function setLastSeen(db: ReturnType<typeof openDb>, agent_id: string, iso: string): void {
  db.prepare(`UPDATE agents SET last_seen_at=? WHERE agent_id=?`).run(iso, agent_id)
}

describe('AgentsRepo.findByRuntimeUiPid', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('returns an empty array when no row matches the ui_pid', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    expect(repo.findByRuntimeUiPid(99999, 'local')).toEqual([])
    db.close()
  })

  it('returns the single local row that matches the ui_pid', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    const matches = repo.findByRuntimeUiPid(25079, 'local')
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({
      agent_id: r.agent_id,
      device: 'local',
      team: 'default',
      name: 'xats-creator',
      role: 'worker',
    })
    expect(typeof matches[0].last_seen_at).toBe('string')
    db.close()
  })

  it('orders multiple matches by last_seen_at DESC', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const older = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-tester',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    const newer = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    setLastSeen(db, older.agent_id, '2024-01-01T00:00:00.000Z')
    setLastSeen(db, newer.agent_id, '2024-06-01T00:00:00.000Z')

    const matches = repo.findByRuntimeUiPid(25079, 'local')
    expect(matches.map(m => m.name)).toEqual(['xats-creator', 'xats-tester'])
    db.close()
  })

  it('returns rows on the configured local device label', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const r = repo.register({
      agent_type: 'claude-code',
      device: 'jt',
      model: 'opus',
      role: 'worker',
      name: 'local-jt-alice',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    const matches = repo.findByRuntimeUiPid(25079, 'jt')
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({
      agent_id: r.agent_id,
      device: 'jt',
      name: 'local-jt-alice',
    })
    db.close()
  })

  it('does not match rows on a different device label', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      device: 'gx',
      model: 'opus',
      role: 'worker',
      name: 'remote-alice',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    expect(repo.findByRuntimeUiPid(25079, 'local')).toEqual([])
    db.close()
  })

  it('hands off the ppid when the same identity re-registers with a new ui_pid', () => {
    // A new agent process (ppid changed) explicitly re-registers the SAME
    // (team, name). The row's runtime_ui_pid must move to the new ppid so the
    // next reconnect resolves to it — and the old ppid must stop resolving.
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const first = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      runtime_ui_pid: 100,
    })
    expect(repo.findByRuntimeUiPid(100, 'local').map(m => m.name)).toEqual(['xats-creator'])

    const second = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      runtime_ui_pid: 200,
    })
    // Same identity reused — no duplicate row, stable agent_id.
    expect(second.agent_id).toBe(first.agent_id)

    // Old ppid no longer resolves; new ppid is now authoritative.
    expect(repo.findByRuntimeUiPid(100, 'local')).toEqual([])
    const matches = repo.findByRuntimeUiPid(200, 'local')
    expect(matches).toHaveLength(1)
    expect(matches[0].agent_id).toBe(first.agent_id)
    db.close()
  })

  it('preserves the bound ppid when re-registering the same identity without a ui_pid', () => {
    // COALESCE boundary: a re-register that omits runtime_ui_pid does NOT clear
    // the previously bound ppid. (In practice claude-code always passes $PPID,
    // so the handoff above is the common path; this documents the omit case.)
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    const first = repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      runtime_ui_pid: 100,
    })
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'xats-creator',
      team: 'default',
      // no runtime_ui_pid
    })
    const matches = repo.findByRuntimeUiPid(100, 'local')
    expect(matches).toHaveLength(1)
    expect(matches[0].agent_id).toBe(first.agent_id)
    db.close()
  })

  it('does not match channel proxy rows', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      runtime_ui_pid: 25079,
    })
    expect(repo.findByRuntimeUiPid(25079, 'local')).toEqual([])
    db.close()
  })
})

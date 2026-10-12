import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-reactive-rebind-'))

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

interface DeliveryRow {
  delivery_kind: string
  delivery_payload: string | null
}

function deliveryOf(db: ReturnType<typeof openDb>, name: string, team = 'default'): DeliveryRow {
  return db.prepare(`SELECT delivery_kind, delivery_payload FROM agents WHERE team=? AND name=?`).get(team, name) as DeliveryRow
}

function deliveryOfDevice(
  db: ReturnType<typeof openDb>,
  device: string,
  name: string,
  team = 'default'
): DeliveryRow {
  return db.prepare(
    `SELECT delivery_kind, delivery_payload FROM agents WHERE device=? AND team=? AND name=?`
  ).get(device, team, name) as DeliveryRow
}

describe('AgentsRepo reactive rebind on __channel_proxy__ register', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('promotes pre-existing delivery=none host to claude-channel when proxy registers (6.10)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      team: 'default',
      runtime_ui_pid: 25424,
    })
    expect(deliveryOf(db, 'alice').delivery_kind).toBe('none')

    repo.register({
      agent_type: 'custom',
      agent_type_name: 'cross-agent-teams-channel',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })
    const row = deliveryOf(db, 'alice')
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-new' })
    db.close()
  })

  it('rewrites stale csid on proxy restart (6.11)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      team: 'default',
      runtime_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-old' },
    })
    // Initial proxy row with csid-old
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-old' },
    })
    // Proxy restart: same identity, new csid
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })
    const row = deliveryOf(db, 'alice')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-new' })
    db.close()
  })

  it('does not rebind hosts on another device with the same runtime_ui_pid', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      device: 'jt',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      team: 'default',
      runtime_ui_pid: 25424,
    })
    repo.register({
      agent_type: 'claude-code',
      device: 'gx',
      model: 'opus',
      role: 'worker',
      name: 'bob',
      team: 'default',
      runtime_ui_pid: 25424,
    })
    repo.register({
      agent_type: 'custom',
      device: 'jt',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })

    expect(deliveryOfDevice(db, 'jt', 'alice').delivery_kind).toBe('claude-channel')
    expect(deliveryOfDevice(db, 'gx', 'bob').delivery_kind).toBe('none')
    db.close()
  })

  it('skips hosts with runtime_ui_pid IS NULL (6.12)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'bob',
      team: 'default',
      // no runtime_ui_pid
    })
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })
    expect(deliveryOf(db, 'bob').delivery_kind).toBe('none')
    db.close()
  })

  it('does not overwrite codex-appserver delivery (6.13)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'codex',
      model: 'gpt-5',
      role: 'worker',
      name: 'carol',
      team: 'default',
      runtime_ui_pid: 25424,
      delivery: {
        kind: 'codex-appserver',
        thread_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
        ws_url: 'ws://127.0.0.1:8799',
      },
    })
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })
    expect(deliveryOf(db, 'carol').delivery_kind).toBe('codex-appserver')
    db.close()
  })

  it('is scoped to the proxy team (6.14)', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'dave',
      team: 'alpha',
      runtime_ui_pid: 25424,
    })
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'channel-proxy-27245',
      team: 'default',
      claude_ui_pid: 25424,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-new' },
    })
    expect(deliveryOf(db, 'dave', 'alpha').delivery_kind).toBe('none')
    db.close()
  })

  it('is a no-op when proxy registers without claude_ui_pid', () => {
    const { dir, db, repo } = freshRepo(); cleanups.push(dir)
    repo.register({
      agent_type: 'claude-code',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      team: 'default',
      runtime_ui_pid: 25424,
    })
    // Proxy row without claude_ui_pid (defensive — should not trigger rebind).
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'proxy-1',
      team: 'default',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-xyz' },
    })
    expect(deliveryOf(db, 'alice').delivery_kind).toBe('none')
    db.close()
  })
})

import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { RegisterAgentService } from '../src/mcp/register-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-reg-device-'))

describe('RegisterAgentService device resolution', () => {
  const dirs: string[] = []
  afterEach(() => {
    dirs.forEach(d => rmSync(d, { recursive: true, force: true }))
    dirs.length = 0
  })

  function setup(origin: 'local' | 'remote' = 'local') {
    const dir = tmp()
    dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db, { localDevice: 'jt' })
    const svc = new RegisterAgentService(db, {
      localDevice: 'jt',
      getSessionOrigin: () => ({
        origin,
        remote_addr: origin === 'remote' ? '192.168.1.42' : null,
      }),
    })
    return { db, svc }
  }

  it('fills local device for loopback callers that omit device', () => {
    const { db, svc } = setup('local')
    const res = svc.register({ connection_id: 'c1', agent_type: 'custom', name: 'alice' })
    if ('error' in res) throw new Error(res.error)
    const row = db.prepare(
      `SELECT device, remote_addr FROM agents WHERE agent_id=?`
    ).get(res.agent_id) as { device: string; remote_addr: string | null }
    expect(row).toEqual({ device: 'jt', remote_addr: null })
  })

  it('accepts matching loopback device and rejects mismatches', () => {
    const { svc } = setup('local')
    const ok = svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'alice',
      device: 'jt',
    })
    expect('agent_id' in ok).toBe(true)
    const bad = svc.register({
      connection_id: 'c2',
      agent_type: 'custom',
      name: 'bob',
      device: 'gx',
    })
    expect(bad).toEqual({ error: 'device_spoofing_from_loopback' })
  })

  it('requires remote device and stores remote_addr on success', () => {
    const { db, svc } = setup('remote')
    expect(svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'alice',
    })).toEqual({ error: 'device_required_from_remote' })

    const res = svc.register({
      connection_id: 'c2',
      agent_type: 'custom',
      name: 'bob',
      device: 'gx',
    })
    if ('error' in res) throw new Error(res.error)
    const row = db.prepare(
      `SELECT device, remote_addr FROM agents WHERE agent_id=?`
    ).get(res.agent_id) as { device: string; remote_addr: string | null }
    expect(row).toEqual({ device: 'gx', remote_addr: '192.168.1.42' })
  })

  it('rejects remote local-label spoofing and invalid device labels', () => {
    const { svc } = setup('remote')
    expect(svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'alice',
      device: 'jt',
    })).toEqual({ error: 'device_spoofing_local_label_from_remote' })
    expect(svc.register({
      connection_id: 'c2',
      agent_type: 'custom',
      name: 'bob',
      device: 'has:colon',
    })).toEqual({ error: 'invalid_device_label' })
    expect(svc.register({
      connection_id: 'c3',
      agent_type: 'custom',
      name: 'carol',
      device: 'a'.repeat(65),
    })).toEqual({ error: 'invalid_device_label' })
  })

  it('rejects names containing colon regardless of origin', () => {
    const { svc } = setup('local')
    expect(svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'bad:name',
    })).toEqual({ error: 'invalid_name_label' })
  })

  it('rejects names containing parentheses (name(team) shorthand passed literally)', () => {
    const { svc } = setup('local')
    expect(svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'skills-creator(default)',
    })).toEqual({ error: 'invalid_name_label' })
  })

  it('rejects an explicit team containing parentheses', () => {
    const { svc } = setup('local')
    expect(svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'alice',
      team: 'default)',
    })).toEqual({ error: 'invalid_team_label' })
  })

  it('still derives a team from project_dir basename even if it contains parentheses (explicit-only guard)', () => {
    const { svc } = setup('local')
    const res = svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'alice',
      project_dir: '/tmp/my(proj)',
    })
    expect('agent_id' in res).toBe(true)
    if ('agent_id' in res) expect(res.team).toBe('my(proj)')
  })

  it('normalizes remote-supplied device labels using the same rules as local', () => {
    const { db, svc } = setup('remote')
    const res = svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'creator',
      device: 'MyMac.local',
    })
    expect(res).toMatchObject({ team: 'default' })
    expect('agent_id' in res).toBe(true)
    if (!('agent_id' in res)) return
    const row = db.prepare(
      `SELECT device FROM agents WHERE agent_id=?`
    ).get(res.agent_id) as { device: string }
    expect(row.device).toBe('mymac-local')
  })

  it('normalizes punctuation in remote device labels to dashes', () => {
    const { db, svc } = setup('remote')
    const res = svc.register({
      connection_id: 'c1',
      agent_type: 'custom',
      name: 'creator',
      device: '@@@',
    })
    expect('agent_id' in res).toBe(true)
    if (!('agent_id' in res)) return
    const row = db.prepare(
      `SELECT device FROM agents WHERE agent_id=?`
    ).get(res.agent_id) as { device: string }
    expect(row.device).toBe('---')
  })
})

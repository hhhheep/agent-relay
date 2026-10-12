import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-find-by-kimi-session-'))

const BASE_URL = 'http://127.0.0.1:58627'

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

function registerKimi(
  repo: AgentsRepo,
  args: {
    name: string
    session_id: string
    base_url?: string
    device?: string
  }
): string {
  return repo.register({
    agent_type: 'kimi-code',
    device: args.device,
    name: args.name,
    team: 'default',
    delivery: {
      kind: 'kimi-server',
      session_id: args.session_id,
      base_url: args.base_url ?? BASE_URL,
    },
  }).agent_id
}

describe('AgentsRepo.findByKimiSession', () => {
  const cleanups: string[] = []

  afterEach(() => {
    cleanups.forEach(dir => rmSync(dir, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('returns the local kimi delivery row matching (base_url, session_id)', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    const agentId = registerKimi(repo, {
      name: 'kimi-a',
      session_id: 'session_aaa',
    })

    expect(repo.findByKimiSession(BASE_URL, 'session_aaa', 'local')).toEqual([
      expect.objectContaining({
        agent_id: agentId,
        device: 'local',
        name: 'kimi-a',
      }),
    ])
    db.close()
  })

  it('matches base_url trailing-slash-tolerant in both directions', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerKimi(repo, {
      name: 'kimi-slash',
      session_id: 'session_slash',
      base_url: `${BASE_URL}/`,
    })

    expect(
      repo.findByKimiSession(BASE_URL, 'session_slash', 'local').map(r => r.name)
    ).toEqual(['kimi-slash'])
    expect(
      repo.findByKimiSession(`${BASE_URL}/`, 'session_slash', 'local').map(r => r.name)
    ).toEqual(['kimi-slash'])
    db.close()
  })

  it('does not match when session_id differs even if base_url matches', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerKimi(repo, { name: 'kimi-a', session_id: 'session_aaa' })

    expect(repo.findByKimiSession(BASE_URL, 'session_other', 'local')).toEqual([])
    db.close()
  })

  it('orders duplicate matches by last_seen_at DESC', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    const older = registerKimi(repo, { name: 'older', session_id: 'session_dup' })
    const newer = registerKimi(repo, { name: 'newer', session_id: 'session_dup' })
    db.prepare(`UPDATE agents SET last_seen_at=? WHERE agent_id=?`).run(
      '2024-01-01T00:00:00.000Z',
      older,
    )
    db.prepare(`UPDATE agents SET last_seen_at=? WHERE agent_id=?`).run(
      '2024-06-01T00:00:00.000Z',
      newer,
    )

    expect(
      repo.findByKimiSession(BASE_URL, 'session_dup', 'local').map(r => r.name)
    ).toEqual(['newer', 'older'])
    db.close()
  })

  it('ignores another device and non-kimi delivery rows', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerKimi(repo, {
      name: 'remote',
      session_id: 'session_rem',
      device: 'gx',
    })
    repo.register({
      agent_type: 'opencode',
      name: 'oc',
      team: 'default',
      delivery: {
        kind: 'opencode-server',
        session_id: 'ses_oc',
        base_url: BASE_URL,
      },
    })

    expect(repo.findByKimiSession(BASE_URL, 'session_rem', 'local')).toEqual([])
    expect(repo.findByKimiSession(BASE_URL, 'ses_oc', 'local')).toEqual([])
    db.close()
  })

  it('ignores malformed kimi delivery payloads instead of throwing', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    db.prepare(
      `INSERT INTO agents (
         agent_id, agent_type, device, team, role, name, registered_at,
         last_seen_at, delivery_kind, delivery_payload
       ) VALUES ('broken', 'kimi-code', 'local', 'default', 'worker', 'broken',
         '2024-01-01T00:00:00.000Z', '2024-01-01T00:00:00.000Z',
         'kimi-server', 'not-json')`
    ).run()

    expect(repo.findByKimiSession(BASE_URL, 'session_any', 'local')).toEqual([])
    db.close()
  })
})

describe('AgentsRepo.findByKimiBaseUrl', () => {
  const cleanups: string[] = []

  afterEach(() => {
    cleanups.forEach(dir => rmSync(dir, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('returns every local kimi row on the base_url regardless of session_id', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerKimi(repo, { name: 'kimi-a', session_id: 'session_aaa' })
    registerKimi(repo, { name: 'kimi-b', session_id: 'session_bbb' })
    registerKimi(repo, {
      name: 'kimi-other-url',
      session_id: 'session_ccc',
      base_url: 'http://127.0.0.1:59999',
    })

    expect(
      repo.findByKimiBaseUrl(BASE_URL, 'local').map(r => r.name).sort()
    ).toEqual(['kimi-a', 'kimi-b'])
    db.close()
  })

  it('scopes to the local device', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerKimi(repo, {
      name: 'remote',
      session_id: 'session_rem',
      device: 'gx',
    })

    expect(repo.findByKimiBaseUrl(BASE_URL, 'local')).toEqual([])
    db.close()
  })
})

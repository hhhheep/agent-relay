import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-find-by-opencode-session-'))

const BASE_URL = 'http://127.0.0.1:18888'

function freshRepo() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  return { dir, db, repo: new AgentsRepo(db) }
}

function registerOpencode(
  repo: AgentsRepo,
  args: {
    name: string
    session_id: string
    base_url?: string
    device?: string
  }
): string {
  return repo.register({
    agent_type: 'opencode',
    device: args.device,
    name: args.name,
    team: 'default',
    delivery: {
      kind: 'opencode-server',
      session_id: args.session_id,
      base_url: args.base_url ?? BASE_URL,
    },
  }).agent_id
}

describe('AgentsRepo.findByOpencodeSession', () => {
  const cleanups: string[] = []

  afterEach(() => {
    cleanups.forEach(dir => rmSync(dir, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('returns the local opencode delivery row matching (base_url, session_id)', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    const agentId = registerOpencode(repo, {
      name: 'oc-a',
      session_id: 'ses_aaa',
    })

    expect(repo.findByOpencodeSession(BASE_URL, 'ses_aaa', 'local')).toEqual([
      expect.objectContaining({
        agent_id: agentId,
        device: 'local',
        name: 'oc-a',
      }),
    ])
    db.close()
  })

  it('matches base_url trailing-slash-tolerant (stored with slash, query without)', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerOpencode(repo, {
      name: 'oc-slash',
      session_id: 'ses_slash',
      base_url: `${BASE_URL}/`,
    })

    expect(
      repo.findByOpencodeSession(BASE_URL, 'ses_slash', 'local').map(r => r.name)
    ).toEqual(['oc-slash'])
    db.close()
  })

  it('matches base_url trailing-slash-tolerant (stored without, query with)', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerOpencode(repo, {
      name: 'oc-noslash',
      session_id: 'ses_noslash',
      base_url: BASE_URL,
    })

    expect(
      repo
        .findByOpencodeSession(`${BASE_URL}/`, 'ses_noslash', 'local')
        .map(r => r.name)
    ).toEqual(['oc-noslash'])
    db.close()
  })

  it('does not match when session_id differs even if base_url matches', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerOpencode(repo, { name: 'oc-a', session_id: 'ses_aaa' })

    expect(repo.findByOpencodeSession(BASE_URL, 'ses_other', 'local')).toEqual([])
    db.close()
  })

  it('orders duplicate matches by last_seen_at DESC', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    const older = registerOpencode(repo, { name: 'older', session_id: 'ses_dup' })
    const newer = registerOpencode(repo, { name: 'newer', session_id: 'ses_dup' })
    db.prepare(`UPDATE agents SET last_seen_at=? WHERE agent_id=?`).run(
      '2024-01-01T00:00:00.000Z',
      older,
    )
    db.prepare(`UPDATE agents SET last_seen_at=? WHERE agent_id=?`).run(
      '2024-06-01T00:00:00.000Z',
      newer,
    )

    expect(
      repo.findByOpencodeSession(BASE_URL, 'ses_dup', 'local').map(r => r.name)
    ).toEqual(['newer', 'older'])
    db.close()
  })

  it('ignores another device and non-opencode delivery rows', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    registerOpencode(repo, {
      name: 'remote',
      session_id: 'ses_rem',
      device: 'gx',
    })
    repo.register({
      agent_type: 'claude-code',
      name: 'claude',
      team: 'default',
      delivery: { kind: 'none' },
    })

    expect(repo.findByOpencodeSession(BASE_URL, 'ses_rem', 'local')).toEqual([])
    db.close()
  })

  it('ignores malformed opencode delivery payloads instead of throwing', () => {
    const { dir, db, repo } = freshRepo()
    cleanups.push(dir)
    db.prepare(
      `INSERT INTO agents (
         agent_id, agent_type, device, team, role, name, registered_at,
         last_seen_at, delivery_kind, delivery_payload
       ) VALUES ('broken', 'opencode', 'local', 'default', 'worker', 'broken',
         '2024-01-01T00:00:00.000Z', '2024-01-01T00:00:00.000Z',
         'opencode-server', 'not-json')`
    ).run()

    expect(repo.findByOpencodeSession(BASE_URL, 'ses_any', 'local')).toEqual([])
    db.close()
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-agents-identity-tn-'))

describe('AgentsRepo identity is (device, team, name)', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function fresh(): { db: ReturnType<typeof openDb>; repo: AgentsRepo } {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return { db, repo: new AgentsRepo(db) }
  }

  it('findByIdentity takes {device, team, name} and returns the row when it exists', () => {
    const { db, repo } = fresh()
    const r = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    expect('agent_id' in r).toBe(true)
    const id = (r as { agent_id: string }).agent_id
    const found = repo.findByIdentity({ device: 'local', team: 'default', name: 'alice' })
    expect(found?.agent_id).toBe(id)
    void db
  })

  it('register returns same agent_id when (device, team, name) matches, regardless of role', () => {
    const { repo } = fresh()
    const r1 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id1 = (r1 as { agent_id: string }).agent_id
    const r2 = repo.register({ name: 'alice', role: 'frontend', team: 'default', model: 'sonnet' })
    const id2 = (r2 as { agent_id: string }).agent_id
    expect(id2).toBe(id1)
  })

  it('register allows the same team and name on different devices', () => {
    const { db, repo } = fresh()
    const r1 = repo.register({ device: 'jt', name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const r2 = repo.register({ device: 'gx', name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id1 = (r1 as { agent_id: string }).agent_id
    const id2 = (r2 as { agent_id: string }).agent_id
    expect(id2).not.toBe(id1)
    const rows = db.prepare(`SELECT device, team, name FROM agents WHERE team='default' AND name='alice' ORDER BY device`).all()
    expect(rows).toEqual([
      { device: 'gx', team: 'default', name: 'alice' },
      { device: 'jt', team: 'default', name: 'alice' },
    ])
  })

  it('role change updates existing row in place (single row, new role, same agent_id)', () => {
    const { db, repo } = fresh()
    repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    repo.register({ name: 'alice', role: 'frontend', team: 'default', model: 'opus' })
    const rows = db.prepare(`SELECT agent_id, role FROM agents WHERE team='default' AND name='alice'`).all() as Array<{ agent_id: string; role: string }>
    expect(rows).toHaveLength(1)
    expect(rows[0].role).toBe('frontend')
  })

  it('role change preserves registered_at and last_processed_event_id', () => {
    const { db, repo } = fresh()
    const r1 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id1 = (r1 as { agent_id: string }).agent_id
    const row1 = db.prepare(`SELECT registered_at FROM agents WHERE agent_id=?`).get(id1) as { registered_at: string }
    db.prepare(`UPDATE agents SET last_processed_event_id=? WHERE agent_id=?`).run(5, id1)
    repo.register({ name: 'alice', role: 'frontend', team: 'default', model: 'opus' })
    const row2 = db.prepare(`SELECT registered_at, last_processed_event_id FROM agents WHERE agent_id=?`).get(id1) as { registered_at: string; last_processed_event_id: number }
    expect(row2.registered_at).toBe(row1.registered_at)
    expect(row2.last_processed_event_id).toBe(5)
  })

  it('tmux_pane_id update on reuse when provided', () => {
    const { db, repo } = fresh()
    const r = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus', tmux_pane_id: '%42' })
    const id = (r as { agent_id: string }).agent_id
    repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus', tmux_pane_id: '%99' })
    const row = db.prepare(`SELECT tmux_pane_id FROM agents WHERE agent_id=?`).get(id) as { tmux_pane_id: string }
    expect(row.tmux_pane_id).toBe('%99')
  })

  it('tmux_pane_id preserved on reuse when omitted', () => {
    const { db, repo } = fresh()
    const r = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus', tmux_pane_id: '%42' })
    const id = (r as { agent_id: string }).agent_id
    repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const row = db.prepare(`SELECT tmux_pane_id FROM agents WHERE agent_id=?`).get(id) as { tmux_pane_id: string }
    expect(row.tmux_pane_id).toBe('%42')
  })

  it('team change produces a new agent_id', () => {
    const { repo } = fresh()
    const r1 = repo.register({ name: 'alice', role: 'backend', team: 'default', model: 'opus' })
    const id1 = (r1 as { agent_id: string }).agent_id
    const r2 = repo.register({ name: 'alice', role: 'backend', team: 'alpha', model: 'opus' })
    const id2 = (r2 as { agent_id: string }).agent_id
    expect(id2).not.toBe(id1)
  })
})

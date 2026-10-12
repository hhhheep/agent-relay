// Tripwire: pins canonical self-poke semantics keyed on agent_id; do not relax.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

vi.mock('../src/daemon/tmux-cli.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/daemon/tmux-cli.js')>()
  return {
    ...actual,
    isTmuxAvailable: vi.fn(async () => true),
    capturePaneTail: vi.fn(async () => 'tail'),
    loadBuffer: vi.fn(async () => {}),
    pasteBuffer: vi.fn(async () => {}),
    sendEnter: vi.fn(async () => {})
  }
})

import { poke } from '../src/mcp/poke.js'
import { fakePaneSnapshot } from './helpers/pane-snapshot.js'

// Self-poke semantics are keyed on agent_id, but the paste is still gated by
// host verification — give it a snapshot so these cases reach that decision.
const snapshot = fakePaneSnapshot([{ pane_id: '%1' }, { pane_id: '%2' }, { pane_id: '%42' }])

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-poke-self-distinct-'))

function seed(db: ReturnType<typeof openDb>, agent_id: string, team: string, name: string, pane: string | null): void {
  const now = new Date().toISOString()
  db.prepare(
    `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
     VALUES (?,?,?,?,?,?,?,?,?)`
  ).run(agent_id, 'local', team, 'dev', name, null, now, now, pane)
}

describe('poke() distinct agents are never self-poke', () => {
  const dirs: string[] = []
  beforeEach(() => { process.env.POKE_QUIET_MS = '50' })
  afterEach(() => {
    dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0
    delete process.env.POKE_QUIET_MS
  })

  function fresh(): ReturnType<typeof openDb> {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return db
  }

  it('fully independent agents (different name, different pane) are not self-poke', async () => {
    const db = fresh()
    seed(db, 'A', 'default', 'alice', '%1')
    seed(db, 'B', 'default', 'bob', '%2')
    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })
    expect(res).not.toMatchObject({ error: 'self_poke_denied' })
    expect('ok' in res && res.ok).toBe(true)
  })

  it('different name but colliding tmux_pane_id are not self-poke', async () => {
    const db = fresh()
    seed(db, 'A', 'default', 'alice', '%42')
    seed(db, 'B', 'default', 'bob', '%42')
    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })
    expect(res).not.toMatchObject({ error: 'self_poke_denied' })
    expect('ok' in res && res.ok).toBe(true)
  })

  it('different name with both pane null are not self-poke', async () => {
    const db = fresh()
    seed(db, 'A', 'default', 'alice', null)
    seed(db, 'B', 'default', 'bob', null)
    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })
    expect(res).not.toMatchObject({ error: 'self_poke_denied' })
    expect(res).toMatchObject({ error: 'tmux_pane_not_set' })
  })
})

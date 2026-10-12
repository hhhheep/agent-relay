import { describe, it, expect, vi, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

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

import { createAutoPokeImpl } from '../src/mcp/tools.js'
import { fakePaneSnapshot } from './helpers/pane-snapshot.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-autopoke-xteam-'))

describe('createAutoPokeImpl cross-team', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function seed(db: ReturnType<typeof openDb>, agent_id: string, team: string, name: string, pane: string | null): void {
    const now = new Date().toISOString()
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).run(agent_id, 'local', team, 'r', name, null, now, now, pane)
  }

  it('cross-team fan-out poke succeeds (not guard_failed via cross_team_denied)', async () => {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    seed(db, 'A', 'alpha', 'alice', '%pA')
    seed(db, 'B', 'beta', 'bob', '%pB')

    const autoPoke = createAutoPokeImpl(db, new AgentsRepo(db))
    const res = await autoPoke({
      team: 'beta',
      fromAgentId: 'A',
      targetAgentId: 'B',
      paneId: '%pB',
      body: 'anything (body stays in mailbox; poke injects hint only)',
      paneSnapshot: fakePaneSnapshot([{ pane_id: '%pB' }])
    })
    expect(res).toEqual({ ok: true })
  })
})

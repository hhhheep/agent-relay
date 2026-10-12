import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import * as tmuxCli from '../src/daemon/tmux-cli.js'
import { __setCapturePaneTail, __resetCapturePaneTail } from '../src/mcp/poke-guard.js'
import { poke } from '../src/mcp/poke.js'
import { fakePaneSnapshot } from './helpers/pane-snapshot.js'

// Host verification runs before the paste, so the pane must be visible in a
// snapshot; without one every route below is undecidable and never reaches the
// tmux-cli mocks these cases are about.
const snapshot = fakePaneSnapshot([{ pane_id: '%1' }, { pane_id: '%2' }])

vi.mock('../src/daemon/tmux-cli.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/daemon/tmux-cli.js')>()
  return {
    ...actual,
    isTmuxAvailable: vi.fn(async () => true),
    capturePaneTail: vi.fn(async () => 'paste-tail'),
    loadBuffer: vi.fn(async () => {}),
    pasteBuffer: vi.fn(async () => {}),
    sendEnter: vi.fn(async () => {}),
  }
})

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-poke-guard-prim-'))

function seed(db: ReturnType<typeof openDb>, agent_id: string, name: string, pane: string | null): void {
  const now = new Date().toISOString()
  db.prepare(
    `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
     VALUES (?,?,?,?,?,?,?,?,?)`
  ).run(agent_id, 'local', 'default', 'dev', name, null, now, now, pane)
}

describe('tmuxPokeImpl runs the quiet-guard on the paste branch', () => {
  const dirs: string[] = []
  beforeEach(() => { vi.clearAllMocks(); process.env.POKE_QUIET_MS = '50' })
  afterEach(() => {
    dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0
    __resetCapturePaneTail()
    delete process.env.POKE_QUIET_MS
  })

  function fresh(): ReturnType<typeof openDb> {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return db
  }

  it('active pane: returns guard_failed and issues no paste/Enter', async () => {
    // distinct tail each capture => guard detects activity
    __setCapturePaneTail(async (paneId: string) => `active-${paneId}-${Math.random()}`)
    const db = fresh()
    seed(db, 'A', 'alice', '%1')
    seed(db, 'B', 'bob', '%2')

    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })

    expect(res).toEqual({ error: 'guard_failed', transport_used: 'tmux-poke' })
    expect(vi.mocked(tmuxCli.loadBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.pasteBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.sendEnter)).not.toHaveBeenCalled()
  })

  it('pane dies during the guard window: classified as pane_dead, not thrown', async () => {
    __setCapturePaneTail(async (paneId: string) => {
      throw new Error(`can't find pane: ${paneId}`)
    })
    const db = fresh()
    seed(db, 'A', 'alice', '%1')
    seed(db, 'B', 'bob', '%2')

    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })

    expect(res).toEqual({ error: 'pane_dead', detail: expect.any(String), transport_used: 'tmux-poke' })
    expect(vi.mocked(tmuxCli.loadBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.pasteBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.sendEnter)).not.toHaveBeenCalled()
  })

  it('idle pane: pastes normally', async () => {
    __setCapturePaneTail(async (paneId: string) => `idle-${paneId}`)
    const db = fresh()
    seed(db, 'A', 'alice', '%1')
    seed(db, 'B', 'bob', '%2')

    const res = await poke({ db, callerAgentId: 'A', paneSnapshot: snapshot }, { target_agent_id: 'B', prompt: 'p' })

    expect('ok' in res && res.ok).toBe(true)
    expect(res).toMatchObject({ ok: true, transport_used: 'tmux-poke', pane_id: '%2' })
    expect(vi.mocked(tmuxCli.loadBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.pasteBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.sendEnter)).toHaveBeenCalledTimes(1)
  })

  it('skipGuard: pastes on an active pane without running the guard', async () => {
    const guardCaptures: string[] = []
    __setCapturePaneTail(async (paneId: string) => {
      guardCaptures.push(paneId)
      return `active-${paneId}-${Math.random()}`
    })
    const db = fresh()
    seed(db, 'A', 'alice', '%1')
    seed(db, 'B', 'bob', '%2')

    const res = await poke(
      { db, callerAgentId: 'A', paneSnapshot: snapshot },
      { target_agent_id: 'B', prompt: 'p', skipGuard: true }
    )

    expect('ok' in res && res.ok).toBe(true)
    expect(res).toMatchObject({ ok: true, transport_used: 'tmux-poke', pane_id: '%2' })
    // guard never ran => no capture via the guard's capture hook
    expect(guardCaptures).toHaveLength(0)
    expect(vi.mocked(tmuxCli.loadBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.pasteBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.sendEnter)).toHaveBeenCalledTimes(1)
  })
})

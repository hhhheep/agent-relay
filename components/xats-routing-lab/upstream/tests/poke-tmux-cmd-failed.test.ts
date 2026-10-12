import { describe, it, expect, afterEach, vi, beforeEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fakePaneSnapshot } from './helpers/pane-snapshot.js'

// Host verification precedes the paste; without a snapshot the pane is
// undecidable and loadBuffer never gets a chance to reject.
const snapshot = fakePaneSnapshot([{ pane_id: '%42' }])

vi.mock('../src/daemon/tmux-cli.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/daemon/tmux-cli.js')>()
  return {
    ...actual,
    isTmuxAvailable: vi.fn(async () => true),
    capturePaneTail: vi.fn(async () => 'before-tail'),
    loadBuffer: vi.fn(async () => { throw new Error('unexpected-x') }),
    pasteBuffer: vi.fn(async () => { /* not reached */ }),
    sendEnter: vi.fn(async () => { /* not reached */ })
  }
})

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-poke-cmd-fail-'))

describe('poke tmux_cmd_failed with stage info', () => {
  const cleanups: string[] = []
  beforeEach(() => { vi.clearAllMocks(); process.env.POKE_QUIET_MS = '50' })
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    delete process.env.POKE_QUIET_MS
  })

  it('returns tmux_cmd_failed with stage "load_buffer" when loadBuffer rejects unexpectedly', async () => {
    const { poke } = await import('../src/mcp/poke.js')
    const { openDb } = await import('../src/storage/db.js')
    const { applySchema } = await import('../src/storage/schema.js')

    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)

    db.prepare(`INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id) VALUES (?,?,?,?,?,?,?,?,?)`).run(
      'caller-a', 'local', 'default', 'dev', 'caller-a', 'opus-4-7', new Date().toISOString(), new Date().toISOString(), null
    )
    db.prepare(`INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id) VALUES (?,?,?,?,?,?,?,?,?)`).run(
      'target-b', 'local', 'default', 'dev', 'target-b', 'gpt-5', new Date().toISOString(), new Date().toISOString(), '%42'
    )

    const result = await poke(
      { db, callerAgentId: 'caller-a', paneSnapshot: snapshot },
      { target_agent_id: 'target-b', prompt: 'hi' }
    )

    expect(result).toEqual({
      error: 'tmux_cmd_failed',
      detail: { stage: 'load_buffer', stderr: 'unexpected-x' },
      transport_used: 'tmux-poke'
    })

    db.close()
  })
})

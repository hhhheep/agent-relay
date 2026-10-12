import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import * as tmuxCli from '../src/daemon/tmux-cli.js'
import { __setCapturePaneTail, __resetCapturePaneTail } from '../src/mcp/poke-guard.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { createAutoPokeImpl } from '../src/mcp/tools.js'
import { clearAllRetries } from '../src/mcp/poke-retry.js'
import { fakePaneSnapshot } from './helpers/pane-snapshot.js'

// Mock only the paste-flow primitives; the quiet-guard's capture is driven via
// __setCapturePaneTail so each test can toggle pane activity precisely.
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

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-guard-on-paste-'))

function setup(panes: Record<string, 'idle' | 'active'>): {
  svc: SendMessageService
  db: ReturnType<typeof openDb>
  fanout: ChannelWakeFanout
  guardCaptures: string[]
  setPane: (paneId: string, state: 'idle' | 'active') => void
  cleanup: () => void
} {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const agents = new AgentsRepo(db)
  const events = new EventsOutbox(db)
  const fanout = new ChannelWakeFanout()

  const paneState: Record<string, 'idle' | 'active'> = { ...panes }
  const guardCaptures: string[] = []
  __setCapturePaneTail(async (paneId: string) => {
    guardCaptures.push(paneId)
    const state = paneState[paneId] ?? 'idle'
    if (state === 'idle') return `idle-${paneId}`
    return `active-${paneId}-${Math.random()}`
  })

  const autoPokeImpl = createAutoPokeImpl(db, agents, fanout)
  const svc = new SendMessageService(db, agents, events, {
    poke: autoPokeImpl,
    tmuxAvailable: async () => true,
    paneSnapshot: fakePaneSnapshot(Object.keys(paneState).map(pane_id => ({ pane_id }))),
  })
  return {
    svc,
    db,
    fanout,
    guardCaptures,
    setPane: (paneId, state) => { paneState[paneId] = state },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  }
}

describe('send_message: quiet-guard lives in the tmux paste primitive', () => {
  const cleanups: Array<() => void> = []
  beforeEach(() => { vi.clearAllMocks(); process.env.POKE_QUIET_MS = '50' })
  afterEach(() => {
    clearAllRetries()
    if (vi.isFakeTimers()) vi.useRealTimers()
    cleanups.forEach(c => c()); cleanups.length = 0
    __resetCapturePaneTail()
    delete process.env.POKE_QUIET_MS
  })

  // 4.3
  it('claude-channel recipient with no live sink + active pane → guard_failed, retry, no paste', async () => {
    const { svc, db, fanout, cleanup } = setup({ '%B': 'active' })
    cleanups.push(cleanup)
    const a = db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?,?,?,?,?,?,?,?,?)`
    )
    const now = new Date().toISOString()
    a.run('A', 'local', 'default', 'dev', 'alice', 'm', now, now, '%A')
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id, delivery_kind, delivery_payload)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)`
    ).run('B', 'local', 'default', 'dev', 'bob', 'm', now, now, '%B', 'claude-channel', JSON.stringify({ channel_session_id: 'csid-b' }))

    // No sink attached for csid-b => channel offline => falls back to tmux paste.
    expect(fanout.has('csid-b')).toBe(false)

    const r = await svc.send({ from: 'A', to_agent_id: 'B', body: 'hi' })
    if ('error' in r) throw new Error('expected success')

    expect(r.poked).toBe(false)
    expect(r.poke_skip_reasons ?? []).toContainEqual({ agent_id: 'B', reason: 'guard_failed' })
    expect(r.retry_scheduled).toBe(true)
    expect(r.retry_delays_s).toEqual([30, 180, 600])
    // no paste reached the pane
    expect(vi.mocked(tmuxCli.loadBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.pasteBuffer)).not.toHaveBeenCalled()
    expect(vi.mocked(tmuxCli.sendEnter)).not.toHaveBeenCalled()
  })

  // 4.4
  it('channel sink online → delivered via channel, never reaches tmux, no guard runs', async () => {
    const { svc, db, fanout, guardCaptures, cleanup } = setup({ '%B': 'active' })
    cleanups.push(cleanup)
    const now = new Date().toISOString()
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).run('A', 'local', 'default', 'dev', 'alice', 'm', now, now, '%A')
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id, delivery_kind, delivery_payload)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)`
    ).run('B', 'local', 'default', 'dev', 'bob', 'm', now, now, '%B', 'claude-channel', JSON.stringify({ channel_session_id: 'csid-b' }))

    const emitted: unknown[] = []
    fanout.attach('csid-b', (p) => emitted.push(p), 'sess-P')

    const r = await svc.send({ from: 'A', to_agent_id: 'B', body: 'hi' })
    if ('error' in r) throw new Error('expected success')

    expect(r.poked).toBe(true)
    expect(r.poke_skip_reasons ?? []).toEqual([])
    expect(r.retry_scheduled).toBe(false)
    expect(emitted).toHaveLength(1)
    // delivery via channel never reaches the tmux branch, so the guard never runs
    expect(guardCaptures).toHaveLength(0)
    expect(vi.mocked(tmuxCli.pasteBuffer)).not.toHaveBeenCalled()
  })

  // 4.5
  it('retry tick whose own guard passes fires the poke with skipGuard (no second guard window)', async () => {
    vi.useFakeTimers()
    const { svc, db, guardCaptures, setPane, cleanup } = setup({ '%B': 'active' })
    cleanups.push(cleanup)
    const now = new Date().toISOString()
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).run('A', 'local', 'default', 'dev', 'alice', 'm', now, now, '%A')
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).run('B', 'local', 'default', 'dev', 'bob', 'm', now, now, '%B')

    const sendPromise = svc.send({ from: 'A', to_agent_id: 'B', body: 'hi' })
    await vi.advanceTimersByTimeAsync(100)
    const r = await sendPromise
    if ('error' in r) throw new Error('expected success')
    expect(r.retry_scheduled).toBe(true)
    // initial guard ran (2 captures, before+after) and failed; no paste yet
    expect(vi.mocked(tmuxCli.pasteBuffer)).not.toHaveBeenCalled()

    // Flip pane idle so the retry tick's own guard passes, then fire the tick.
    // Advance past the 30s backoff, the tick's 50ms guard window, and the
    // primitive's paste-settle delays (~800ms) so pokeFn fully resolves.
    setPane('%B', 'idle')
    guardCaptures.length = 0
    await vi.advanceTimersByTimeAsync(31_500)

    // The retry tick ran exactly ONE guard (its own: before+after = 2 captures).
    // The primitive poke fired with skipGuard:true, so it did NOT open a second
    // guard window (which would add 2 more captures).
    expect(guardCaptures).toHaveLength(2)
    expect(vi.mocked(tmuxCli.loadBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.pasteBuffer)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(tmuxCli.sendEnter)).toHaveBeenCalledTimes(1)

    const status = db.prepare(
      'SELECT wake_status FROM message_delivery_status WHERE message_id=? AND agent_id=?'
    ).get(r.message_id, 'B') as { wake_status: string }
    expect(status.wake_status).toBe('delivered')

    vi.useRealTimers()
  })
})

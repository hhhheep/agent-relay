import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { insertAgent } from './helpers/insert-agent.js'

vi.mock('../src/mcp/poke.js', () => ({
  poke: vi.fn(async (_deps: unknown, _input: { target_agent_id: string; prompt: string }) => ({
    ok: true as const,
    transport_used: 'tmux-poke' as const,
    pane_id: '%mock',
    pane_tail_before: '',
    pane_tail_after: ''
  }))
}))

import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { __setCapturePaneTail, __resetCapturePaneTail, runQuietGuard } from '../src/mcp/poke-guard.js'
import { clearAllRetries } from '../src/mcp/poke-retry.js'
import { createAutoPokeImpl } from '../src/mcp/tools.js'
import { poke as pokeMock } from '../src/mcp/poke.js'

interface PokeCall { target: string; prompt: string }

function setupService(opts?: { paneState?: Record<string, 'idle' | 'active'> }): {
  svc: SendMessageService
  db: ReturnType<typeof openDb>
  pokeCalls: PokeCall[]
  setPaneState: (paneId: string, state: 'idle' | 'active') => void
  cleanup: () => void
} {
  const dir = mkdtempSync(join(tmpdir(), 'atm-sm-xteam-autopoke-'))
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const agents = new AgentsRepo(db)
  const events = new EventsOutbox(db)

  const panes: Record<string, 'idle' | 'active'> = { ...(opts?.paneState ?? {}) }
  __setCapturePaneTail(async (paneId: string) => {
    const state = panes[paneId] ?? 'idle'
    if (state === 'idle') return `idle-${paneId}`
    return `active-${paneId}-${Math.random()}`
  })

  const autoPokeImpl = createAutoPokeImpl(db, agents)
  const pokeCalls: PokeCall[] = []
  // The mock stands in for the real poke primitive, which runs the quiet-guard on
  // the tmux paste branch unless skipGuard is set. Mirror that so active panes
  // resolve to guard_failed (and the retry tick's skipGuard:true poke fires).
  vi.mocked(pokeMock).mockImplementation(async (deps: { db: ReturnType<typeof openDb> }, input: { target_agent_id: string; prompt: string; skipGuard?: boolean }) => {
    if (!input.skipGuard) {
      const row = deps.db
        .prepare('SELECT tmux_pane_id FROM agents WHERE agent_id=?')
        .get(input.target_agent_id) as { tmux_pane_id: string | null } | undefined
      if (row?.tmux_pane_id && (await runQuietGuard(row.tmux_pane_id)) === 'fail') {
        return { error: 'guard_failed', transport_used: 'tmux-poke' as const }
      }
    }
    pokeCalls.push({ target: input.target_agent_id, prompt: input.prompt })
    return { ok: true as const, transport_used: 'tmux-poke' as const, pane_id: '%mock', pane_tail_before: '', pane_tail_after: '' }
  })

  const svc = new SendMessageService(db, agents, events, {
    poke: autoPokeImpl,
    tmuxAvailable: async () => true
  })
  return {
    svc,
    db,
    pokeCalls,
    setPaneState: (paneId, state) => { panes[paneId] = state },
    cleanup: () => rmSync(dir, { recursive: true, force: true })
  }
}

describe('send_message cross-team auto-poke + retry', () => {
  const cleanups: Array<() => void> = []
  beforeEach(() => {
    process.env.POKE_QUIET_MS = '50'
    vi.mocked(pokeMock).mockClear()
  })
  afterEach(() => {
    clearAllRetries()
    if (vi.isFakeTimers()) vi.useRealTimers()
    cleanups.forEach(c => c()); cleanups.length = 0
    __resetCapturePaneTail()
    delete process.env.POKE_QUIET_MS
  })

  it('cross-team send auto-pokes recipient idle pane with hint-only (no team prefix, no body leak)', async () => {
    const { svc, db, pokeCalls, cleanup } = setupService({ paneState: { '%B': 'idle' } })
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', team: 'alpha', name: 'lead-alpha', tmux_pane_id: '%A' })
    insertAgent(db, { agent_id: 'B', team: 'beta', name: 'worker-beta', tmux_pane_id: '%B' })

    const r = await svc.send({ from: 'A', to_agent_id: 'B', to_team: 'beta', body: 'secret:token=xyz' })
    if ('error' in r) throw new Error(`expected success, got ${r.error}`)

    expect(r.poked).toBe(true)
    expect(pokeCalls).toHaveLength(1)
    expect(pokeCalls[0].target).toBe('B')
    expect(pokeCalls[0].prompt).toContain('新邮件 from lead-alpha')
    expect(pokeCalls[0].prompt).not.toContain('token=xyz')
    // No team prefix on the SENDER segment (cross-team sender name is bare,
    // same format as same-team); the target segment carries the TARGET's team.
    expect(pokeCalls[0].prompt).not.toContain('alpha/')
    expect(pokeCalls[0].prompt).not.toContain('[alpha]')
    expect(pokeCalls[0].prompt).toContain('→ worker-beta@beta')
  })

  it('cross-team send guard_failed schedules retries; retry lookup works without team filter', async () => {
    // Fake timers must be active BEFORE the initial send, so the retry timer (30s)
    // is registered on the fake clock and can be advanced later.
    vi.useFakeTimers()
    const { svc, db, pokeCalls, setPaneState, cleanup } = setupService({ paneState: { '%B': 'active' } })
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', team: 'alpha', name: 'lead-alpha', tmux_pane_id: '%A' })
    insertAgent(db, { agent_id: 'B', team: 'beta', name: 'worker-beta', tmux_pane_id: '%B' })

    // Kick off the send and concurrently advance past the quiet guard window (50ms).
    const sendPromise = svc.send({ from: 'A', to_agent_id: 'B', to_team: 'beta', body: 'hi' })
    await vi.advanceTimersByTimeAsync(100)
    const r = await sendPromise
    if ('error' in r) throw new Error(`expected success, got ${r.error}`)

    expect(r.retry_scheduled).toBe(true)
    expect(r.retry_delays_s).toEqual([30, 180, 600])
    expect(pokeCalls).toHaveLength(0)

    // Flip pane to idle; at retry tick, recipient lookup (by agent_id alone) should find
    // B even though B lives in team 'beta' (not caller's team 'alpha').
    setPaneState('%B', 'idle')
    // Advance past the 30s retry backoff; the tick's inner quiet-guard 50ms wait
    // falls within the same window.
    await vi.advanceTimersByTimeAsync(30_500)

    expect(pokeCalls).toHaveLength(1)
    expect(pokeCalls[0].target).toBe('B')
    expect(pokeCalls[0].prompt).toContain('新邮件 from lead-alpha')
    // The retry tick re-resolves the cross-team target, not the caller's team.
    expect(pokeCalls[0].prompt).toContain('→ worker-beta@beta')

    vi.useRealTimers()
  })

  it('cross-team send treats Codex transport success as poked', async () => {
    const { svc, db, cleanup } = setupService({ paneState: { '%B': 'idle' } })
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', team: 'alpha', name: 'lead-alpha', tmux_pane_id: '%A' })
    insertAgent(db, {
      agent_id: 'B',
      team: 'beta',
      name: 'worker-beta',
      delivery: {
        kind: 'codex-appserver',
        thread_id: '11111111-1111-4111-8111-111111111111',
        ws_url: 'ws://127.0.0.1:8799',
      },
    })

    vi.mocked(pokeMock).mockImplementationOnce(async (_deps: unknown, input: { target_agent_id: string; prompt: string }) => {
      return {
        ok: true as const,
        transport_used: 'codex-appserver' as const,
        thread_id: '11111111-1111-4111-8111-111111111111',
      }
    })

    const r = await svc.send({ from: 'A', to_agent_id: 'B', to_team: 'beta', body: 'hello' })
    if ('error' in r) throw new Error(`expected success, got ${r.error}`)

    expect(r.poked).toBe(true)
    expect(r.retry_scheduled).toBe(false)
  })
})

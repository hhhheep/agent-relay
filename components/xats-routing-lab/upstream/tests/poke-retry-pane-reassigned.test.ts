import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  scheduleRetry,
  clearAllRetries,
  __peekRetryMap,
  type RetryContext
} from '../src/mcp/poke-retry.js'

interface StatusCall {
  agentId: string
  wake_status: string
  skip_reason?: string | null
}

// A pane can change hands between the initial guard_failed and the retry tick.
// The tick then injects nothing, and the wake status must say so.
function makeCtx(overrides: Partial<RetryContext>): RetryContext {
  const base: RetryContext = {
    agentId: 'B',
    messageId: 'm1',
    fromAgentId: 'A',
    body: 'hi',
    team: 'default',
    sentAt: '2020-01-01T00:00:00.000Z',
    paneId: '%19',
    paneGuardFn: async () => 'pass',
    pokeFn: async () => ({ ok: true as const }),
    lookupAgentFn: () => ({
      agent_id: 'B',
      tmux_pane_id: '%19',
      last_seen_at: '2019-12-31T00:00:00.000Z'
    })
  }
  return { ...base, ...overrides }
}

describe('retry tick honours a pane_reassigned outcome', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    clearAllRetries()
  })
  afterEach(() => {
    clearAllRetries()
    vi.useRealTimers()
  })

  it('records skipped, not delivered, when the retry poke finds the pane reassigned', async () => {
    const statuses: StatusCall[] = []
    scheduleRetry(makeCtx({
      pokeFn: async () => ({ ok: false as const, reason: 'pane_reassigned' }),
      updateStatusFn: (s) => { statuses.push(s as StatusCall) }
    }))

    await vi.advanceTimersByTimeAsync(30_000)

    const terminal = statuses.at(-1)
    expect(terminal?.wake_status).toBe('skipped')
    expect(terminal?.skip_reason).toBe('pane_reassigned')
    expect(statuses.some(s => s.wake_status === 'delivered')).toBe(false)
  })

  it('stops retrying — a changed host does not revert on a timer', async () => {
    const pokeFn = vi.fn(async () => ({ ok: false as const, reason: 'pane_reassigned' }))
    scheduleRetry(makeCtx({ pokeFn }))

    await vi.advanceTimersByTimeAsync(30_000)
    expect(pokeFn).toHaveBeenCalledTimes(1)
    expect(__peekRetryMap().size).toBe(0)

    await vi.advanceTimersByTimeAsync(600_000)
    expect(pokeFn).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['tmux_unavailable', 'skipped', 'tmux_unavailable'],
    ['no_pane', 'skipped', 'no_pane'],
  ])('a %s outcome is skipped, never delivered', async (reason, status, skipReason) => {
    const statuses: StatusCall[] = []
    scheduleRetry(makeCtx({
      pokeFn: async () => ({ ok: false as const, reason }),
      updateStatusFn: (s) => { statuses.push(s as StatusCall) }
    }))

    await vi.advanceTimersByTimeAsync(30_000)

    expect(statuses.at(-1)?.wake_status).toBe(status)
    expect(statuses.at(-1)?.skip_reason).toBe(skipReason)
    expect(statuses.some(s => s.wake_status === 'delivered')).toBe(false)
  })

  it('an ok:false with no reason at all is never delivered', async () => {
    const statuses: StatusCall[] = []
    scheduleRetry(makeCtx({
      pokeFn: async () => ({ ok: false as const }),
      updateStatusFn: (s) => { statuses.push(s as StatusCall) }
    }))

    // Unknown failures keep the backoff rather than inventing a terminal state.
    await vi.advanceTimersByTimeAsync(30_000 + 180_000 + 600_000)

    expect(statuses.some(s => s.wake_status === 'delivered')).toBe(false)
    expect(statuses.at(-1)?.skip_reason).toBe('retry_exhausted')
  })

  it('still records delivered when the retry poke succeeds', async () => {
    const statuses: StatusCall[] = []
    scheduleRetry(makeCtx({ updateStatusFn: (s) => { statuses.push(s as StatusCall) } }))

    await vi.advanceTimersByTimeAsync(30_000)

    expect(statuses.at(-1)?.wake_status).toBe('delivered')
  })

  it('treats a void-returning legacy pokeFn as delivered', async () => {
    const statuses: StatusCall[] = []
    scheduleRetry(makeCtx({
      pokeFn: async () => { /* pre-existing callers return nothing */ },
      updateStatusFn: (s) => { statuses.push(s as StatusCall) }
    }))

    await vi.advanceTimersByTimeAsync(30_000)

    expect(statuses.at(-1)?.wake_status).toBe('delivered')
  })
})

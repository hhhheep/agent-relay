import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { fanoutAutoPoke } from '../src/mcp/auto-poke-fanout.js'
import type { AutoPokeFn } from '../src/mcp/auto-poke-fanout.js'
import { __peekRetryMap, clearAllRetries } from '../src/mcp/poke-retry.js'
import { __setCapturePaneTail, __resetCapturePaneTail } from '../src/mcp/poke-guard.js'
import { guardingPoke } from './helpers/guarding-poke.js'

describe('fanoutAutoPoke: guard_failed recipients get retry scheduled', () => {
  beforeEach(() => { process.env.POKE_QUIET_MS = '50' })
  afterEach(() => {
    clearAllRetries()
    __resetCapturePaneTail()
    delete process.env.POKE_QUIET_MS
  })

  it('single recipient with guard_failed → retry scheduled in retry map; retryScheduledCount=1', async () => {
    __setCapturePaneTail(async (paneId: string) => `active-${paneId}-${Math.random()}`)
    const pokeCalls: Array<{ target: string }> = []
    const poke: AutoPokeFn = guardingPoke(
      async () => ({ ok: true }),
      (a) => { pokeCalls.push({ target: a.targetAgentId }) }
    )

    const r = await fanoutAutoPoke({
      team: 'default',
      fromAgentId: 'A',
      recipients: [{ agent_id: 'B', tmux_pane_id: '%2' }],
      body: 'hi',
      deps: { poke, tmuxAvailable: async () => true },
      retry: {
        messageId: 'm1',
        sentAt: '2020-01-01T00:00:00.000Z',
        lookupAgentFn: (id: string) => ({ agent_id: id, tmux_pane_id: '%2', last_seen_at: '2019-12-31T00:00:00.000Z' })
      }
    })

    expect(r.poked).toBe(false)
    expect(r.skipReasons.find(x => x.agent_id === 'B')?.reason).toBe('guard_failed')
    expect(r.retryScheduledCount).toBe(1)
    const map = __peekRetryMap()
    expect(map.has('m1:B')).toBe(true)
  })

  it('recipient with no pane_id → no retry scheduled; retryScheduledCount=0', async () => {
    __setCapturePaneTail(async () => 'idle')
    const poke: AutoPokeFn = async () => ({ ok: true })

    const r = await fanoutAutoPoke({
      team: 'default',
      fromAgentId: 'A',
      recipients: [{ agent_id: 'B', tmux_pane_id: null }],
      body: 'hi',
      deps: { poke, tmuxAvailable: async () => true },
      retry: {
        messageId: 'm2',
        sentAt: '2020-01-01T00:00:00.000Z',
        lookupAgentFn: (id: string) => ({ agent_id: id, tmux_pane_id: null, last_seen_at: '2019-12-31T00:00:00.000Z' })
      }
    })

    expect(r.retryScheduledCount).toBe(0)
    expect(__peekRetryMap().size).toBe(0)
  })

  it('mixed recipients: guard_failed + no_pane → retry map has exactly one entry for the guard_failed agent', async () => {
    __setCapturePaneTail(async (paneId: string) => {
      if (paneId === '%3') return `active-${paneId}-${Math.random()}`
      return `idle-${paneId}`
    })
    const poke: AutoPokeFn = guardingPoke(async () => ({ ok: true }))

    const r = await fanoutAutoPoke({
      team: 'default',
      fromAgentId: 'A',
      recipients: [
        { agent_id: 'C', tmux_pane_id: '%3' },
        { agent_id: 'D', tmux_pane_id: null }
      ],
      body: 'hi',
      deps: { poke, tmuxAvailable: async () => true },
      retry: {
        messageId: 'm3',
        sentAt: '2020-01-01T00:00:00.000Z',
        lookupAgentFn: (id: string) => ({ agent_id: id, tmux_pane_id: id === 'C' ? '%3' : null, last_seen_at: '2019-12-31T00:00:00.000Z' })
      }
    })

    expect(r.retryScheduledCount).toBe(1)
    const map = __peekRetryMap()
    expect(map.size).toBe(1)
    expect(map.has('m3:C')).toBe(true)
    expect(map.has('m3:D')).toBe(false)
  })

  it('without retry ctx (legacy callers): retryScheduledCount=0, no map entries', async () => {
    __setCapturePaneTail(async (paneId: string) => `active-${paneId}-${Math.random()}`)
    const poke: AutoPokeFn = guardingPoke(async () => ({ ok: true }))

    const r = await fanoutAutoPoke({
      team: 'default',
      fromAgentId: 'A',
      recipients: [{ agent_id: 'B', tmux_pane_id: '%2' }],
      body: 'hi',
      deps: { poke, tmuxAvailable: async () => true }
    })

    expect(r.skipReasons.find(x => x.agent_id === 'B')?.reason).toBe('guard_failed')
    expect(r.retryScheduledCount).toBe(0)
    expect(__peekRetryMap().size).toBe(0)
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'

const { listPanesSpy } = vi.hoisted(() => ({ listPanesSpy: vi.fn(async () => []) }))

vi.mock('../src/daemon/tmux-pane-list.js', () => ({
  listTmuxPaneRows: listPanesSpy,
  listTmuxPaneIds: async () => new Set<string>(),
}))

import { fanoutAutoPoke, type AutoPokeArgs } from '../src/mcp/auto-poke-fanout.js'

describe('one tmux pane snapshot per fan-out round', () => {
  beforeEach(() => { listPanesSpy.mockClear() })

  it('every recipient resolves the same snapshot from a single tmux query', async () => {
    const seen: Array<AutoPokeArgs['paneSnapshot']> = []
    const result = await fanoutAutoPoke({
      team: 't',
      fromAgentId: 'sender',
      recipients: ['a', 'b', 'c', 'd'].map(id => ({ agent_id: id, tmux_pane_id: `%${id}` })),
      body: 'hi',
      deps: {
        tmuxAvailable: async () => true,
        poke: async (args) => {
          seen.push(args.paneSnapshot)
          await args.paneSnapshot?.()
          return { ok: true }
        },
      },
    })

    expect(result.deliveredAgentIds).toHaveLength(4)
    expect(seen).toHaveLength(4)
    expect(seen.every(loader => loader === seen[0])).toBe(true)
    expect(listPanesSpy).toHaveBeenCalledTimes(1)
  })

  it('a round whose recipients never reach tmux takes no snapshot at all', async () => {
    await fanoutAutoPoke({
      team: 't',
      fromAgentId: 'sender',
      recipients: [{ agent_id: 'a', tmux_pane_id: null }],
      body: 'hi',
      deps: { tmuxAvailable: async () => true, poke: async () => ({ ok: true }) },
    })
    expect(listPanesSpy).not.toHaveBeenCalled()
  })
})

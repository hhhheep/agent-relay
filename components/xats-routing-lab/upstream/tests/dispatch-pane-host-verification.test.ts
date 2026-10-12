import { describe, it, expect } from 'vitest'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import {
  dispatchPoke,
  type DispatchDeps,
  type TargetRow,
  type TmuxPokeResult,
} from '../src/mcp/transport-dispatch.js'

interface TmuxStub {
  calls: Array<{ pane_id: string; content: string }>
  fn: DispatchDeps['tmuxPoke']
}

function stubTmux(): TmuxStub {
  const stub: TmuxStub = {
    calls: [],
    fn: async (args) => {
      stub.calls.push({ pane_id: args.pane_id, content: args.content })
      return { ok: true, pane_tail_before: 'b', pane_tail_after: 'a' } as TmuxPokeResult
    },
  }
  return stub
}

function target(overrides: Partial<TargetRow>): TargetRow {
  return {
    agent_id: 'target',
    agent_type: null,
    device: 'jt',
    delivery: { kind: 'none' },
    tmux_pane_id: '%19',
    runtime_ui_pid: null,
    ...overrides,
  }
}

const rejectHost: DispatchDeps['verifyPaneHost'] =
  async () => ({ ok: false, reason: 'pane_reassigned' })

// Every route below reaches tmux only through dispatchTmux, so a single failing
// verifier must stop all five without a byte reaching the pane.
const routes: Array<{ name: string; row: TargetRow; deps: Partial<DispatchDeps> }> = [
  {
    name: 'claude-code channel fallback',
    row: target({
      agent_type: 'claude-code',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-dead' },
    }),
    deps: { channelWakeFanout: new ChannelWakeFanout() },
  },
  {
    name: 'codex legacy fallback',
    row: target({ agent_type: 'codex' }),
    deps: {},
  },
  {
    name: 'opencode legacy fallback',
    row: target({ agent_type: 'opencode' }),
    deps: {},
  },
  {
    name: 'kimi legacy fallback',
    row: target({ agent_type: 'kimi-code' }),
    deps: {},
  },
  {
    name: 'unknown agent_type fallback',
    row: target({ agent_type: null }),
    deps: {},
  },
]

describe('dispatchPoke tmux host verification', () => {
  for (const route of routes) {
    it(`${route.name}: a failing host check injects nothing`, async () => {
      const tmux = stubTmux()
      const res = await dispatchPoke(
        { tmuxPoke: tmux.fn, verifyPaneHost: rejectHost, ...route.deps },
        route.row,
        { content: 'hi', meta: {} }
      )
      expect(res).toEqual({ error: 'pane_reassigned', transport_used: 'tmux-poke' })
      expect(tmux.calls).toEqual([])
    })
  }

  it('codex-appserver failure still refuses the tmux fallback when the pane changed hands', async () => {
    const tmux = stubTmux()
    const res = await dispatchPoke(
      {
        tmuxPoke: tmux.fn,
        verifyPaneHost: rejectHost,
        codexAppserverDispatch: async () => ({
          error: 'codex_connect_failed',
          detail: 'ECONNREFUSED',
        }),
      },
      target({
        agent_type: 'codex',
        delivery: {
          kind: 'codex-appserver',
          thread_id: '11111111-1111-4111-8111-111111111111',
          ws_url: 'ws://127.0.0.1:8799',
        },
      }),
      { content: 'hi', meta: {} }
    )
    expect(res).toEqual({ error: 'pane_reassigned', transport_used: 'tmux-poke' })
    expect(tmux.calls).toEqual([])
  })

  it('a passing host check leaves the injection byte-for-byte unchanged', async () => {
    const tmux = stubTmux()
    const res = await dispatchPoke(
      { tmuxPoke: tmux.fn, verifyPaneHost: async () => ({ ok: true }) },
      target({}),
      { content: 'hi', meta: {} }
    )
    expect(res).toMatchObject({ ok: true, transport_used: 'tmux-poke', pane_id: '%19' })
    expect(tmux.calls).toEqual([{ pane_id: '%19', content: 'hi' }])
  })
})

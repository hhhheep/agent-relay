import { describe, expect, it, vi } from 'vitest'
import { verifyPaneHost } from '../src/mcp/pane-host-verify.js'
import { dispatchPoke } from '../src/mcp/transport-dispatch.js'
import { poke } from '../src/mcp/poke.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { insertAgent } from './helpers/insert-agent.js'
import { paneSnapshotOf } from './helpers/pane-snapshot.js'

const LOCAL = 'jt'

// The four review findings that turned "verification exists" into "verification
// actually gates the write". Each one injected on a state it could not justify.

describe('pane host verification is fail-closed on an unqueryable tmux', () => {
  it('a null snapshot is undecidable, not verified, for a live-pid row', async () => {
    const verdict = await verifyPaneHost({
      row: { agent_id: 'A', device: LOCAL, runtime_ui_pid: 4242 },
      paneId: '%19',
      paneSnapshot: null,
      localDevice: LOCAL,
      isProcessAlive: () => true,
    })
    expect(verdict).toEqual({ ok: false, reason: 'undecidable' })
  })

  it('a null snapshot is undecidable, not verified, for a pid-less row', async () => {
    const verdict = await verifyPaneHost({
      row: { agent_id: 'A', device: LOCAL, runtime_ui_pid: null },
      paneId: '%19',
      paneSnapshot: null,
      localDevice: LOCAL,
    })
    expect(verdict).toEqual({ ok: false, reason: 'undecidable' })
  })

  it('dispatch performs zero tmux writes and reports tmux_unavailable', async () => {
    const tmuxPoke = vi.fn(async () => ({
      ok: true as const,
      pane_tail_before: '',
      pane_tail_after: '',
    }))
    const result = await dispatchPoke(
      {
        tmuxPoke,
        verifyPaneHost: async () => ({ ok: false, reason: 'undecidable' }),
      },
      {
        agent_id: 'A',
        agent_type: 'claude-code',
        delivery: { kind: 'none' },
        tmux_pane_id: '%19',
        device: LOCAL,
        runtime_ui_pid: 4242,
      },
      { content: 'hint', meta: {} }
    )
    expect(tmuxPoke).not.toHaveBeenCalled()
    expect(result).toMatchObject({ error: 'tmux_unavailable', transport_used: 'tmux-poke' })
  })
})

describe('the legacy no-fanout path reports the same reasons as the dispatcher', () => {
  it('maps an unqueryable tmux to tmux_unavailable, not the raw verdict', async () => {
    const db = openDb(':memory:')
    applySchema(db)
    insertAgent(db, { agent_id: 'caller', name: 'caller', team: 't', device: LOCAL })
    insertAgent(db, {
      agent_id: 'target',
      name: 'target',
      team: 't',
      device: LOCAL,
      tmux_pane_id: '%19',
      runtime_ui_pid: process.pid,
    })

    // No ChannelWakeFanout: this is the legacy branch reviewer found unmapped.
    const result = await poke(
      {
        db,
        callerAgentId: 'caller',
        localDevice: LOCAL,
        paneSnapshot: async () => null,
      },
      { target_agent_id: 'target', prompt: 'hint' }
    )

    expect(result).toMatchObject({ error: 'tmux_unavailable', transport_used: 'tmux-poke' })
    expect(result).not.toMatchObject({ error: 'undecidable' })
    db.close()
  })
})

describe('ownership is re-read immediately before the first tmux write', () => {
  it('a takeover landing after verification still blocks the write', async () => {
    // The quiet-guard parks for POKE_QUIET_MS, so verification at dispatch entry
    // cannot be what the write relies on.
    let ownedByTarget = true
    const writes: string[] = []
    const tmuxPoke = vi.fn(async (args: {
      pane_id: string
      confirmOwnership?: () => boolean
    }) => {
      ownedByTarget = false // takeover lands during the guard window
      if (args.confirmOwnership && !args.confirmOwnership()) {
        return { error: 'pane_reassigned' as const }
      }
      writes.push(args.pane_id)
      return { ok: true as const, pane_tail_before: '', pane_tail_after: '' }
    })

    const result = await dispatchPoke(
      {
        tmuxPoke,
        verifyPaneHost: async () => ({ ok: true }),
        confirmPaneOwnership: () => ownedByTarget,
      },
      {
        agent_id: 'A',
        agent_type: 'claude-code',
        delivery: { kind: 'none' },
        tmux_pane_id: '%19',
        device: LOCAL,
        runtime_ui_pid: 4242,
      },
      { content: 'hint', meta: {} }
    )

    expect(writes).toEqual([])
    expect(result).toMatchObject({ error: 'pane_reassigned' })
  })
})

describe('a stale cached target row cannot outlive the pane it was read with', () => {
  const snapshot = paneSnapshotOf([{ pane_id: '%19', pane_pid: null }])

  it('rejects when the DB no longer records the cached row as the pane holder', async () => {
    const verdict = await verifyPaneHost({
      row: { agent_id: 'A', device: LOCAL, runtime_ui_pid: null },
      paneId: '%19',
      paneSnapshot: snapshot,
      localDevice: LOCAL,
      findPaneClaimants: () => [{ agent_id: 'B', device: LOCAL, runtime_ui_pid: null }],
      // last-writer-wins already moved %19 to B and cleared A.
      stillOwnsPane: () => false,
    })
    expect(verdict).toEqual({ ok: false, reason: 'pane_reassigned' })
  })

  it('rejects a live-pid row too once the DB says it no longer holds the pane', async () => {
    const verdict = await verifyPaneHost({
      row: { agent_id: 'A', device: LOCAL, runtime_ui_pid: 4242 },
      paneId: '%19',
      paneSnapshot: paneSnapshotOf([{ pane_id: '%19', pane_pid: 4242 }]),
      localDevice: LOCAL,
      isProcessAlive: () => true,
      stillOwnsPane: () => false,
    })
    expect(verdict).toEqual({ ok: false, reason: 'pane_reassigned' })
  })

  it('still verifies an uncontested pid-less row that the DB confirms', async () => {
    const verdict = await verifyPaneHost({
      row: { agent_id: 'A', device: LOCAL, runtime_ui_pid: null },
      paneId: '%19',
      paneSnapshot: snapshot,
      localDevice: LOCAL,
      findPaneClaimants: () => [],
      stillOwnsPane: () => true,
    })
    expect(verdict).toEqual({ ok: true })
  })
})

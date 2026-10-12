import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import { poke } from '../src/mcp/poke.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('poke channel transport integration', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('uses claude-channel transport when csid set and sink attached', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const fanout = new ChannelWakeFanout()
    const alice = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    const bob = repo.register({
      model: 'opus', role: 'backend', name: 'bob',
      tmux_pane_id: '%99',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-bob' },
    })
    const emitted: unknown[] = []
    fanout.attach('csid-bob', (p) => emitted.push(p), 'sess-P')
    const res = await poke(
      { db, callerAgentId: alice.agent_id, channelWakeFanout: fanout },
      { target_agent_id: bob.agent_id, prompt: 'check inbox' }
    )
    expect(res).toMatchObject({
      ok: true,
      transport_used: 'claude-channel',
      channel_session_id: 'csid-bob'
    })
    expect(emitted).toHaveLength(1)
    db.close()
  })

  it('returns no_transport_available when neither csid/sink nor tmux_pane set', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const fanout = new ChannelWakeFanout()
    const alice = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    const bob = repo.register({ model: 'opus', role: 'backend', name: 'bob' })
    const res = await poke(
      { db, callerAgentId: alice.agent_id, channelWakeFanout: fanout },
      { target_agent_id: bob.agent_id, prompt: 'p' }
    )
    expect(res).toMatchObject({
      error: 'no_transport_available',
      detail: { channel_subscribed: false, tmux_pane_set: false }
    })
    db.close()
  })

  it('existing identity checks (self_poke_denied, cross_team_denied) remain intact', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const repo = new AgentsRepo(db)
    const fanout = new ChannelWakeFanout()
    const alice = repo.register({ model: 'opus', role: 'backend', name: 'alice' })
    const resSelf = await poke(
      { db, callerAgentId: alice.agent_id, channelWakeFanout: fanout },
      { target_agent_id: alice.agent_id, prompt: 'p' }
    )
    expect(resSelf).toEqual({ error: 'self_poke_denied' })

    const carol = repo.register({ model: 'opus', role: 'backend', name: 'carol', team: 'other' })
    const resCross = await poke(
      { db, callerAgentId: alice.agent_id, channelWakeFanout: fanout },
      { target_agent_id: carol.agent_id, prompt: 'p' }
    )
    expect(resCross).toEqual({ error: 'cross_team_denied' })
    db.close()
  })
})

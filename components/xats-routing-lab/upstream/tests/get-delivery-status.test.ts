import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { SendMessageService, type AutoPokeFn } from '../src/mcp/send-message.js'
import { BroadcastService } from '../src/mcp/broadcast.js'
import { GetDeliveryStatusService } from '../src/mcp/delivery-status.js'
import { __resetCapturePaneTail, __setCapturePaneTail } from '../src/mcp/poke-guard.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-delivery-status-'))

function setup() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const agents = new AgentsRepo(db)
  const events = new EventsOutbox(db)
  const fakePoke: AutoPokeFn = async () => ({ ok: true })
  const send = new SendMessageService(db, agents, events, { poke: fakePoke })
  const broadcast = new BroadcastService(db, agents, { poke: fakePoke })
  const status = new GetDeliveryStatusService(db)
  return { db, send, broadcast, status, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

describe('get_delivery_status service', () => {
  const cleanups: Array<() => void> = []
  beforeEach(() => {
    process.env.POKE_QUIET_MS = '20'
    __setCapturePaneTail(async (paneId: string) => `idle-${paneId}`)
  })
  afterEach(() => {
    cleanups.forEach(c => c())
    cleanups.length = 0
    __resetCapturePaneTail()
    delete process.env.POKE_QUIET_MS
  })

  it('sender reads status for a direct message', async () => {
    const { db, send, status, cleanup } = setup()
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'caller', tmux_pane_id: '%1', name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'worker', tmux_pane_id: '%2', name: 'B' })

    const sent = await send.send({ from: 'A', to_agent_id: 'B', body: 'hi' })
    if ('error' in sent) throw new Error('expected success')

    const out = status.get({ caller: 'A', message_id: sent.message_id })
    if ('error' in out) throw new Error('expected status')
    expect(out.statuses).toHaveLength(1)
    expect(out.statuses[0]).toMatchObject({ agent_id: 'B', wake_status: 'delivered' })
  })

  it('non-sender cannot read status', async () => {
    const { db, send, status, cleanup } = setup()
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'caller', tmux_pane_id: '%1', name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'worker', tmux_pane_id: '%2', name: 'B' })
    insertAgent(db, { agent_id: 'C', model: 'm', role: 'observer', tmux_pane_id: '%3', name: 'C' })

    const sent = await send.send({ from: 'A', to_agent_id: 'B', body: 'hi' })
    if ('error' in sent) throw new Error('expected success')

    expect(status.get({ caller: 'C', message_id: sent.message_id })).toEqual({ error: 'unknown_message' })
  })

  it('broadcast sender reads per-recipient statuses', async () => {
    const { db, broadcast, status, cleanup } = setup()
    cleanups.push(cleanup)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'caller', tmux_pane_id: '%1', name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'worker', tmux_pane_id: '%2', name: 'B' })
    insertAgent(db, { agent_id: 'C', model: 'm', role: 'worker', tmux_pane_id: '%3', name: 'C' })

    const sent = await broadcast.broadcast({ from: 'A', body: 'hi' })
    if ('error' in sent) throw new Error('expected success')

    const out = status.get({ caller: 'A', message_id: sent.message_id })
    if ('error' in out) throw new Error('expected status')
    expect(out.statuses.map(s => s.agent_id)).toEqual(['B', 'C'])
    expect(out.statuses.map(s => s.wake_status)).toEqual(['delivered', 'delivered'])
  })
})

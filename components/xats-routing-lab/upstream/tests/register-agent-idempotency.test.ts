import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { RegisterAgentService } from '../src/mcp/register-agent.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { GetInboxService } from '../src/mcp/get-inbox.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-idemp-'))

interface Setup {
  db: ReturnType<typeof openDb>
  svc: RegisterAgentService
  agents: AgentsRepo
  events: EventsOutbox
}

describe('register_agent idempotency integration', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  function setup(deps: ConstructorParameters<typeof RegisterAgentService>[1] = {}): Setup {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const svc = new RegisterAgentService(db, deps)
    const agents = new AgentsRepo(db)
    const events = new EventsOutbox(db)
    return { db, svc, agents, events }
  }

  it('scenario 1: new identity produces a fresh agent_id', () => {
    const { svc, db } = setup()
    const r = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in r) throw new Error('unexpected error')
    expect(typeof r.agent_id).toBe('string')
    expect(r.agent_id.length).toBeGreaterThan(0)
    const count = db.prepare('SELECT COUNT(*) AS c FROM agents').get() as { c: number }
    expect(count.c).toBe(1)
  })

  it('scenario 2: same identity different connection_id reuses after release (cross-session reconnect)', () => {
    const { svc } = setup()
    const r1 = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in r1) throw new Error('r1 unexpected')
    svc.releaseConnection(r1.agent_id, 'c1')
    const r2 = svc.register({ connection_id: 'c2', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in r2) throw new Error('r2 unexpected')
    expect(r2.agent_id).toBe(r1.agent_id)
  })

  it('scenario 3: reuse updates tmux_pane_id when provided', () => {
    const { svc, db } = setup()
    const r1 = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend', tmux_pane_id: '%42' })
    if ('error' in r1) throw new Error('r1 unexpected')
    svc.releaseConnection(r1.agent_id, 'c1')
    const r2 = svc.register({ connection_id: 'c2', model: 'opus', name: 'alice', role: 'backend', tmux_pane_id: '%99' })
    if ('error' in r2) throw new Error('r2 unexpected')
    const row = db.prepare('SELECT tmux_pane_id FROM agents WHERE agent_id=?').get(r1.agent_id) as { tmux_pane_id: string }
    expect(row.tmux_pane_id).toBe('%99')
  })

  it('scenario 4: reuse preserves tmux_pane_id when omitted', () => {
    const { svc, db } = setup()
    const r1 = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend', tmux_pane_id: '%42' })
    if ('error' in r1) throw new Error('r1 unexpected')
    svc.releaseConnection(r1.agent_id, 'c1')
    const r2 = svc.register({ connection_id: 'c2', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in r2) throw new Error('r2 unexpected')
    const row = db.prepare('SELECT tmux_pane_id FROM agents WHERE agent_id=?').get(r1.agent_id) as { tmux_pane_id: string }
    expect(row.tmux_pane_id).toBe('%42')
  })

  it('scenario 5: role change from another live session is a takeover (no collision)', () => {
    const closes: string[] = []
    const { svc, db } = setup({ closeSessionByConnectionId: (cid) => { closes.push(cid); return true } })
    const r1 = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in r1) throw new Error('r1 unexpected')
    const r2 = svc.register({ connection_id: 'c2', model: 'opus', name: 'alice', role: 'frontend' })
    if ('error' in r2) throw new Error('r2 unexpected')
    expect(r2.agent_id).toBe(r1.agent_id)
    expect(closes).toEqual(['c1'])
    const count = db.prepare('SELECT COUNT(*) AS c FROM agents').get() as { c: number }
    expect(count.c).toBe(1)
  })

  it('scenario 6: team change produces new agent_id', () => {
    const { svc, db } = setup()
    const r1 = svc.register({ connection_id: 'c1', model: 'opus', name: 'alice', role: 'backend' })
    const r2 = svc.register({ connection_id: 'c2', model: 'opus', name: 'alice', role: 'backend', team: 'alpha' })
    if ('error' in r1 || 'error' in r2) throw new Error('unexpected error')
    expect(r2.agent_id).not.toBe(r1.agent_id)
    const count = db.prepare('SELECT COUNT(*) AS c FROM agents').get() as { c: number }
    expect(count.c).toBe(2)
  })

  it('scenario 7: mailbox content survives reuse after reconnect', () => {
    const { svc, db, agents, events } = setup()
    // Alice registers first
    const rAlice1 = svc.register({ connection_id: 'cA', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in rAlice1) throw new Error('alice reg failed')
    // Sender registers
    const rSender = svc.register({ connection_id: 'cS', model: 'opus', name: 'sender', role: 'backend' })
    if ('error' in rSender) throw new Error('sender reg failed')

    // Sender sends a message to Alice
    const sendSvc = new SendMessageService(db, agents, events)
    const sendRes = sendSvc.send({
      from: rSender.agent_id,
      to_agent_id: rAlice1.agent_id,
      body: 'persistent greeting',
      auto_poke: false
    })
    if ('error' in sendRes) throw new Error('send failed')

    // Alice "disconnects" and reconnects with new connection_id
    svc.releaseConnection(rAlice1.agent_id, 'cA')
    const rAlice2 = svc.register({ connection_id: 'cA2', model: 'opus', name: 'alice', role: 'backend' })
    if ('error' in rAlice2) throw new Error('alice re-reg failed')
    expect(rAlice2.agent_id).toBe(rAlice1.agent_id)

    // Alice reads inbox via reused agent_id
    const inboxSvc = new GetInboxService(db, agents)
    const inbox = inboxSvc.get({ caller: rAlice2.agent_id })
    if ('error' in inbox) throw new Error('inbox read failed')
    expect(inbox.messages.some(m => m.body === 'persistent greeting')).toBe(true)
  })
})

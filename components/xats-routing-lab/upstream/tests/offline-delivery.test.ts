import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { EventsOutbox } from '../src/storage/events-outbox.js'
import { SendMessageService } from '../src/mcp/send-message.js'
import { GetInboxService } from '../src/mcp/get-inbox.js'
import { insertAgent } from './helpers/insert-agent.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('offline delivery', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('recipient catches up after coming back online', async () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db')); applySchema(db)
    const agents = new AgentsRepo(db)
    insertAgent(db, { agent_id: 'A', model: 'm', role: 'backend' , name: 'A' })
    insertAgent(db, { agent_id: 'B', model: 'm', role: 'frontend' , name: 'B' })
    const old = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    db.prepare('UPDATE agents SET last_seen_at=? WHERE agent_id=?').run(old, 'B')

    const send = new SendMessageService(db, agents, new EventsOutbox(db))
    await send.send({ from: 'A', to_agent_id: 'B', body: 'offline-hello', auto_poke: false })

    agents.touch('B')

    const inbox = new GetInboxService(db, agents)
    const r = inbox.get({ caller: 'B', since_event_id: 0 })
    expect(r.messages.length).toBe(1)
    expect(r.messages[0].body).toBe('offline-hello')
  })
})

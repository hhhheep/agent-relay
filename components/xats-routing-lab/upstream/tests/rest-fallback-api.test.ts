import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type Database from 'better-sqlite3'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { buildServer, startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import type { DeliverySpec } from '../src/lib/delivery-spec.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-rest-'))

interface Harness {
  base: string
  app: Awaited<ReturnType<typeof startServer>>['app']
  fanout: SseFanout
  channelWakeFanout: ChannelWakeFanout
  seedDb: Database.Database
  repo: AgentsRepo
  close: () => Promise<void>
}

async function boot(dir: string, opts: { token?: string } = {}): Promise<Harness> {
  const dbPath = join(dir, 'data.db')
  const fanout = new SseFanout()
  const channelWakeFanout = new ChannelWakeFanout()
  const { app, port, host } = await startServer({
    dbPath,
    port: 0,
    host: '127.0.0.1',
    localDevice: 'local',
    token: opts.token,
    fanout,
    channelWakeFanout,
  })
  const seedDb = openDb(dbPath)
  return {
    base: `http://${host}:${port}`,
    app,
    fanout,
    channelWakeFanout,
    seedDb,
    repo: new AgentsRepo(seedDb),
    close: async () => {
      await app.close()
      seedDb.close()
    },
  }
}

function seedAgent(
  repo: AgentsRepo,
  args: { name: string; team?: string; agent_type?: 'custom' | 'claude-code'; delivery?: DeliverySpec }
): string {
  return repo.register({
    agent_type: args.agent_type ?? 'custom',
    name: args.name,
    team: args.team ?? 'default',
    device: 'local',
    delivery: args.delivery,
  }).agent_id
}

async function postJson(base: string, path: string, body: unknown, headers: Record<string, string> = {}): Promise<Response> {
  return fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
}

function countMessages(db: Database.Database): number {
  return (db.prepare('SELECT COUNT(*) AS c FROM messages').get() as { c: number }).c
}

describe('rest fallback api — behavior parity', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('5.1 loopback send by (team,name) inserts the message row and pokes the recipient', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const aliceId = seedAgent(h.repo, { name: 'alice' })
    const bobId = seedAgent(h.repo, {
      name: 'bob',
      agent_type: 'claude-code',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-bob' },
    })
    const woke: unknown[] = []
    h.channelWakeFanout.attach('csid-bob', (payload) => { woke.push(payload) }, 'sess-bob')

    const res = await postJson(h.base, '/api/send', {
      from: { team: 'default', name: 'alice' },
      to: { team: 'default', name: 'bob' },
      body: 'hi',
    })
    expect(res.status).toBe(200)
    const data = await res.json()
    expect(typeof data.message_id).toBe('string')
    expect(typeof data.event_id).toBe('number')
    expect(data.recipients).toEqual([bobId])
    expect(data.poked).toBe(true)

    // DB row inserted from alice -> bob with the given body.
    const row = h.seedDb.prepare(
      'SELECT from_agent_id, to_agent_id, body FROM messages WHERE id=?'
    ).get(data.message_id) as { from_agent_id: string; to_agent_id: string; body: string }
    expect(row).toEqual({ from_agent_id: aliceId, to_agent_id: bobId, body: 'hi' })

    // Recipient poked the same way the tool would — via the channel-wake sink.
    expect(woke).toHaveLength(1)
    expect((woke[0] as { method: string }).method).toBe('notifications/channel_wake')

    await h.close()
  })

  it('5.2 unknown_sender is rejected with no insert', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    seedAgent(h.repo, { name: 'bob' })

    const res = await postJson(h.base, '/api/send', {
      from: { team: 'default', name: 'ghost' },
      to: { team: 'default', name: 'bob' },
      body: 'hi',
    })
    expect(res.status).toBe(404)
    expect(await res.json()).toEqual({ error: 'unknown_sender' })
    expect(countMessages(h.seedDb)).toBe(0)
    await h.close()
  })

  it('5.2 unknown_recipient returns the tool error, auto_poke:false inserts without poking', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    seedAgent(h.repo, { name: 'alice' })
    seedAgent(h.repo, {
      name: 'bob',
      agent_type: 'claude-code',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-bob2' },
    })
    const woke: unknown[] = []
    h.channelWakeFanout.attach('csid-bob2', (payload) => { woke.push(payload) }, 'sess-bob2')

    // unknown recipient
    const miss = await postJson(h.base, '/api/send', {
      from: { team: 'default', name: 'alice' },
      to: { team: 'default', name: 'nobody' },
      body: 'hi',
    })
    expect(miss.status).toBe(404)
    expect(await miss.json()).toEqual({ error: 'unknown_recipient' })
    expect(countMessages(h.seedDb)).toBe(0)

    // auto_poke:false — inserted, NOT poked
    const noPoke = await postJson(h.base, '/api/send', {
      from: { team: 'default', name: 'alice' },
      to: { team: 'default', name: 'bob' },
      body: 'quiet',
      auto_poke: false,
    })
    expect(noPoke.status).toBe(200)
    const data = await noPoke.json()
    expect(data.poked).toBe(false)
    expect(countMessages(h.seedDb)).toBe(1)
    expect(woke).toHaveLength(0)
    await h.close()
  })

  it('5.3 inbox default read advances cursor; explicit since_event_id is read-only; unknown owner rejected', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    seedAgent(h.repo, { name: 'alice' })
    seedAgent(h.repo, { name: 'bob' })

    // bob -> alice x2 (via REST send; alice has no transport so poke is skipped)
    await postJson(h.base, '/api/send', { from: { team: 'default', name: 'bob' }, to: { team: 'default', name: 'alice' }, body: 'm1' })
    await postJson(h.base, '/api/send', { from: { team: 'default', name: 'bob' }, to: { team: 'default', name: 'alice' }, body: 'm2' })

    // Explicit since_event_id=0 is read-only inspection — repeatable, no advance.
    const ro1 = await (await fetch(`${h.base}/api/inbox?team=default&name=alice&since_event_id=0`)).json()
    expect(ro1.messages).toHaveLength(2)
    const ro2 = await (await fetch(`${h.base}/api/inbox?team=default&name=alice&since_event_id=0`)).json()
    expect(ro2.messages).toHaveLength(2)

    // Default read (no since) returns the unread tail AND advances the cursor.
    const def1 = await (await fetch(`${h.base}/api/inbox?team=default&name=alice`)).json()
    expect(def1.messages).toHaveLength(2)
    const def2 = await (await fetch(`${h.base}/api/inbox?team=default&name=alice`)).json()
    expect(def2.messages).toHaveLength(0)

    // Unknown owner rejected.
    const miss = await fetch(`${h.base}/api/inbox?team=default&name=ghost`)
    expect(miss.status).toBe(404)
    expect(await miss.json()).toEqual({ error: 'unknown_owner' })

    await h.close()
  })

  it('5.4 /api/agents is team-scoped with no cross-team leak', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    seedAgent(h.repo, { name: 'alice', team: 'default' })
    seedAgent(h.repo, { name: 'bob', team: 'default' })
    seedAgent(h.repo, { name: 'carol', team: 'other' })

    const res = await fetch(`${h.base}/api/agents?team=default`)
    expect(res.status).toBe(200)
    const data = await res.json()
    const names = (data.agents as Array<{ name: string }>).map(a => a.name).sort()
    expect(names).toEqual(['alice', 'bob'])
    await h.close()
  })
})

describe('rest fallback api — no session/delivery side-effect invariant', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('6.1/6.2 REST send as a live agent leaves its MCP session + delivery binding intact', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)

    // alice holds a LIVE MCP session; register_agent attaches her fanout sink.
    const url = new URL(`${h.base}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'rest-side-effect-test', version: '0.0.0' })
    await client.connect(transport)
    const reg = await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', team: 'default', model: 'm', role: 'default' },
    }) as { content: Array<{ text: string }> }
    const aliceId = JSON.parse(reg.content[0].text).agent_id as string

    // bob is the recipient (seeded, no live session).
    seedAgent(h.repo, { name: 'bob' })

    // Snapshot the live-session state BEFORE the REST call.
    const fanoutBefore = h.fanout.peek()
    expect(fanoutBefore).toEqual([{ agent_id: aliceId, team: 'default' }])
    const healthBefore = await (await fetch(`${h.base}/health`)).json()
    expect(healthBefore.mcp_sessions).toMatchObject({ total: 1, registered: 1 })

    // REST send AS alice.
    const res = await postJson(h.base, '/api/send', {
      from: { team: 'default', name: 'alice' },
      to: { team: 'default', name: 'bob' },
      body: 'lifeboat',
    })
    expect(res.status).toBe(200)

    // No new session, no takeover: fanout binding + session counts unchanged.
    expect(h.fanout.peek()).toEqual(fanoutBefore)
    const healthAfter = await (await fetch(`${h.base}/health`)).json()
    expect(healthAfter.mcp_sessions).toMatchObject({ total: 1, registered: 1 })

    // alice's live MCP session is still usable (was not force-closed / taken over).
    const echo = await client.callTool({
      name: 'echo',
      arguments: { msg: 'still-alive' },
    }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('still-alive')

    await client.close()
    await transport.close()
    await h.close()
  }, 15000)
})

describe('rest fallback api — loopback gate + token auth', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('7.1 remote origin gets 403 on every /api route with no data action', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const app = await buildServer({ dbPath, localDevice: 'local' })
    const seedDb = openDb(dbPath)
    const repo = new AgentsRepo(seedDb)
    repo.register({ agent_type: 'custom', name: 'alice', team: 'default', device: 'local' })
    repo.register({ agent_type: 'custom', name: 'bob', team: 'default', device: 'local' })

    const remote = { remoteAddress: '10.0.0.42' }
    const send = await app.inject({
      method: 'POST', url: '/api/send', ...remote,
      payload: { from: { team: 'default', name: 'alice' }, to: { team: 'default', name: 'bob' }, body: 'hi' },
    })
    expect(send.statusCode).toBe(403)
    expect(send.json()).toEqual({ error: 'remote_forbidden' })

    const inbox = await app.inject({ method: 'GET', url: '/api/inbox?team=default&name=alice', ...remote })
    expect(inbox.statusCode).toBe(403)

    const agents = await app.inject({ method: 'GET', url: '/api/agents?team=default', ...remote })
    expect(agents.statusCode).toBe(403)

    // The rejected send performed no data-layer action.
    expect(countMessages(seedDb)).toBe(0)

    await app.close()
    seedDb.close()
  })

  it('7.3 forged X-Forwarded-For does not change the socket-derived origin', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const app = await buildServer({ dbPath, localDevice: 'local' })
    const seedDb = openDb(dbPath)
    const repo = new AgentsRepo(seedDb)
    repo.register({ agent_type: 'custom', name: 'alice', team: 'default', device: 'local' })

    // Remote socket claiming a loopback X-Forwarded-For is still remote -> 403.
    const forgedLocal = await app.inject({
      method: 'GET', url: '/api/agents?team=default',
      remoteAddress: '10.0.0.42',
      headers: { 'x-forwarded-for': '127.0.0.1' },
    })
    expect(forgedLocal.statusCode).toBe(403)
    expect(forgedLocal.json()).toEqual({ error: 'remote_forbidden' })

    // Loopback socket claiming a remote X-Forwarded-For is still local -> served.
    const forgedRemote = await app.inject({
      method: 'GET', url: '/api/agents?team=default',
      remoteAddress: '127.0.0.1',
      headers: { 'x-forwarded-for': '10.0.0.42' },
    })
    expect(forgedRemote.statusCode).toBe(200)

    await app.close()
    seedDb.close()
  })

  it('7.4 ambiguous recipient object (agent_id + name) is rejected at the boundary', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const app = await buildServer({ dbPath, localDevice: 'local' })
    const seedDb = openDb(dbPath)
    const repo = new AgentsRepo(seedDb)
    repo.register({ agent_type: 'custom', name: 'alice', team: 'default', device: 'local' })

    const res = await app.inject({
      method: 'POST', url: '/api/send',
      payload: { from: { team: 'default', name: 'alice' }, to: { agent_id: 'x', name: 'y' }, body: 'hi' },
    })
    expect(res.statusCode).toBe(400)
    expect(res.json().error).toBe('invalid_request')
    expect(countMessages(seedDb)).toBe(0)

    await app.close()
    seedDb.close()
  })

  it('7.2 token gates 401; loopback served; remote-with-token still 403', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const app = await buildServer({ dbPath, localDevice: 'local', token: 's3cret' })

    // Missing token from loopback -> 401.
    const noToken = await app.inject({
      method: 'POST', url: '/api/send',
      payload: { from: { team: 'default', name: 'alice' }, to: { team: 'default', name: 'bob' }, body: 'hi' },
    })
    expect(noToken.statusCode).toBe(401)

    // Correct token + loopback -> served (200, not 401/403).
    const served = await app.inject({
      method: 'GET', url: '/api/agents?team=default',
      headers: { authorization: 'Bearer s3cret' },
    })
    expect(served.statusCode).toBe(200)

    // Correct token + remote -> still 403 (loopback gate is independent of auth).
    const remote = await app.inject({
      method: 'GET', url: '/api/agents?team=default',
      headers: { authorization: 'Bearer s3cret' },
      remoteAddress: '10.0.0.42',
    })
    expect(remote.statusCode).toBe(403)

    await app.close()
  })
})

import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import Fastify from 'fastify'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { mountMcp } from '../src/mcp/transport.js'
import type { OrphanSessionGcOptions } from '../src/mcp/transport.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-orphan-gc-'))

interface Harness {
  app: Awaited<ReturnType<typeof Fastify>>
  port: number
  host: string
  channelWakeFanout: ChannelWakeFanout
  fanout: SseFanout
  reapOrphanSessions: (now: number, opts?: number | OrphanSessionGcOptions) => void
  close: () => Promise<void>
}

async function bootHarness(
  dbPath: string,
  opts: { orphanSessionLimit?: number } = {}
): Promise<Harness> {
  const app = Fastify({ logger: false })
  const db = openDb(dbPath)
  applySchema(db)
  const fanout = new SseFanout()
  const channelWakeFanout = new ChannelWakeFanout()
  const mcp = mountMcp(app, db, fanout, channelWakeFanout, opts)
  await app.listen({ port: 0, host: '127.0.0.1' })
  const addr = app.server.address()
  const port = addr && typeof addr === 'object' ? addr.port : 0
  return {
    app,
    port,
    host: '127.0.0.1',
    fanout,
    channelWakeFanout,
    reapOrphanSessions: mcp.reapOrphanSessions,
    close: async () => {
      await app.close()
      fanout.stopAll()
      db.close()
    },
  }
}

async function connectAndInit(host: string, port: number): Promise<{ c: Client; t: StreamableHTTPClientTransport }> {
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'orphan-gc-test', version: '0.0.0' })
  await c.connect(t)
  return { c, t }
}

async function expectUnknownSession(host: string, port: number, sid: string): Promise<void> {
  const probe = await fetch(`http://${host}:${port}/mcp`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'accept': 'application/json, text/event-stream',
      'mcp-session-id': sid,
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} })
  })
  expect(probe.status).toBe(404)
  expect(await probe.text()).toBe('')
}

describe('mcp-transport orphan-session GC', () => {
  const cleanups: string[] = []
  afterEach(() => {
    vi.restoreAllMocks()
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('orphan session past grace is reaped', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)
    const sid = t.sessionId!
    expect(typeof sid).toBe('string')

    // Idle-based reap: the session has had no activity since the initialize
    // request, so a virtual `now` 60 001 ms after the last bump exceeds the
    // 60 s explicit grace and reaps it.
    h.reapOrphanSessions(Date.now() + 60_001, 60_000)

    // After reap, raw POST with the orphan's sid returns unknown_session.
    await new Promise(r => setTimeout(r, 100))
    await expectUnknownSession(h.host, h.port, sid)

    try { await t.close() } catch { /* already gone */ }
    await c.close().catch(() => { /* already closed */ })
    await h.close()
  }, 15000)

  it('orphan reap is silent by default', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)

    h.reapOrphanSessions(Date.now() + 60_001, 60_000)
    await new Promise(r => setTimeout(r, 100))

    expect(debug).not.toHaveBeenCalled()

    try { await t.close() } catch { /* already gone */ }
    await c.close().catch(() => { /* already closed */ })
    await h.close()
  }, 15000)

  it('registered session is exempt from GC', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)
    const sid = t.sessionId!

    await c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    })

    // Far past any threshold.
    h.reapOrphanSessions(Date.now() + 86_400_000)

    await new Promise(r => setTimeout(r, 50))
    // Session should still be alive.
    const echo = await c.callTool({ name: 'echo', arguments: { msg: 'still here' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('still here')
    void sid
    await c.close()
    await t.close()
    await h.close()
  }, 15000)

  it('orphan within grace is not reaped yet', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)

    // Only 30 seconds elapsed virtually — within the explicit 60 s grace.
    h.reapOrphanSessions(Date.now() + 30_000, 60_000)
    await new Promise(r => setTimeout(r, 50))

    // Session should still be alive — call a tool over it.
    const echo = await c.callTool({ name: 'echo', arguments: { msg: 'hb' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('hb')

    await c.close()
    await t.close()
    await h.close()
  }, 15000)

  it('activity bumps the idle clock and prevents reap', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)

    // Simulate a human-paced workflow: client initialized minutes ago, then
    // issues a tool call (any POST counts as activity), then takes another
    // minute before calling register_agent. With createdAt-based reaping the
    // first GC tick would have killed the session; idle-based reaping keeps
    // it alive because the echo call bumped lastActivityAt.
    await new Promise(r => setTimeout(r, 50))
    await c.callTool({ name: 'echo', arguments: { msg: 'still working' } })

    // GC at 5 s past the echo: well within a 60 s idle grace.
    h.reapOrphanSessions(Date.now() + 5_000, 60_000)
    await new Promise(r => setTimeout(r, 50))

    const echo = await c.callTool({ name: 'echo', arguments: { msg: 'survived' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('survived')

    await c.close()
    await t.close()
    await h.close()
  }, 15000)

  it('active orphan past max age is NOT reaped (recent activity exempts it)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)

    await c.callTool({ name: 'echo', arguments: { msg: 'hb' } })

    // Past max-age (40 s) but within the idle window (60 s) of the echo above:
    // recent client activity exempts the orphan from max-age reaping.
    h.reapOrphanSessions(Date.now() + 45_000, {
      idleMs: 60_000,
      maxAgeMs: 40_000,
      maxSessions: 100,
    })
    await new Promise(r => setTimeout(r, 100))

    const echo = await c.callTool({ name: 'echo', arguments: { msg: 'survived' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('survived')

    await c.close()
    await t.close()
    await h.close()
  }, 15000)

  it('idle orphan past max age is reaped', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)
    const sid = t.sessionId!

    // No activity since initialize; virtual `now` is past both idle and max-age.
    h.reapOrphanSessions(Date.now() + 45_000, {
      idleMs: 40_000,
      maxAgeMs: 40_000,
      maxSessions: 100,
    })
    await new Promise(r => setTimeout(r, 100))

    await expectUnknownSession(h.host, h.port, sid)

    try { await c.close() } catch { /* already gone */ }
    try { await t.close() } catch { /* already gone */ }
    await h.close()
  }, 15000)

  it('orphan cap reaps oldest unregistered sessions only', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const first = await connectAndInit(h.host, h.port)
    await new Promise(r => setTimeout(r, 5))
    const second = await connectAndInit(h.host, h.port)
    await new Promise(r => setTimeout(r, 5))
    const third = await connectAndInit(h.host, h.port)

    await third.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'registered', model: 'm', role: 'r' }
    })

    h.reapOrphanSessions(Date.now(), {
      idleMs: 60_000,
      maxAgeMs: 60_000,
      maxSessions: 1,
    })
    await new Promise(r => setTimeout(r, 100))

    await expectUnknownSession(h.host, h.port, first.t.sessionId!)
    const echo = await second.c.callTool({ name: 'echo', arguments: { msg: 'kept' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('kept')
    const registeredEcho = await third.c.callTool({ name: 'echo', arguments: { msg: 'registered kept' } }) as { content: Array<{ text: string }> }
    expect(registeredEcho.content[0].text).toContain('registered kept')

    try { await first.c.close() } catch { /* already gone */ }
    try { await first.t.close() } catch { /* already gone */ }
    await second.c.close()
    await second.t.close()
    await third.c.close()
    await third.t.close()
    await h.close()
  }, 15000)

  it('session initialization enforces the orphan cap immediately', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'), { orphanSessionLimit: 1 })
    const first = await connectAndInit(h.host, h.port)
    await new Promise(r => setTimeout(r, 5))
    const second = await connectAndInit(h.host, h.port)
    await new Promise(r => setTimeout(r, 100))

    await expectUnknownSession(h.host, h.port, first.t.sessionId!)
    const echo = await second.c.callTool({ name: 'echo', arguments: { msg: 'kept' } }) as { content: Array<{ text: string }> }
    expect(echo.content[0].text).toContain('kept')

    try { await first.c.close() } catch { /* already gone */ }
    try { await first.t.close() } catch { /* already gone */ }
    await second.c.close()
    await second.t.close()
    await h.close()
  }, 15000)

  it('reap propagates to fanout and channel bindings', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await bootHarness(join(dir, 'data.db'))
    const { c, t } = await connectAndInit(h.host, h.port)
    const sid = t.sessionId!

    // Inject a synthetic SSE sink and channel-wake binding for this session id
    // — simulating a half-finished registration that bound a sink before the
    // register_agent path failed.
    const csid = 'csid-orphan-test'
    h.channelWakeFanout.attach(csid, () => { /* sink */ }, sid)
    expect(h.channelWakeFanout.has(csid)).toBe(true)

    h.reapOrphanSessions(Date.now() + 60_001, 60_000)
    await new Promise(r => setTimeout(r, 100))

    expect(h.channelWakeFanout.has(csid)).toBe(false)

    try { await c.close() } catch { /* already gone */ }
    try { await t.close() } catch { /* already gone */ }
    await h.close()
  }, 15000)
})

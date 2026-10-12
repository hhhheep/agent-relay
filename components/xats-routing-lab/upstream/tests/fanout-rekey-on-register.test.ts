import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-fanout-rekey-'))

interface Booted {
  app: Awaited<ReturnType<typeof startServer>>['app']
  port: number
  host: string
  fanout: SseFanout
  cleanup: () => void
}

async function boot(): Promise<Booted> {
  const dir = tmp()
  const fanout = new SseFanout({ heartbeatIntervalMs: 60_000 })
  const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0, fanout })
  return { app, port, host, fanout, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

async function connectClient(host: string, port: number) {
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'test', version: '0.0.0' })
  await c.connect(t)
  return { c, t }
}

function parseTool(resp: unknown): Record<string, unknown> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

describe('fanout re-key on register_agent', () => {
  const teardown: Array<() => Promise<void> | void> = []
  afterEach(async () => {
    for (const t of teardown.reverse()) { try { await t() } catch { /* ignore */ } }
    teardown.length = 0
  })

  it('does not attach fanout at session init (before register_agent)', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connectClient(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    // Session is initialized (client.connect sends initialize + notifications/initialized),
    // but no register_agent was called yet.
    expect(b.fanout.peek()).toEqual([])
  })

  it('attaches fanout under returned agent_id (NOT session id) after register', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connectClient(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const reg = parseTool(await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' } }))
    const sessionId = t.sessionId!
    const peek = b.fanout.peek()
    expect(peek).toEqual([{ agent_id: reg.agent_id, team: 'default' }])
    expect(peek.map(p => p.agent_id)).not.toContain(sessionId)
  })

  it('cross-session reuse replaces prior sink with new session', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())

    const first = await connectClient(b.host, b.port)
    const regA = parseTool(await first.c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' } }))
    const peekA = b.fanout.peek()
    expect(peekA).toEqual([{ agent_id: regA.agent_id, team: 'default' }])

    // Close the first session fully to detach its sink
    await first.t.close()
    await first.c.close()

    const second = await connectClient(b.host, b.port)
    teardown.push(async () => { await second.t.close(); await second.c.close() })
    const regB = parseTool(await second.c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' } }))
    expect(regB.agent_id).toBe(regA.agent_id)
    const peekB = b.fanout.peek()
    expect(peekB).toEqual([{ agent_id: regA.agent_id, team: 'default' }])
  })

  it('session close detaches the agent_id sink', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connectClient(b.host, b.port)
    const reg = parseTool(await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' } }))
    expect(b.fanout.peek().map(p => p.agent_id)).toContain(reg.agent_id)
    // terminateSession issues a DELETE so the server transport fires onclose
    await t.terminateSession()
    await t.close()
    await c.close()
    await new Promise(r => setTimeout(r, 100))
    expect(b.fanout.peek()).toEqual([])
  })

  it('close before register is a no-op for fanout', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connectClient(b.host, b.port)
    // close immediately without register
    await t.close()
    await c.close()
    await new Promise(r => setTimeout(r, 50))
    expect(b.fanout.peek()).toEqual([])
  })
})

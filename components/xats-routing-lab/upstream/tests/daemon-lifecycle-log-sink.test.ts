import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { buildServer, startServer } from '../src/daemon/server.js'

// The daemon binary path (cli.ts `daemon` -> startServer) supplies no mcpLog.
// The default sink MUST land the transport lifecycle lines on the same stream
// as the startup banner (console.log / stdout), which the launchers append to
// the daemon log file. These tests exercise that default (no mcpLog passed).
// Embedded buildServer callers (library use, unit tests) get NO default sink
// and stay silent.

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-log-sink-'))

interface Connected {
  c: Client
  t: StreamableHTTPClientTransport
}

async function connectClient(host: string, port: number): Promise<Connected> {
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'log-sink-test', version: '0.0.0' })
  await c.connect(t)
  return { c, t }
}

describe('daemon default lifecycle log sink', () => {
  const cleanups: string[] = []
  afterEach(() => {
    vi.restoreAllMocks()
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  function capturedLines(spy: ReturnType<typeof vi.spyOn>): string[] {
    return spy.mock.calls.map(args => args.map(String).join(' '))
  }

  it('emits the takeover line on console.log when no mcpLog is supplied', async () => {
    const dir = tmp(); cleanups.push(dir)
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
    })

    const a = await connectClient(host, port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' },
    })
    const b = await connectClient(host, port)
    await b.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' },
    })

    const lines = capturedLines(logSpy)
    expect(
      lines.some(line => line.includes('register_agent takeover')),
      `expected takeover line on console.log; lines=${JSON.stringify(lines)}`
    ).toBe(true)

    try { await a.t.close() } catch { /* already closed */ }
    await b.c.close()
    await app.close()
  }, 15000)

  it('emits the session-closed line on console.log when a session closes', async () => {
    const dir = tmp(); cleanups.push(dir)
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
    })

    const a = await connectClient(host, port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' },
    })
    const sid = a.t.sessionId
    await a.t.terminateSession()
    await a.c.close()
    await new Promise(resolve => setTimeout(resolve, 200))

    const lines = capturedLines(logSpy)
    expect(
      lines.some(line =>
        line.includes('mcp session closed') && line.includes(`sid=${sid}`)
      ),
      `expected session-closed line for sid=${sid}; lines=${JSON.stringify(lines)}`
    ).toBe(true)

    await app.close()
  }, 15000)

  it('emits the orphan reap line on console.log when GC reaps an idle session', async () => {
    const dir = tmp(); cleanups.push(dir)
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      orphanGcIntervalMs: 50,
      orphanGcIdleMs: 20,
    })

    // Connect without registering: an orphan session idles past the GC window.
    const a = await connectClient(host, port)
    await new Promise(resolve => setTimeout(resolve, 400))

    const lines = capturedLines(logSpy)
    expect(
      lines.some(line => line.includes('mcp orphan session reap')),
      `expected orphan reap line; lines=${JSON.stringify(lines)}`
    ).toBe(true)

    try { await a.c.close() } catch { /* reaped by GC */ }
    await app.close()
  }, 15000)

  it('embedded buildServer without mcpLog stays silent on console.log', async () => {
    const dir = tmp(); cleanups.push(dir)
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const app = await buildServer({
      dbPath: join(dir, 'data.db'),
    })
    await app.listen({ port: 0, host: '127.0.0.1' })
    const addr = app.server.address()
    const port = addr && typeof addr === 'object' ? addr.port : 0

    const a = await connectClient('127.0.0.1', port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' },
    })

    expect(
      capturedLines(logSpy).some(line => line.includes('mcp session created'))
    ).toBe(false)

    await a.c.close()
    await app.close()
  }, 15000)

  it('an explicit mcpLog still overrides the default sink', async () => {
    const dir = tmp(); cleanups.push(dir)
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const lines: string[] = []
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      mcpLog: line => lines.push(line),
    })

    const a = await connectClient(host, port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' },
    })

    expect(lines.some(line => line.includes('mcp session created'))).toBe(true)
    expect(
      capturedLines(logSpy).some(line => line.includes('mcp session created'))
    ).toBe(false)

    await a.c.close()
    await app.close()
  }, 15000)
})

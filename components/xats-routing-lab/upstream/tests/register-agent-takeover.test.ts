import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { z } from 'zod'
import { startServer } from '../src/daemon/server.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-takeover-'))

interface Connected {
  c: Client
  t: StreamableHTTPClientTransport
}

const HeartbeatNotification = z.object({
  jsonrpc: z.literal('2.0'),
  method: z.literal('notifications/heartbeat'),
  params: z.any().optional(),
})

async function connectClient(
  host: string,
  port: number,
  onHeartbeat?: () => void
): Promise<Connected> {
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'takeover-test', version: '0.0.0' })
  if (onHeartbeat) {
    c.setNotificationHandler(
      HeartbeatNotification as any,
      async () => { onHeartbeat() }
    )
  }
  await c.connect(t)
  return { c, t }
}

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

const CODEX_THREAD_A = '11111111-1111-4111-8111-111111111111'

function codexDelivery(thread_id: string) {
  return {
    kind: 'codex-appserver',
    thread_id,
    ws_url: 'ws://127.0.0.1:8799',
  }
}

async function probeSession(
  host: string,
  port: number,
  sessionId: string
): Promise<Response> {
  return fetch(`http://${host}:${port}/mcp`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'accept': 'application/json, text/event-stream',
      'mcp-session-id': sessionId,
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/list',
      params: {},
    }),
  })
}

describe('register_agent cross-session takeover', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('second session re-claims (device, team, name): 200 + same agent_id, prior session removed from daemon sessions Map', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })

    // Session A registers
    const a = await connectClient(host, port)
    const r1 = await parseTool(await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    }))
    expect(r1.agent_id).toBeDefined()
    const sidA = a.t.sessionId
    expect(typeof sidA).toBe('string')

    // Session B re-claims same (device, team, name)
    const b = await connectClient(host, port)
    const r2 = await parseTool(await b.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    }))
    expect(r2.error).toBeUndefined()
    expect(r2.agent_id).toBe(r1.agent_id)

    // Allow the daemon onclose chain to settle.
    await new Promise(r => setTimeout(r, 200))

    // The old session id MUST no longer be present on the daemon: a raw POST
    // with the old session id returns unknown_session.
    const probe = await probeSession(host, port, sidA!)
    expect(probe.status).toBe(404)
    expect(await probe.text()).toBe('')

    try { await a.t.close() } catch { /* already closed */ }
    await b.c.close()
    await app.close()
  }, 15000)

  it('clean reconnect after explicit close does NOT log takeover (registerSvc binding released on session close)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const lines: string[] = []
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      mcpLog: line => lines.push(line)
    })

    // Session A registers, then closes cleanly via DELETE.
    const a = await connectClient(host, port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    })
    await a.t.terminateSession()
    await a.c.close()

    // Allow onclose chain (and our new releaseConnection call) to settle.
    await new Promise(r => setTimeout(r, 200))

    // Reset captured lines so we only assert on the second register attempt.
    lines.length = 0

    // Session B re-registers same (device, team, name) — should be a clean reuse,
    // not a takeover, because A's connection_id was released on close.
    const b = await connectClient(host, port)
    await b.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    })

    const takeoverLine = lines.find(l => l.includes('register_agent takeover'))
    expect(
      takeoverLine,
      `did NOT expect takeover log on clean reconnect; lines=${JSON.stringify(lines)}`
    ).toBeUndefined()

    await b.c.close()
    await app.close()
  }, 15000)

  it('emits a debug-level takeover log identifying old/new sids and (device, team, name)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const lines: string[] = []
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      mcpLog: line => lines.push(line)
    })

    const a = await connectClient(host, port)
    await a.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    })
    const sidA = a.t.sessionId!

    const b = await connectClient(host, port)
    await b.c.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'alice', model: 'm', role: 'r' }
    })
    const sidB = b.t.sessionId!

    const takeoverLine = lines.find(l => l.includes('register_agent takeover'))
    expect(takeoverLine, `expected takeover log; lines=${JSON.stringify(lines)}`).toBeDefined()
    expect(takeoverLine!).toContain(`old=${sidA}`)
    expect(takeoverLine!).toContain(`new=${sidB}`)
    expect(takeoverLine!).toMatch(/device=[^\s]+/)
    expect(takeoverLine!).toContain('team=default')
    expect(takeoverLine!).toContain('name=alice')

    try { await a.t.close() } catch { /* already closed */ }
    await b.c.close()
    await app.close()
  }, 15000)

  it('keeps both MCP sessions usable for the same Codex thread', async () => {
    const dir = tmp(); cleanups.push(dir)
    let lines: string[] = []
    const fanout = new SseFanout({ heartbeatIntervalMs: 50 })
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      mcpLog: line => { lines = [...lines, line] },
      fanout,
    })
    let firstHeartbeats = 0
    const first = await connectClient(
      host,
      port,
      () => { firstHeartbeats += 1 }
    )
    const second = await connectClient(host, port)

    try {
      const firstRegistration = await parseTool(await first.c.callTool({
        name: 'register_agent',
        arguments: {
          agent_type: 'codex',
          name: 'alice',
          delivery: codexDelivery(CODEX_THREAD_A),
        },
      }))
      const secondRegistration = await parseTool(await second.c.callTool({
        name: 'register_agent',
        arguments: {
          agent_type: 'codex',
          name: 'alice',
          delivery: codexDelivery(CODEX_THREAD_A),
        },
      }))

      expect(secondRegistration.agent_id).toBe(firstRegistration.agent_id)
      expect(lines.some(line => line.includes('register_agent takeover')))
        .toBe(false)

      const firstInbox = await parseTool(await first.c.callTool({
        name: 'get_inbox',
        arguments: {},
      }))
      expect(firstInbox.error).toBeUndefined()

      await second.t.terminateSession()
      await second.c.close()
      await new Promise(resolve => setTimeout(resolve, 100))

      const firstInboxAfterClose = await parseTool(await first.c.callTool({
        name: 'get_inbox',
        arguments: {},
      }))
      expect(firstInboxAfterClose.error).toBeUndefined()
      const heartbeatsBeforeWait = firstHeartbeats
      await new Promise(resolve => setTimeout(resolve, 150))
      expect(firstHeartbeats).toBeGreaterThan(heartbeatsBeforeWait)
    } finally {
      const results = await Promise.allSettled([
        first.c.close(),
        second.c.close(),
      ])
      await app.close()
      const failures = results.flatMap(result =>
        result.status === 'rejected' ? [result.reason] : []
      )
      if (failures.length > 0) {
        throw new AggregateError(failures, 'Failed to close test clients.')
      }
    }
  }, 15000)

})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { z } from 'zod'
import { startServer } from '../src/daemon/server.js'

const HeartbeatNotification = z.object({
  jsonrpc: z.literal('2.0'),
  method: z.literal('notifications/heartbeat'),
  params: z.any().optional()
})

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-hbe2e-'))

describe('mcp heartbeat end-to-end', () => {
  const cleanups: string[] = []
  const savedEnv = { ...process.env }
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0
    for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k]
    for (const [k, v] of Object.entries(savedEnv)) process.env[k] = v
  })

  it('client receives at least one heartbeat notification within 400ms at interval 100ms', async () => {
    process.env.HEARTBEAT_INTERVAL_MS = '100'
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'test', version: '0.0.0' })

    let received = 0
    client.setNotificationHandler(HeartbeatNotification as any, async () => { received += 1 })
    await client.connect(transport)
    await client.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', name: 'tester-5', model: 'opus-4-7', role: 'test' } })

    await new Promise(r => setTimeout(r, 400))
    expect(received).toBeGreaterThanOrEqual(1)

    try { await transport.terminateSession() } catch { /* ignore */ }
    await client.close()
    await app.close()
  }, 20000)
})

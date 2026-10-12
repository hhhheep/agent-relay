import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { buildServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('health endpoint', () => {
  const cleanups: string[] = []
  afterEach(async () => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('returns ok, version, uptime_seconds without auth', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db'), token: 's3cret' })
    const res = await app.inject({ method: 'GET', url: '/health' })
    expect(res.statusCode).toBe(200)
    const body = res.json() as { ok: boolean; version: string; uptime_seconds: number }
    expect(body.ok).toBe(true)
    expect(typeof body.version).toBe('string')
    expect(typeof body.uptime_seconds).toBe('number')
    expect(body.uptime_seconds).toBeGreaterThanOrEqual(0)
    expect(body).toHaveProperty('mcp_sessions')
    await app.close()
  })

  it('reports mcp session metrics', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db') })
    await app.listen({ port: 0, host: '127.0.0.1' })
    const addr = app.server.address()
    const port = addr && typeof addr === 'object' ? addr.port : 0
    const url = new URL(`http://127.0.0.1:${port}/mcp`)
    const orphanTransport = new StreamableHTTPClientTransport(url)
    const orphanClient = new Client({ name: 'health-orphan', version: '0.0.0' })
    const registeredTransport = new StreamableHTTPClientTransport(url)
    const registeredClient = new Client({ name: 'health-registered', version: '0.0.0' })

    await orphanClient.connect(orphanTransport)
    await registeredClient.connect(registeredTransport)
    await registeredClient.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', name: 'health-agent', role: 'r' }
    })

    const res = await app.inject({ method: 'GET', url: '/health' })
    expect(res.statusCode).toBe(200)
    const body = res.json() as {
      mcp_sessions: {
        total: number
        registered: number
        orphan: number
        fanout: number
      }
    }
    expect(body.mcp_sessions).toEqual({
      total: 2,
      registered: 1,
      orphan: 1,
      fanout: 1,
    })

    await orphanClient.close()
    await orphanTransport.close()
    await registeredClient.close()
    await registeredTransport.close()
    await app.close()
  })
})

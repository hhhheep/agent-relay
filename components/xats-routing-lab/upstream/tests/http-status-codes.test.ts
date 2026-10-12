import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

async function postMcp(url: string, sid: string | undefined, body: unknown, authHeader?: string): Promise<Response> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    'accept': 'application/json, text/event-stream'
  }
  if (sid) headers['mcp-session-id'] = sid
  if (authHeader) headers['authorization'] = authHeader
  return fetch(url, { method: 'POST', headers, body: JSON.stringify(body) })
}

describe('HTTP status codes for identity errors', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('second call presenting a different Authorization on a bound session id returns HTTP 409', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = `http://${host}:${port}/mcp`

    const t = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: { headers: { authorization: 'Bearer tokenX' } }
    } as any)
    const c = new Client({ name: 'a', version: '0.0.0' }, { capabilities: {} })
    await c.connect(t)
    const sid = t.sessionId!
    try {
      await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', name: 'tester-1', model: 'm', role: 'r' } })
      const res = await postMcp(url, sid, {
        jsonrpc: '2.0',
        id: 999,
        method: 'tools/call',
        params: { name: 'register_agent', arguments: { agent_type: 'custom', name: 'tester-2', model: 'm', role: 'r' } }
      }, 'Bearer tokenY')
      expect(res.status).toBe(409)
      // Body must not be a bare {error} object that would poison a strict client.
      expect(await res.text()).toBe('')
    } finally {
      await c.close()
      await app.close()
    }
  }, 15000)

  it('tools/call with a spoofed from_agent_id returns HTTP 403', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = `http://${host}:${port}/mcp`

    const t = new StreamableHTTPClientTransport(new URL(url))
    const c = new Client({ name: 'a', version: '0.0.0' }, { capabilities: {} })
    await c.connect(t)
    const sid = t.sessionId!
    try {
      await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', name: 'tester-3', model: 'm', role: 'r' } })
      const res = await postMcp(url, sid, {
        jsonrpc: '2.0',
        id: 1000,
        method: 'tools/call',
        params: { name: 'send_message', arguments: { body: 'hello', from_agent_id: 'not-my-session' } }
      })
      expect(res.status).toBe(403)
      // Body must not be a bare {error} object that would poison a strict client.
      expect(await res.text()).toBe('')
    } finally {
      await c.close()
      await app.close()
    }
  }, 15000)
})

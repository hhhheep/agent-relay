import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as http from 'node:http'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

interface RawResponse { status: number; headers: Record<string, string>; body: string }

function rawPost(opts: { host: string; port: number; path: string; headers: Record<string, string>; body: string; agent: http.Agent }): Promise<RawResponse> {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: opts.host, port: opts.port, path: opts.path, method: 'POST',
      headers: {
        'content-type': 'application/json',
        'accept': 'application/json, text/event-stream',
        ...opts.headers,
        'content-length': Buffer.byteLength(opts.body).toString()
      },
      agent: opts.agent
    }, (res) => {
      const chunks: Buffer[] = []
      res.on('data', (c: Buffer) => chunks.push(c))
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        const normalized: Record<string, string> = {}
        for (const [k, v] of Object.entries(res.headers)) {
          if (typeof v === 'string') normalized[k.toLowerCase()] = v
          else if (Array.isArray(v)) normalized[k.toLowerCase()] = v.join(',')
        }
        resolve({ status: res.statusCode ?? 0, headers: normalized, body: text })
      })
      res.on('error', reject)
    })
    req.on('error', reject)
    req.write(opts.body)
    req.end()
  })
}

function parseJsonOrSSE(res: RawResponse): any {
  const ct = res.headers['content-type'] ?? ''
  if (ct.includes('text/event-stream')) {
    const lines = res.body.split(/\n/).filter(l => l.startsWith('data:'))
    const last = lines[lines.length - 1]
    return last ? JSON.parse(last.slice(5).trim()) : null
  }
  return JSON.parse(res.body)
}

async function initSession(host: string, port: number, agent: http.Agent, authHeader?: string): Promise<string> {
  const headers: Record<string, string> = {}
  if (authHeader) headers['authorization'] = authHeader
  const res = await rawPost({
    host, port, path: '/mcp', headers, agent,
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } }
    })
  })
  const sid = res.headers['mcp-session-id']
  if (!sid) throw new Error('initialize did not return Mcp-Session-Id')
  await rawPost({
    host, port, path: '/mcp', agent,
    headers: { ...headers, 'mcp-session-id': sid },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })
  })
  return sid
}

async function callRegister(host: string, port: number, agent: http.Agent, sid: string, args: Record<string, unknown>, authHeader?: string): Promise<RawResponse> {
  const headers: Record<string, string> = { 'mcp-session-id': sid }
  if (authHeader) headers['authorization'] = authHeader
  return rawPost({
    host, port, path: '/mcp', agent, headers,
    body: JSON.stringify({
      jsonrpc: '2.0', id: Math.floor(Math.random() * 1e9), method: 'tools/call',
      params: { name: 'register_agent', arguments: { agent_type: 'custom', ...args } }
    })
  })
}

describe('agent_id collision (credential-based)', () => {
  const cleanups: string[] = []
  const agents: http.Agent[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0
    agents.forEach(a => a.destroy()); agents.length = 0
  })

  function freshAgent(): http.Agent {
    const a = new http.Agent({ keepAlive: false })
    agents.push(a)
    return a
  }

  async function bootServer() {
    const dir = tmp(); cleanups.push(dir)
    return startServer({ dbPath: join(dir, 'data.db'), port: 0 })
  }

  it('different Authorization header presenting same session id returns collision', async () => {
    const { app, port } = await bootServer()
    const a1 = freshAgent(); const a2 = freshAgent()
    const sid = await initSession('127.0.0.1', port, a1, 'Bearer ownerToken')

    const res1 = await callRegister('127.0.0.1', port, a1, sid, { model: 'm', role: 'r', name: 'alice' }, 'Bearer ownerToken')
    expect(res1.status).toBe(200)

    const res2 = await callRegister('127.0.0.1', port, a2, sid, { model: 'm', role: 'r', name: 'alice' }, 'Bearer imposterToken')
    expect(res2.status).toBe(409)
    // Body must not be a bare {error} object that would poison a strict client.
    expect(res2.body).toBe('')

    await app.close()
  })

  it('same Authorization re-registering across two TCP sockets is ok', async () => {
    const { app, port } = await bootServer()
    const a1 = freshAgent(); const a2 = freshAgent()
    const sid = await initSession('127.0.0.1', port, a1, 'Bearer ownerToken')

    const res1 = await callRegister('127.0.0.1', port, a1, sid, { model: 'm', role: 'r', name: 'alice' }, 'Bearer ownerToken')
    expect(res1.status).toBe(200)

    // OLD SEMANTICS (pre-fix) expected 409 here because of different TCP sockets.
    // NEW SEMANTICS: same Authorization -> no collision. Guard against regression by asserting 200.
    const res2 = await callRegister('127.0.0.1', port, a2, sid, { model: 'm', role: 'r', name: 'alice' }, 'Bearer ownerToken')
    expect(res2.status).toBe(200)
    const body = parseJsonOrSSE(res2)
    const reg = JSON.parse(body.result.content[0].text)
    expect(reg.error).toBeUndefined()
    expect(reg.agent_id).toBeDefined()

    await app.close()
  })
})

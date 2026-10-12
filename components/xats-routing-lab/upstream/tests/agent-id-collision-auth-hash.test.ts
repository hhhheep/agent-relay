import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as http from 'node:http'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-coll-'))

interface RawResponse {
  status: number
  headers: Record<string, string>
  body: string
}

function rawPost(opts: { host: string; port: number; path: string; headers: Record<string, string>; body: string; agent: http.Agent }): Promise<RawResponse> {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: opts.host, port: opts.port, path: opts.path, method: 'POST',
      headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream', ...opts.headers, 'content-length': Buffer.byteLength(opts.body).toString() },
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
  if (!sid) throw new Error('initialize did not return Mcp-Session-Id, status=' + res.status + ' body=' + res.body)
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

describe('agent_id_collision auth-hash semantics', () => {
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

  async function bootServer(opts: { token?: string }) {
    const dir = tmp(); cleanups.push(dir)
    return startServer({ dbPath: join(dir, 'data.db'), port: 0, token: opts.token })
  }

  it('same Authorization across two TCP sockets with same sessionId does not 409 (regression)', async () => {
    const { app, port } = await bootServer({ token: 'tokenX' })
    const agent1 = freshAgent()
    const agent2 = freshAgent()
    const sid = await initSession('127.0.0.1', port, agent1, 'Bearer tokenX')

    const res1 = await callRegister('127.0.0.1', port, agent1, sid, { model: 'opus-4-7', role: 'frontend', name: 'alice' }, 'Bearer tokenX')
    expect(res1.status).toBe(200)
    const body1 = parseJsonOrSSE(res1)
    const reg1 = JSON.parse(body1.result.content[0].text)
    expect(reg1.agent_id).toBeDefined()

    // Second call: same token, same session id, FRESH TCP socket (separate agent, keepAlive=false)
    const res2 = await callRegister('127.0.0.1', port, agent2, sid, { model: 'opus-4-7', role: 'frontend', name: 'alice' }, 'Bearer tokenX')
    expect(res2.status).toBe(200)
    const body2 = parseJsonOrSSE(res2)
    const reg2 = JSON.parse(body2.result.content[0].text)
    expect(reg2.error).toBeUndefined()
    expect(reg2.agent_id).toBeDefined()

    await app.close()
  })

  it('different Authorization on same sessionId returns 409 in token mode', async () => {
    // Server started without --token so the auth hook does not 401 the tokenY request.
    // The collision-detection logic under test compares the Authorization hash, independent of the auth hook.
    const { app, port } = await bootServer({})
    const agent1 = freshAgent()
    const agent2 = freshAgent()
    const sid = await initSession('127.0.0.1', port, agent1, 'Bearer tokenX')

    const res1 = await callRegister('127.0.0.1', port, agent1, sid, { model: 'opus-4-7', role: 'frontend', name: 'alice' }, 'Bearer tokenX')
    expect(res1.status).toBe(200)

    const res2 = await callRegister('127.0.0.1', port, agent2, sid, { model: 'opus-4-7', role: 'reviewer', name: 'alice' }, 'Bearer tokenY')
    expect(res2.status).toBe(409)
    // Body must not be a bare {error} object that would poison a strict client.
    expect(res2.body).toBe('')

    await app.close()
  })

  it('no Authorization header never triggers collision across sockets', async () => {
    const { app, port } = await bootServer({})
    const agent1 = freshAgent()
    const agent2 = freshAgent()
    const sid = await initSession('127.0.0.1', port, agent1)

    const res1 = await callRegister('127.0.0.1', port, agent1, sid, { model: 'opus-4-7', role: 'frontend', name: 'alice' })
    expect(res1.status).toBe(200)
    const body1 = parseJsonOrSSE(res1)
    const reg1 = JSON.parse(body1.result.content[0].text)
    expect(reg1.agent_id).toBeDefined()

    const res2 = await callRegister('127.0.0.1', port, agent2, sid, { model: 'opus-4-7', role: 'frontend', name: 'alice' })
    expect(res2.status).toBe(200)
    const body2 = parseJsonOrSSE(res2)
    const reg2 = JSON.parse(body2.result.content[0].text)
    expect(reg2.error).toBeUndefined()
    expect(reg2.agent_id).toBeDefined()

    await app.close()
  })
})

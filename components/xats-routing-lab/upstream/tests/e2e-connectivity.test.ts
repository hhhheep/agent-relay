import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

async function readJsonOrSSE(res: Response): Promise<any> {
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('text/event-stream')) {
    const text = await res.text()
    const lines = text.split(/\n/).filter(l => l.startsWith('data:'))
    const last = lines[lines.length - 1]
    return last ? JSON.parse(last.slice(5).trim()) : null
  }
  return res.json()
}

async function initSession(url: string): Promise<string> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'probe', version: '0' } }
    })
  })
  const sid = res.headers.get('Mcp-Session-Id')!
  expect(sid).toBeTruthy()
  // drain response body to release the connection
  await res.text()
  await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream', 'Mcp-Session-Id': sid },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })
  })
  return sid
}

async function echo(url: string, sid: string, msg: string): Promise<string> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream', 'Mcp-Session-Id': sid },
    body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'echo', arguments: { msg } } })
  })
  const body = await readJsonOrSSE(res)
  return JSON.parse(body.result.content[0].text).msg
}

describe('phase 0 connectivity', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('three agents can each echo concurrently with distinct session ids', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = `http://${host}:${port}/mcp`
    const roles = ['opencode', 'claude-code', 'codex-cli']
    const sids = await Promise.all(roles.map(() => initSession(url)))
    expect(new Set(sids).size).toBe(3)
    const out = await Promise.all(roles.map((r, i) => echo(url, sids[i], r)))
    expect(out).toEqual(roles)
    await app.close()
  })
})

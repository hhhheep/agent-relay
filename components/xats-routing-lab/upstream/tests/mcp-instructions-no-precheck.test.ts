import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-instructions-'))

async function rpc(url: string, body: unknown, sessionId?: string): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'accept': 'application/json, text/event-stream',
      ...(sessionId ? { 'Mcp-Session-Id': sessionId } : {})
    },
    body: JSON.stringify(body)
  })
}

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

async function initializeAndGetInstructions(dir: string): Promise<string> {
  const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
  const url = `http://${host}:${port}/mcp`
  const res = await rpc(url, {
    jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } }
  })
  const body = await readJsonOrSSE(res)
  await app.close()
  expect(body.result.instructions).toEqual(expect.any(String))
  return body.result.instructions as string
}

describe('mcp server instructions: anti-pattern paragraph for list_agents pre-check', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  it('contains the literal substrings list_agents, send_message, unknown_recipient', async () => {
    const dir = tmp(); dirs.push(dir)
    const instructions = await initializeAndGetInstructions(dir)
    expect(instructions).toContain('list_agents')
    expect(instructions).toContain('send_message')
    expect(instructions).toContain('unknown_recipient')
  })

  it('uses jussive prose (DO NOT / MUST NOT + pre) in the same sentence as list_agents', async () => {
    const dir = tmp(); dirs.push(dir)
    const instructions = await initializeAndGetInstructions(dir)
    const sentences = instructions.split(/(?<=[.!?])\s+/)
    const directive = sentences.some(
      s => /do not|must not/i.test(s) && /\bpre/i.test(s) && /list_agents/.test(s)
    )
    expect(directive).toBe(true)
  })

  it('declares list_agents caller-team scope and inability to see other teams', async () => {
    const dir = tmp(); dirs.push(dir)
    const instructions = await initializeAndGetInstructions(dir)
    expect(instructions).toMatch(/caller'?s team|caller-team/i)
    // "CANNOT see cross-team agents" or equivalent — match cross-team plus a negative jussive.
    expect(instructions).toMatch(/cannot.*cross-team|cross-team.*cannot/i)
  })

  it('preserves pre-existing required substrings xats, cross-agent-teams, project_dir', async () => {
    const dir = tmp(); dirs.push(dir)
    const instructions = await initializeAndGetInstructions(dir)
    expect(instructions).toContain('xats')
    expect(instructions).toContain('cross-agent-teams')
    expect(instructions).toContain('project_dir')
  })
})

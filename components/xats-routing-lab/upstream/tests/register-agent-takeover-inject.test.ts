import { describe, it, expect } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type {
  FastifyInstance,
  InjectOptions,
  LightMyRequestResponse,
} from 'fastify'
import { buildServer } from '../src/daemon/server.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'

const CODEX_THREAD_A = '11111111-1111-4111-8111-111111111111'
const CODEX_THREAD_B = '22222222-2222-4222-8222-222222222222'

function requestHeaders(sessionId?: string): Record<string, string> {
  return {
    'accept': 'application/json, text/event-stream',
    'content-type': 'application/json',
    ...(sessionId ? { 'mcp-session-id': sessionId } : {}),
  }
}

function sessionHeaders(sessionId: string): Record<string, string> {
  return {
    'accept': 'application/json, text/event-stream',
    'mcp-session-id': sessionId,
  }
}

async function inject(
  app: FastifyInstance,
  options: InjectOptions
): Promise<LightMyRequestResponse> {
  const response = await app.inject(options)
  const request = response.raw.req
  if (typeof request.socket.destroySoon !== 'function') {
    request.socket.destroySoon = () => request.destroy()
  }
  return response
}

async function initialize(app: FastifyInstance, id: number): Promise<string> {
  const response = await inject(app, {
    method: 'POST',
    url: '/mcp',
    headers: requestHeaders(),
    payload: {
      jsonrpc: '2.0',
      id,
      method: 'initialize',
      params: {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'inject-test', version: '0' },
      },
    },
  })
  expect(response.statusCode).toBe(200)
  return response.headers['mcp-session-id'] as string
}

function parseToolBody(body: string): Record<string, unknown> {
  const data = body
    .split('\n')
    .find(line => line.startsWith('data: '))
  if (!data) throw new Error('missing SSE data')
  const rpc = JSON.parse(data.slice(6)) as {
    result: { content: Array<{ text: string }> }
  }
  return JSON.parse(rpc.result.content[0].text)
}

async function callTool(
  app: FastifyInstance,
  sessionId: string,
  id: number,
  name: string,
  args: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const response = await inject(app, {
    method: 'POST',
    url: '/mcp',
    headers: requestHeaders(sessionId),
    payload: {
      jsonrpc: '2.0',
      id,
      method: 'tools/call',
      params: { name, arguments: args },
    },
  })
  expect(response.statusCode).toBe(200)
  return parseToolBody(response.body)
}

function codexRegistration(thread_id: string): Record<string, unknown> {
  return {
    agent_type: 'codex',
    name: 'alice',
    delivery: {
      kind: 'codex-appserver',
      thread_id,
      ws_url: 'ws://127.0.0.1:8799',
    },
  }
}

describe('register_agent Codex lifecycle through Fastify inject', () => {
  it('keeps the peer registered after one same-thread session closes', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'xats-takeover-inject-'))
    const fanout = new SseFanout({ heartbeatIntervalMs: 60_000 })
    const app = await buildServer({ dbPath: join(dir, 'data.db'), fanout })
    try {
      const first = await initialize(app, 1)
      const second = await initialize(app, 2)
      const firstRegistration = await callTool(
        app,
        first,
        3,
        'register_agent',
        codexRegistration(CODEX_THREAD_A)
      )
      const secondRegistration = await callTool(
        app,
        second,
        4,
        'register_agent',
        codexRegistration(CODEX_THREAD_A)
      )
      expect(secondRegistration.agent_id).toBe(firstRegistration.agent_id)

      const closed = await inject(app, {
        method: 'DELETE',
        url: '/mcp',
        headers: sessionHeaders(second),
      })
      expect(closed.statusCode).toBe(200)

      const inbox = await callTool(app, first, 5, 'get_inbox', {})
      expect(inbox.error).toBeUndefined()
      expect(fanout.peek()).toEqual([
        { agent_id: firstRegistration.agent_id, team: 'default' },
      ])
      const health = await inject(app, { method: 'GET', url: '/health' })
      expect(health.json().mcp_sessions.registered).toBe(1)
    } finally {
      await app.close()
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('different thread removes every old same-thread session', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'xats-takeover-inject-'))
    let lines: string[] = []
    const app = await buildServer({
      dbPath: join(dir, 'data.db'),
      mcpLog: line => { lines = [...lines, line] },
    })
    try {
      const first = await initialize(app, 1)
      const second = await initialize(app, 2)
      const third = await initialize(app, 3)
      await callTool(
        app,
        first,
        4,
        'register_agent',
        codexRegistration(CODEX_THREAD_A)
      )
      await callTool(
        app,
        second,
        5,
        'register_agent',
        codexRegistration(CODEX_THREAD_A)
      )
      await callTool(
        app,
        third,
        6,
        'register_agent',
        codexRegistration(CODEX_THREAD_B)
      )

      for (const sessionId of [first, second]) {
        const response = await inject(app, {
          method: 'POST',
          url: '/mcp',
          headers: requestHeaders(sessionId),
          payload: {
            jsonrpc: '2.0',
            id: 7,
            method: 'tools/list',
            params: {},
          },
        })
        expect(response.statusCode).toBe(404)
      }
      const takeoverLines = lines.filter(line =>
        line.includes('register_agent takeover')
      )
      expect(takeoverLines).toHaveLength(2)
      expect(takeoverLines.some(line => line.includes(first))).toBe(true)
      expect(takeoverLines.some(line => line.includes(second))).toBe(true)
      const inbox = await callTool(app, third, 8, 'get_inbox', {})
      expect(inbox.error).toBeUndefined()
    } finally {
      await app.close()
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

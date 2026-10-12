import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-sm-zod-'))

interface ToolCallResult {
  content?: Array<{ type: string; text: string }>
  isError?: boolean
}

describe('send_message Zod schema', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  async function client(): Promise<{ c: Client; close: () => Promise<void> }> {
    const dir = tmp(); dirs.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0' })
    await c.connect(t)
    return { c, close: async () => { await t.close(); await app.close() } }
  }

  async function registeredClient(): Promise<{ c: Client; close: () => Promise<void> }> {
    const { c, close } = await client()
    await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'test', name: 'caller', team: 'default' } })
    return { c, close }
  }

  function textOf(resp: unknown): string {
    const r = resp as ToolCallResult
    return (r.content ?? []).map(c => c.text).join('\n')
  }

  it('rejects unknown field to_role with validation error', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({ name: 'send_message', arguments: { to_agent_name: 'X', to_role: 'frontend', body: 'hi' } }) as ToolCallResult
    expect(resp.isError).toBe(true)
    expect(textOf(resp)).toMatch(/to_role|unknown|unrecognized|validation/i)
    await close()
  })

  it('rejects missing to_agent_name as schema validation error', async () => {
    const { c, close } = await registeredClient()
    const resp = await c.callTool({ name: 'send_message', arguments: { body: 'hi' } }) as ToolCallResult
    expect(textOf(resp)).toMatch(/to_agent_name|required|validation/i)
    await close()
  })

  it('accepts to_team as optional string', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({ name: 'send_message', arguments: { to_agent_name: 'ghost', to_team: 'beta', body: 'hi' } }) as ToolCallResult
    expect(resp.isError).toBeFalsy()
    expect(textOf(resp)).toMatch(/unknown_agent|unknown_recipient/)
    await close()
  })

  it('accepts to_agent_name alone and returns unknown_recipient (not validation error)', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({ name: 'send_message', arguments: { to_agent_name: 'ghost', body: 'hi' } }) as ToolCallResult
    expect(resp.isError).toBeFalsy()
    expect(textOf(resp)).toMatch(/unknown_recipient|unknown_agent/)
    await close()
  })

  it('accepts need_reply as optional boolean', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({
      name: 'send_message',
      arguments: { to_agent_name: 'ghost', body: 'FYI', need_reply: false }
    }) as ToolCallResult
    expect(resp.isError).toBeFalsy()
    expect(textOf(resp)).toMatch(/unknown_agent|unknown_recipient/)
    await close()
  })

  it('rejects extra to_agent_id field on send_message (additionalProperties: false)', async () => {
    const { c, close } = await registeredClient()
    const resp = await c.callTool({
      name: 'send_message',
      arguments: { to_agent_id: 'X', to_agent_name: 'bob', body: 'hi' }
    }) as ToolCallResult
    expect(resp.isError).toBe(true)
    expect(textOf(resp)).toMatch(/to_agent_id|unrecognized|validation/i)
    await close()
  })
})

describe('send_message_by_id Zod schema', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  async function client(): Promise<{ c: Client; close: () => Promise<void> }> {
    const dir = tmp(); dirs.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0' })
    await c.connect(t)
    return { c, close: async () => { await t.close(); await app.close() } }
  }

  async function registeredClient(): Promise<{ c: Client; close: () => Promise<void> }> {
    const { c, close } = await client()
    await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'test', name: 'caller', team: 'default' } })
    return { c, close }
  }

  function textOf(resp: unknown): string {
    const r = resp as ToolCallResult
    return (r.content ?? []).map(c => c.text).join('\n')
  }

  it('accepts to_agent_id alone and returns unknown_recipient (not validation error)', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({ name: 'send_message_by_id', arguments: { to_agent_id: 'fake', body: 'hi' } }) as ToolCallResult
    expect(resp.isError).toBeFalsy()
    expect(textOf(resp)).toMatch(/unknown_recipient|unknown_agent/)
    await close()
  })

  it('rejects missing to_agent_id as schema validation error', async () => {
    const { c, close } = await registeredClient()
    const resp = await c.callTool({ name: 'send_message_by_id', arguments: { body: 'hi' } }) as ToolCallResult
    expect(resp.isError).toBe(true)
    expect(textOf(resp)).toMatch(/to_agent_id|required|validation/i)
    await close()
  })

  it('rejects extra to_agent_name field on send_message_by_id', async () => {
    const { c, close } = await registeredClient()
    const resp = await c.callTool({
      name: 'send_message_by_id',
      arguments: { to_agent_id: 'X', to_agent_name: 'bob', body: 'hi' }
    }) as ToolCallResult
    expect(resp.isError).toBe(true)
    expect(textOf(resp)).toMatch(/to_agent_name|unrecognized|validation/i)
    await close()
  })

  it('rejects extra to_team field on send_message_by_id', async () => {
    const { c, close } = await registeredClient()
    const resp = await c.callTool({
      name: 'send_message_by_id',
      arguments: { to_agent_id: 'X', to_team: 'beta', body: 'hi' }
    }) as ToolCallResult
    expect(resp.isError).toBe(true)
    expect(textOf(resp)).toMatch(/to_team|unrecognized|validation/i)
    await close()
  })

  it('accepts need_reply as optional boolean', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({
      name: 'send_message_by_id',
      arguments: { to_agent_id: 'fake', body: 'FYI', need_reply: false }
    }) as ToolCallResult
    expect(resp.isError).toBeFalsy()
    expect(textOf(resp)).toMatch(/unknown_agent|unknown_recipient/)
    await close()
  })
})

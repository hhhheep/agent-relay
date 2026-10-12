import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-kimi-schema-'))

async function setupInMemory() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const server = new McpServer({ name: 'test', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-kimi-schema'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
    undefined,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'test-client', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct }
}

describe('register_agent({agent_type:"kimi-code"})', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  async function callRegister(args: Record<string, unknown>) {
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)
    const resp = await client.callTool({
      name: 'register_agent',
      arguments: args,
    }) as { isError?: boolean; content: Array<{ text: string }> }
    return { resp, db, server, client, transport }
  }

  async function teardown(ctx: { db: any; server: any; client: any; transport: any }) {
    await ctx.transport.close()
    await ctx.client.close()
    ctx.db.close()
    await ctx.server.close()
  }

  it('writes kimi-server delivery and returns { agent_id, team, session_id, base_url }', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      team: 'default',
      base_url: 'http://127.0.0.1:58627',
      session_id: 'session_abc',
    })
    expect(ctx.resp.isError).toBeFalsy()
    const obj = JSON.parse(ctx.resp.content[0].text)
    expect(obj.agent_id).toBeDefined()
    expect(obj.team).toBe('default')
    expect(obj.session_id).toBe('session_abc')
    expect(obj.base_url).toBe('http://127.0.0.1:58627')

    const row = ctx.db.prepare(
      'SELECT agent_type, model, delivery_kind, delivery_payload FROM agents WHERE team=? AND name=?'
    ).get('default', 'kimi-1') as {
      agent_type: string
      model: string | null
      delivery_kind: string
      delivery_payload: string
    }
    expect(row.agent_type).toBe('kimi-code')
    expect(row.delivery_kind).toBe('kimi-server')
    expect(JSON.parse(row.delivery_payload)).toEqual({
      session_id: 'session_abc',
      base_url: 'http://127.0.0.1:58627',
    })
    await teardown(ctx)
  })

  it('persists model NULL when model is omitted', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      base_url: 'http://127.0.0.1:58627',
      session_id: 'session_abc',
    })
    expect(ctx.resp.isError).toBeFalsy()
    const row = ctx.db.prepare(
      'SELECT model FROM agents WHERE name=?'
    ).get('kimi-1') as { model: string | null }
    expect(row.model).toBeNull()
    await teardown(ctx)
  })

  it('preserves auth_token_ref in delivery_payload', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      base_url: 'http://127.0.0.1:58627',
      session_id: 'session_abc',
      auth_token_ref: 'KIMI_SERVER_TOKEN',
    })
    expect(ctx.resp.isError).toBeFalsy()
    const row = ctx.db.prepare(
      'SELECT delivery_payload FROM agents WHERE name=?'
    ).get('kimi-1') as { delivery_payload: string }
    expect(JSON.parse(row.delivery_payload)).toEqual({
      session_id: 'session_abc',
      base_url: 'http://127.0.0.1:58627',
      auth_token_ref: 'KIMI_SERVER_TOKEN',
    })
    await teardown(ctx)
  })

  it('rejects agent_type=kimi-code with missing session_id', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      base_url: 'http://127.0.0.1:58627',
    })
    expect(ctx.resp.isError).toBe(true)
    const text = ctx.resp.content[0].text
    expect(text).toMatch(/session_id/i)
    expect(text).toMatch(/KIMI_XATS_SESSION_ID/i)
    const count = ctx.db.prepare('SELECT COUNT(*) AS n FROM agents').get() as { n: number }
    expect(count.n).toBe(0)
    await teardown(ctx)
  })

  it('rejects agent_type=kimi-code with missing base_url', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      session_id: 'session_abc',
    })
    expect(ctx.resp.isError).toBe(true)
    const text = ctx.resp.content[0].text
    expect(text).toMatch(/base_url/i)
    expect(text).toMatch(/KIMI_XATS_BASE_URL/i)
    const count = ctx.db.prepare('SELECT COUNT(*) AS n FROM agents').get() as { n: number }
    expect(count.n).toBe(0)
    await teardown(ctx)
  })

  it('rejects agent_type=kimi-code with ws:// base_url (protocol mismatch)', async () => {
    const ctx = await callRegister({
      agent_type: 'kimi-code',
      name: 'kimi-1',
      base_url: 'ws://127.0.0.1:58627',
      session_id: 'session_abc',
    })
    expect(ctx.resp.isError).toBe(true)
    expect(ctx.resp.content[0].text).toMatch(/base_url/i)
    await teardown(ctx)
  })

  it('rejects agent_type=kimi-code base_url carrying query, fragment, or userinfo', async () => {
    for (const badBase of [
      'http://127.0.0.1:58627/?a=1',
      'http://127.0.0.1:58627/#frag',
      'http://user:pw@127.0.0.1:58627',
      'http://127.0.0.1:58627/?',
      'http://127.0.0.1:58627/?#',
    ]) {
      const ctx = await callRegister({
        agent_type: 'kimi-code',
        name: 'kimi-1',
        base_url: badBase,
        session_id: 'session_abc',
      })
      expect(ctx.resp.isError, `base_url=${badBase} should be rejected`).toBe(true)
      expect(ctx.resp.content[0].text).toMatch(/base_url/i)
      await teardown(ctx)
    }
  })

  it('rejects a kimi-server delivery object whose base_url carries a query (custom agent_type bypass)', async () => {
    const ctx = await callRegister({
      agent_type: 'custom',
      agent_type_name: 'cursor',
      name: 'kimi-1',
      delivery: {
        kind: 'kimi-server',
        session_id: 'session_abc',
        base_url: 'http://127.0.0.1:58627/?a=1',
      },
    })
    const body = JSON.parse(ctx.resp.content[0].text) as Record<string, unknown>
    expect(body.error).toBe('invalid_delivery')
    expect(body.reason).toBe('invalid_base_url')
    const count = ctx.db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    await teardown(ctx)
  })

  it('rejects base_url supplied with agent_type=custom', async () => {
    const ctx = await callRegister({
      agent_type: 'custom',
      agent_type_name: 'cursor',
      name: 'kimi-1',
      base_url: 'http://127.0.0.1:58627',
    })
    expect(ctx.resp.isError).toBe(true)
    expect(ctx.resp.content[0].text).toMatch(/agent_type=opencode/i)
    await teardown(ctx)
  })

  it('register_agent description contains KIMI_XATS_BASE_URL and KIMI_XATS_SESSION_ID', async () => {
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)
    const { tools } = await client.listTools()
    const registerTool = tools.find(t => t.name === 'register_agent')
    expect(registerTool).toBeDefined()
    expect(registerTool!.description).toContain('KIMI_XATS_BASE_URL')
    expect(registerTool!.description).toContain('KIMI_XATS_SESSION_ID')
    expect(registerTool!.description).toContain('kimi-code')
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

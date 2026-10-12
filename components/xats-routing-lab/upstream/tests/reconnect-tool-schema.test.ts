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

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-reconnect-schema-'))

async function setup() {
  const dir = tmp()
  const dbPath = join(dir, 'data.db')
  const db = openDb(dbPath)
  applySchema(db)
  const server = new McpServer({ name: 'test-server', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-reconnect-schema'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'claude-code', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct }
}

describe('reconnect tool schema validation', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('exposes reconnect with ui_pid, thread_id and base_url in tools/list', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    const resp = await client.listTools()
    const tool = resp.tools.find(t => t.name === 'reconnect')
    expect(tool).toBeDefined()
    expect(tool!.inputSchema).toMatchObject({
      type: 'object',
      properties: expect.objectContaining({
        ui_pid: expect.anything(),
        thread_id: expect.anything(),
        ws_url: expect.anything(),
        auth_token_ref: expect.anything(),
        base_url: expect.anything(),
        session_id: expect.anything(),
      }),
    })
    expect(tool!.description).toContain('thread_id=$CODEX_THREAD_ID')
    expect(tool!.description).toContain('Mac Codex App')
    expect(tool!.description).toContain('context clear')
    expect(tool!.description).toContain('conversation resume')
    expect(tool!.description).not.toContain('App restart')
    expect(tool!.description).toContain('thread/resume')
    expect(tool!.description).toContain('No agent row is mutated')
    expect(tool!.description).toContain('stale stored identity')
    expect(tool!.description).toContain('base_url=$OPENCODE_XATS_BASE_URL')
    expect(tool!.description).toContain('auth_ambiguous')
    expect(tool!.description).toContain('mix ref and no-ref')
    expect(JSON.stringify(tool!.inputSchema)).toContain('Mac Codex App')
    await transport.close()
    await client.close()
    await server.close()
  })

  it('rejects ui_pid: 0 at the schema layer without reading/mutating a row', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const resp = await client.callTool({ name: 'reconnect', arguments: { ui_pid: 0 } }) as {
      isError?: boolean
    }
    expect(resp.isError).toBe(true)
    const count = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('rejects missing ui_pid (reconnect({})) at the schema layer', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const resp = await client.callTool({ name: 'reconnect', arguments: {} }) as {
      isError?: boolean
    }
    expect(resp.isError).toBe(true)
    const count = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('rejects non-integer / negative ui_pid', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    for (const bad of [-1, 1.5]) {
      const resp = await client.callTool({ name: 'reconnect', arguments: { ui_pid: bad } }) as {
        isError?: boolean
      }
      expect(resp.isError, `ui_pid=${bad} should be rejected`).toBe(true)
    }
    await transport.close()
    await client.close()
    await server.close()
  })

  it('rejects invalid thread_id and mixed reconnect keys', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    const invalid = await client.callTool({
      name: 'reconnect',
      arguments: { thread_id: 'not-a-uuid' },
    }) as { isError?: boolean }
    expect(invalid.isError).toBe(true)

    const mixed = await client.callTool({
      name: 'reconnect',
      arguments: {
        ui_pid: 25079,
        thread_id: '11111111-1111-4111-8111-111111111111',
      },
    }) as { isError?: boolean }
    expect(mixed.isError).toBe(true)

    const claudeWithCodexOption = await client.callTool({
      name: 'reconnect',
      arguments: { ui_pid: 25079, ws_url: 'ws://127.0.0.1:8799' },
    }) as { isError?: boolean }
    expect(claudeWithCodexOption.isError).toBe(true)

    const invalidWsUrl = await client.callTool({
      name: 'reconnect',
      arguments: {
        thread_id: '11111111-1111-4111-8111-111111111111',
        ws_url: 'https://example.com',
      },
    }) as { isError?: boolean }
    expect(invalidWsUrl.isError).toBe(true)

    await transport.close()
    await client.close()
    await server.close()
  })

  it('rejects invalid opencode reconnect keys at the schema layer', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)

    // non-http base_url
    const badProto = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: 'ftp://127.0.0.1:18888' },
    }) as { isError?: boolean }
    expect(badProto.isError).toBe(true)

    // unparseable base_url
    const unparseable = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: 'not-a-url' },
    }) as { isError?: boolean }
    expect(unparseable.isError).toBe(true)

    // session_id without base_url
    const sessionWithoutBase = await client.callTool({
      name: 'reconnect',
      arguments: { session_id: 'ses_xyz' },
    }) as { isError?: boolean }
    expect(sessionWithoutBase.isError).toBe(true)

    // session_id not starting with "ses"
    const badSession = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: 'http://127.0.0.1:18888', session_id: 'nope' },
    }) as { isError?: boolean }
    expect(badSession.isError).toBe(true)

    // auth_token_ref without any primary key
    const orphanAuth = await client.callTool({
      name: 'reconnect',
      arguments: { auth_token_ref: 'OPENSEND_TOKEN' },
    }) as { isError?: boolean }
    expect(orphanAuth.isError).toBe(true)

    // mixed primary keys: base_url + ui_pid
    const mixed = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: 'http://127.0.0.1:18888', ui_pid: 25079 },
    }) as { isError?: boolean }
    expect(mixed.isError).toBe(true)

    await transport.close()
    await client.close()
    await server.close()
  })

  it('accepts opencode reconnect shapes at the schema layer (no row mutation)', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)

    const shapes = [
      { base_url: 'http://127.0.0.1:18888' },
      { base_url: 'http://127.0.0.1:18888', session_id: 'ses_xyz' },
      { base_url: 'http://127.0.0.1:18888', auth_token_ref: 'OPENSEND_TOKEN' },
    ]
    for (const args of shapes) {
      // Schema acceptance = the call is NOT rejected with isError at the
      // schema layer. Runtime errors (e.g. opencode_unreachable because no
      // live server is configured here) are fine and still prove acceptance.
      const resp = await client.callTool({ name: 'reconnect', arguments: args }) as {
        isError?: boolean
      }
      expect(resp.isError, `reconnect(${JSON.stringify(args)}) should pass schema`).toBeFalsy()
    }

    const count = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('validates the agent_type discriminator on the base_url arm', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)

    // agent_type without base_url
    const orphanKind = await client.callTool({
      name: 'reconnect',
      arguments: { agent_type: 'kimi-code' },
    }) as { isError?: boolean }
    expect(orphanKind.isError).toBe(true)

    // agent_type="kimi-code" without the REQUIRED session_id
    const kimiWithoutSession = await client.callTool({
      name: 'reconnect',
      arguments: { agent_type: 'kimi-code', base_url: 'http://127.0.0.1:18888' },
    }) as { isError?: boolean }
    expect(kimiWithoutSession.isError).toBe(true)

    // unknown discriminator value
    const badKind = await client.callTool({
      name: 'reconnect',
      arguments: { agent_type: 'codex', base_url: 'http://127.0.0.1:18888' },
    }) as { isError?: boolean }
    expect(badKind.isError).toBe(true)

    // full kimi shape passes the schema (runtime need_register is fine)
    const kimiShape = await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: 'http://127.0.0.1:18888',
        session_id: 'session_xyz',
      },
    }) as { isError?: boolean }
    expect(kimiShape.isError).toBeFalsy()

    // kimi ids only need to be non-empty (registration parity): the "ses"
    // prefix is an opencode contract and must not gate an explicit kimi call
    const nonSesKimi = await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: 'http://127.0.0.1:18888',
        session_id: 'S',
      },
    }) as { isError?: boolean }
    expect(nonSesKimi.isError).toBeFalsy()

    // without the kimi discriminator the historical prefix rule still holds
    const nonSesLegacy = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: 'http://127.0.0.1:18888', session_id: 'S' },
    }) as { isError?: boolean }
    expect(nonSesLegacy.isError).toBe(true)

    // blank session_id (register parity: trimmed non-empty)
    const blankSession = await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: 'http://127.0.0.1:18888',
        session_id: '   ',
      },
    }) as { isError?: boolean }
    expect(blankSession.isError).toBe(true)

    // kimi base_url must not carry query/fragment/userinfo — endpoint URLs
    // are built by appending /api/v1/... to it
    for (const badBase of [
      'http://127.0.0.1:18888/?a=1',
      'http://127.0.0.1:18888/#frag',
      'http://user:pw@127.0.0.1:18888',
      'http://127.0.0.1:18888/?',
      'http://127.0.0.1:18888/?#',
    ]) {
      const resp = await client.callTool({
        name: 'reconnect',
        arguments: {
          agent_type: 'kimi-code',
          base_url: badBase,
          session_id: 'session_xyz',
        },
      }) as { isError?: boolean }
      expect(resp.isError, `base_url=${badBase} should be rejected`).toBe(true)
    }

    await transport.close()
    await client.close()
    await server.close()
  })
})

import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-register-tool-schema-'))

describe('register_agent tool schema', () => {
  const cleanups: string[] = []

  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('exposes register_agent arguments in tools/list', async () => {
    const dir = tmp()
    cleanups.push(dir)

    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'schema-check', version: '0.0.0' })
    await client.connect(transport)

    const resp = await client.listTools()
    const tool = resp.tools.find(x => x.name === 'register_agent')

    expect(tool).toBeDefined()
    expect(tool!.inputSchema).toMatchObject({
      type: 'object',
      properties: expect.objectContaining({
        model: expect.anything(),
        name: expect.anything(),
        device: expect.anything(),
        role: expect.anything(),
        team: expect.anything(),
        project_dir: expect.anything(),
        agent_type: expect.anything(),
        agent_type_name: expect.anything(),
        ui_pid: expect.anything(),
        channel_session_id: expect.anything(),
        thread_id: expect.anything(),
        ws_url: expect.anything(),
        auth_token_ref: expect.anything(),
        identity_key: expect.anything(),
        delivery: expect.anything(),
      }),
      required: expect.arrayContaining(['agent_type', 'name']),
      additionalProperties: false,
    })

    await transport.terminateSession()
    await client.close()
    await app.close()
  })

  it('rejects a blank identity_key before any row is written', async () => {
    const dir = tmp()
    cleanups.push(dir)

    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'identity-key-check', version: '0.0.0' })
    await client.connect(transport)

    for (const identity_key of ['', '   ']) {
      const resp = await client.callTool({
        name: 'register_agent',
        arguments: { agent_type: 'custom', agent_type_name: 'x', name: 'alice', identity_key },
      }) as { isError?: boolean }
      expect(resp.isError, `identity_key=${JSON.stringify(identity_key)}`).toBe(true)
    }

    const db = openDb(dbPath)
    const count = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    db.close()

    await transport.terminateSession()
    await client.close()
    await app.close()
  })

  it('rejects legacy `client` key with a rename hint', async () => {
    const dir = tmp()
    cleanups.push(dir)

    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'legacy-key-check', version: '0.0.0' })
    await client.connect(transport)

    const resp = await client.callTool({
      name: 'register_agent',
      arguments: {
        client: 'custom',
        name: 'alice',
      } as unknown as Record<string, unknown>,
    })
    const errResp = resp as { isError?: boolean; content: Array<{ text: string }> }
    expect(errResp.isError).toBe(true)
    expect(errResp.content[0].text).toMatch(/client/)
    expect(errResp.content[0].text).toMatch(/agent_type/)

    await transport.terminateSession()
    await client.close()
    await app.close()
  })

  it('rejects legacy `client_name` key with a rename hint', async () => {
    const dir = tmp()
    cleanups.push(dir)

    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'legacy-key-check-2', version: '0.0.0' })
    await client.connect(transport)

    const resp = await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        client_name: 'cursor',
        name: 'alice',
      } as unknown as Record<string, unknown>,
    })
    const errResp = resp as { isError?: boolean; content: Array<{ text: string }> }
    expect(errResp.isError).toBe(true)
    expect(errResp.content[0].text).toMatch(/client_name/)
    expect(errResp.content[0].text).toMatch(/agent_type_name/)

    await transport.terminateSession()
    await client.close()
    await app.close()
  })
})

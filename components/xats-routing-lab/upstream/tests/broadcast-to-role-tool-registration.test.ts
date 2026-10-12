import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-btr-reg-'))

describe('broadcast_to_role MCP tool registration', () => {
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

  it('tools/list exposes broadcast_to_role', async () => {
    const { c, close } = await client()
    const resp = await c.listTools()
    const names = resp.tools.map(t => t.name)
    expect(names).toContain('broadcast_to_role')
    await close()
  })

  it('broadcast_to_role rejects to_team via Zod strict schema', async () => {
    const { c, close } = await client()
    const resp = await c.callTool({ name: 'broadcast_to_role', arguments: { to_role: 'x', to_team: 'beta', body: 'hi' } }) as { isError?: boolean; content: Array<{ text?: string }> }
    expect(resp.isError).toBe(true)
    const text = resp.content.map(p => p.text ?? '').join(' ')
    expect(text).toMatch(/to_team|unknown|unrecognized|validation/i)
    await close()
  })
})

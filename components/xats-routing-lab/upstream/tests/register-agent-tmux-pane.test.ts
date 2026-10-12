import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-mcp-pane-'))

describe('register_agent tmux handling integration', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('rejects explicit tmux_pane_id input and creates no row', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const transport = new StreamableHTTPClientTransport(url)
    const client = new Client({ name: 'test', version: '0.0.0' })
    await client.connect(transport)

    const regResp = await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus-4-7', role: 'frontend', name: 'alice', tmux_pane_id: '%42', team: 'default' }
    })
    const errResp = regResp as { isError?: boolean; content: Array<{ text: string }> }
    expect(errResp.isError).toBe(true)
    expect(errResp.content[0].text).toMatch(/tmux_pane_id/i)

    const db = openDb(dbPath); applySchema(db)
    const row = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(row.c).toBe(0)
    db.close()

    await transport.terminateSession()
    await client.close()
    await app.close()
  })

  it('list_agents returns tmux_pane_id: null when registration does not find a pane', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const makeClient = async () => {
      const t = new StreamableHTTPClientTransport(url)
      const c = new Client({ name: 'test', version: '0.0.0' })
      await c.connect(t)
      return { c, t }
    }
    const A = await makeClient()
    const regA = JSON.parse(((await A.c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus-4-7', role: 'frontend', name: 'alice' } })).content as Array<{ text: string }>)[0].text) as { agent_id: string }
    const list = JSON.parse(((await A.c.callTool({ name: 'list_agents', arguments: {} })).content as Array<{ text: string }>)[0].text) as { agents: Array<{ agent_id: string; tmux_pane_id: string | null }> }
    const a = list.agents.find(x => x.agent_id === regA.agent_id)
    expect(a?.tmux_pane_id).toBeNull()
    await A.t.terminateSession()
    await A.c.close()
    await app.close()
  })
})

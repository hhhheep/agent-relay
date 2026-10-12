import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-name-req-'))

interface Booted {
  app: Awaited<ReturnType<typeof startServer>>['app']
  host: string
  port: number
  dbPath: string
  cleanup: () => void
}

async function boot(): Promise<Booted> {
  const dir = tmp()
  const dbPath = join(dir, 'data.db')
  const { app, host, port } = await startServer({ dbPath, port: 0 })
  return { app, host, port, dbPath, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

async function connect(host: string, port: number) {
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'test', version: '0.0.0' })
  await c.connect(t)
  return { c, t }
}

function countAgents(dbPath: string): number {
  const db = openDb(dbPath)
  applySchema(db)
  const row = db.prepare('SELECT COUNT(*) AS c FROM agents').get() as { c: number }
  db.close()
  return row.c
}

describe('register_agent name field validation', () => {
  const teardown: Array<() => Promise<void> | void> = []
  afterEach(async () => {
    for (const t of teardown.reverse()) { try { await t() } catch { /* ignore */ } }
    teardown.length = 0
  })

  it('rejects when name is missing', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connect(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const resp = await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend' } }) as { isError?: boolean; content: Array<{ text: string }> }
    expect(resp.isError).toBe(true)
    expect(resp.content[0].text).toMatch(/validation|invalid/i)
    expect(countAgents(b.dbPath)).toBe(0)
  })

  it('rejects when name is whitespace only', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connect(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const resp = await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: '   ' } }) as { isError?: boolean; content: Array<{ text: string }> }
    expect(resp.isError).toBe(true)
    expect(resp.content[0].text).toMatch(/name/i)
    expect(countAgents(b.dbPath)).toBe(0)
  })

  it('role defaults to "default" when omitted', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connect(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const resp = await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', name: 'alice' } })
    const obj = JSON.parse((resp.content as Array<{ text: string }>)[0].text) as { agent_id: string }
    const db = openDb(b.dbPath); applySchema(db)
    const row = db.prepare('SELECT role FROM agents WHERE agent_id=?').get(obj.agent_id) as { role: string }
    expect(row.role).toBe('default')
    db.close()
  })

  it('team defaults to "default" when team and project_dir are omitted', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connect(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const resp = await c.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', model: 'opus', name: 'alice', role: 'backend' } })
    const obj = JSON.parse((resp.content as Array<{ text: string }>)[0].text) as { team: string }
    expect(obj.team).toBe('default')
  })

  it('derives team from project_dir and does not return project_dir', async () => {
    const b = await boot(); teardown.push(() => b.cleanup()); teardown.push(() => b.app.close())
    const { c, t } = await connect(b.host, b.port)
    teardown.push(async () => { await t.close(); await c.close() })
    const resp = await c.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        model: 'opus',
        name: 'alice',
        role: 'backend',
        project_dir: '/x/y/Cross-Agent-Teams-MCP/',
      },
    })
    const obj = JSON.parse((resp.content as Array<{ text: string }>)[0].text) as {
      team: string
      project_dir?: string
    }
    expect(obj.team).toBe('cross-agent-teams-mcp')
    expect(obj.project_dir).toBeUndefined()

    const listedResp = await c.callTool({ name: 'list_agents', arguments: {} })
    const listed = JSON.parse((listedResp.content as Array<{ text: string }>)[0].text) as {
      agents: Array<Record<string, unknown>>
    }
    expect(listed.agents).toHaveLength(1)
    expect(listed.agents[0].team).toBe('cross-agent-teams-mcp')
    expect(Object.prototype.hasOwnProperty.call(listed.agents[0], 'project_dir')).toBe(false)
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-register-claude-ui-pid-'))

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

async function setup(channelWakeFanout?: ChannelWakeFanout) {
  const dir = tmp()
  const dbPath = join(dir, 'data.db')
  const db = openDb(dbPath)
  applySchema(db)
  const server = new McpServer({ name: 'test-server', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-claude-ui-pid'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
    channelWakeFanout,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'fake-proxy', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct }
}

describe('register_agent schema: claude_ui_pid', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  async function expectInvalid(client: Client, args: Record<string, unknown>): Promise<string> {
    const resp = await client.callTool({ name: 'register_agent', arguments: args }) as {
      isError?: boolean
      content?: Array<{ text?: string }>
    }
    expect(resp.isError).toBe(true)
    const text = resp.content?.[0]?.text ?? ''
    return text
  }

  it("rejects claude_ui_pid when role != '__channel_proxy__' (6.1)", async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const text = await expectInvalid(client, {
      agent_type: 'custom',
      model: 'opus',
      role: 'worker',
      name: 'alice',
      claude_ui_pid: 25424,
    })
    expect(text).toMatch(/claude_ui_pid/)
    // DB must not have a row
    const row = db.prepare(`SELECT COUNT(*) AS c FROM agents WHERE name='alice'`).get() as { c: number }
    expect(row.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('rejects non-positive / non-integer claude_ui_pid (6.2)', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    await expectInvalid(client, {
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'proxy-1',
      claude_ui_pid: 0,
    })
    await expectInvalid(client, {
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'proxy-2',
      claude_ui_pid: -5,
    })
    await expectInvalid(client, {
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'proxy-3',
      claude_ui_pid: 1.5,
    })
    const row = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(row.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it("persists claude_ui_pid when role='__channel_proxy__' (6.3)", async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const resp = await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        agent_type_name: 'cross-agent-teams-channel',
        model: 'proxy',
        role: '__channel_proxy__',
        name: 'channel-proxy-27245',
        team: 'default',
        claude_ui_pid: 25424,
        delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
      },
    })
    const obj = await parseTool(resp)
    expect(obj.agent_id).toBeDefined()
    const row = db.prepare(
      `SELECT claude_ui_pid, delivery_kind, delivery_payload FROM agents WHERE team='default' AND name='channel-proxy-27245'`
    ).get() as { claude_ui_pid: number | null; delivery_kind: string; delivery_payload: string | null }
    expect(row.claude_ui_pid).toBe(25424)
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-abc' })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('preserves existing claude_ui_pid on re-register without the field', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        model: 'proxy',
        role: '__channel_proxy__',
        name: 'p1',
        team: 'default',
        claude_ui_pid: 25424,
        delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
      },
    })
    await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        model: 'proxy',
        role: '__channel_proxy__',
        name: 'p1',
        team: 'default',
        delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
      },
    })
    const row = db.prepare(
      `SELECT claude_ui_pid FROM agents WHERE team='default' AND name='p1'`
    ).get() as { claude_ui_pid: number | null }
    expect(row.claude_ui_pid).toBe(25424)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

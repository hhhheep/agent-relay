import { describe, it, expect, afterEach, vi } from 'vitest'
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

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-register-delivery-'))
const { detectTmuxPaneMock } = vi.hoisted(() => ({
  detectTmuxPaneMock: vi.fn(),
}))

vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: detectTmuxPaneMock,
}))

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const result = resp as { content: Array<{ text: string }> }
  return JSON.parse(result.content[0].text)
}

describe('register_agent delivery integration', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
  })

  async function setup(channelWakeFanout?: ChannelWakeFanout) {
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const dir = tmp()
    cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const db = openDb(dbPath)
    applySchema(db)
    const server = new McpServer({ name: 'test-server', version: '0.0.0' })
    const holder: { current: string | undefined } = { current: undefined }
    const sessionId = 'session-register-delivery'
    registerBusinessTools(
      server,
      db,
      () => holder.current ?? sessionId,
      undefined,
      (agentId) => { holder.current = agentId },
      () => sessionId
      ,
      channelWakeFanout
    )
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await server.connect(serverTransport)
    const client = new Client({ name: 'test', version: '0.0.0' })
    await client.connect(clientTransport)
    return { dir, dbPath, db, server, client, transport: clientTransport }
  }

  it('register without delivery persists kind none', async () => {
    const { db, server, client, transport } = await setup()
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' },
    }))
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id=?`
    ).get(result.agent_id) as {
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.delivery_kind).toBe('none')
    expect(row.delivery_payload).toBeNull()
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register with claude-channel delivery persists identity and delivery atomically', async () => {
    const { db, server, client, transport } = await setup()
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'claude-code',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
      },
    }))
    const row = db.prepare(
      `SELECT name, role, delivery_kind, delivery_payload FROM agents WHERE agent_id=?`
    ).get(result.agent_id) as {
      name: string
      role: string
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.name).toBe('alice')
    expect(row.role).toBe('backend')
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({
      channel_session_id: 'csid-abc',
    })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register with invalid claude-channel delivery returns invalid_delivery and writes no row', async () => {
    const { db, server, client, transport } = await setup()
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'claude-code',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        delivery: { kind: 'claude-channel' },
      },
    }))
    expect(result).toEqual({
      error: 'invalid_delivery',
      reason: 'missing_channel_session_id',
    })
    const row = db.prepare(
      `SELECT agent_id FROM agents WHERE team='default' AND name='alice'`
    ).get() as { agent_id: string } | undefined
    expect(row).toBeUndefined()
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register with codex-appserver delivery persists identity and delivery atomically', async () => {
    const { db, server, client, transport } = await setup()
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'codex',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        delivery: {
          kind: 'codex-appserver',
          thread_id: '11111111-1111-4111-8111-111111111111',
          ws_url: 'wss://example.test/ws',
          auth_token_ref: 'CODEX_REMOTE_TOKEN',
        },
      },
    }))
    expect(result.agent_id).toBeDefined()
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE team='default' AND name='alice'`
    ).get() as {
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.delivery_kind).toBe('codex-appserver')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({
      thread_id: '11111111-1111-4111-8111-111111111111',
      ws_url: 'wss://example.test/ws',
      auth_token_ref: 'CODEX_REMOTE_TOKEN',
    })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register with invalid codex-appserver delivery returns invalid_thread_id and writes no row', async () => {
    const { db, server, client, transport } = await setup()
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'codex',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        delivery: {
          kind: 'codex-appserver',
          thread_id: 'thread-1',
          ws_url: 'wss://example.test/ws',
        },
      },
    }))
    expect(result).toEqual({
      error: 'invalid_delivery',
      reason: 'invalid_thread_id',
    })
    const row = db.prepare(
      `SELECT agent_id FROM agents WHERE team='default' AND name='alice'`
    ).get() as { agent_id: string } | undefined
    expect(row).toBeUndefined()
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register with agent_type=claude-code and channel_session_id binds channel delivery', async () => {
    const channelWakeFanout = new ChannelWakeFanout()
    channelWakeFanout.attach('csid-abc', () => { /* sink */ }, 'sess-proxy')
    const { db, server, client, transport } = await setup(channelWakeFanout)
    const result = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'claude-code',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        channel_session_id: 'csid-abc',
      },
    }))
    expect(result.agent_id).toBeDefined()
    const row = db.prepare(
      `SELECT agent_type, delivery_kind, delivery_payload FROM agents WHERE team='default' AND name='alice'`
    ).get() as {
      agent_type: string | null
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.agent_type).toBe('claude-code')
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({
      channel_session_id: 'csid-abc',
    })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('re-register without delivery preserves existing non-none delivery', async () => {
    const { db, server, client, transport } = await setup()
    const first = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'claude-code',
        model: 'opus',
        role: 'backend',
        name: 'alice',
        delivery: { kind: 'claude-channel', channel_session_id: 'csid-first' },
      },
    }))
    const second = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        model: 'sonnet',
        role: 'backend',
        name: 'alice',
      },
    }))
    expect(second.agent_id).toBe(first.agent_id)

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const alice = (list.agents as Array<Record<string, unknown>>).find(
      row => row.agent_id === first.agent_id
    )
    expect(alice).toMatchObject({
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-first' },
      channel_session_id: 'csid-first',
    })

    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE agent_id=?`
    ).get(first.agent_id) as {
      delivery_kind: string
      delivery_payload: string | null
    }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({
      channel_session_id: 'csid-first',
    })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

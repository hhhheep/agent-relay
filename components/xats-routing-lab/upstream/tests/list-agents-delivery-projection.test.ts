import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-list-agents-projection-'))

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

describe('list_agents delivery public projection (W2)', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
  })

  async function setup() {
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const dir = tmp()
    cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const server = new McpServer({ name: 'test-server', version: '0.0.0' })
    const holder: { current: string | undefined } = { current: undefined }
    const sessionId = 'session-list-agents-projection'
    registerBusinessTools(
      server,
      db,
      () => holder.current ?? sessionId,
      undefined,
      (agentId) => { holder.current = agentId },
      () => sessionId,
    )
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await server.connect(serverTransport)
    const client = new Client({ name: 'test', version: '0.0.0' })
    await client.connect(clientTransport)
    return { db, server, client, transport: clientTransport, holder }
  }

  it('list_agents hides codex-appserver routing fields (thread_id, ws_url, auth_token_ref)', async () => {
    const { db, server, client, transport, holder } = await setup()
    // Register caller so list_agents passes requireAgent.
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'caller' },
    }))
    holder.current = caller.agent_id as string

    // Seed a peer codex-appserver agent directly via AgentsRepo.setDelivery,
    // bypassing register_agent codex branching.
    const repo = new AgentsRepo(db)
    const peer = repo.register({
      model: 'gpt-5',
      role: 'backend',
      name: 'carol',
      team: 'default',
    })
    repo.setDelivery(peer.agent_id, {
      kind: 'codex-appserver',
      thread_id: '11111111-1111-4111-8111-111111111111',
      ws_url: 'ws://127.0.0.1:8799',
      auth_token_ref: 'env:TOKEN',
    })

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const peerRow = (list.agents as Array<Record<string, unknown>>).find(
      row => row.agent_id === peer.agent_id,
    )!
    const delivery = peerRow.delivery as Record<string, unknown>
    expect(delivery.kind).toBe('codex-appserver')
    expect(Object.prototype.hasOwnProperty.call(delivery, 'thread_id')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(delivery, 'ws_url')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(delivery, 'auth_token_ref')).toBe(false)
    expect(peerRow.channel_session_id).toBeNull()

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('list_agents surfaces claude-channel delivery with channel_session_id', async () => {
    const { db, server, client, transport, holder } = await setup()
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'caller' },
    }))
    holder.current = caller.agent_id as string

    const repo = new AgentsRepo(db)
    const peer = repo.register({
      model: 'sonnet',
      role: 'backend',
      name: 'alice',
      team: 'default',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-abc' },
    })

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const peerRow = (list.agents as Array<Record<string, unknown>>).find(
      row => row.agent_id === peer.agent_id,
    )!
    expect(peerRow.delivery).toEqual({
      kind: 'claude-channel',
      channel_session_id: 'csid-abc',
    })
    expect(peerRow.channel_session_id).toBe('csid-abc')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('list_agents surfaces kind none for agents without delivery', async () => {
    const { db, server, client, transport, holder } = await setup()
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'caller' },
    }))
    holder.current = caller.agent_id as string

    const repo = new AgentsRepo(db)
    const peer = repo.register({
      model: 'sonnet',
      role: 'backend',
      name: 'bob',
      team: 'default',
    })

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const peerRow = (list.agents as Array<Record<string, unknown>>).find(
      row => row.agent_id === peer.agent_id,
    )!
    expect(peerRow.delivery).toEqual({ kind: 'none' })
    expect(peerRow.channel_session_id).toBeNull()

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('list_agents includes device and does not expose origin or remote_addr', async () => {
    const { db, server, client, transport, holder } = await setup()
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'caller' },
    }))
    holder.current = caller.agent_id as string

    const repo = new AgentsRepo(db)
    const peer = repo.register({
      device: 'gx',
      model: 'sonnet',
      role: 'backend',
      name: 'remote-peer',
      team: 'default',
      remote_addr: '192.168.1.42',
    })

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const peerRow = (list.agents as Array<Record<string, unknown>>).find(
      row => row.agent_id === peer.agent_id,
    )!
    expect(peerRow.device).toBe('gx')
    expect(Object.prototype.hasOwnProperty.call(peerRow, 'origin')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(peerRow, 'remote_addr')).toBe(false)

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

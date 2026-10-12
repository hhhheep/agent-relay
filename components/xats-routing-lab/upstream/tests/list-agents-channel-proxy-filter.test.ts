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
import { insertAgent } from './helpers/insert-agent.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-list-agents-proxy-filter-'))

const { detectTmuxPaneMock } = vi.hoisted(() => ({
  detectTmuxPaneMock: vi.fn(),
}))

vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: detectTmuxPaneMock,
}))

interface ToolResult { content: Array<{ text: string }> }
async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const result = resp as ToolResult
  return JSON.parse(result.content[0].text)
}

// RED tests for the change `clean-channel-proxy-noise`, task 5.1 (E2E shape via
// in-process MCP client — the project's standard "real user" entry for MCP
// tools, matching list-agents-delivery-projection.test.ts). Until tools.ts
// passes excludeRoles to AgentsRepo.list, list_agents still surfaces every
// __channel_proxy__ row and these expectations fail.
describe('list_agents excludes channel proxy rows', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
  })

  async function setup(): Promise<{
    db: ReturnType<typeof openDb>
    server: McpServer
    client: Client
    transport: ReturnType<typeof InMemoryTransport.createLinkedPair>[0]
    holder: { current: string | undefined }
  }> {
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const dir = tmp()
    cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const server = new McpServer({ name: 'test-server', version: '0.0.0' })
    const holder: { current: string | undefined } = { current: undefined }
    const sessionId = 'session-list-agents-proxy-filter'
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

  it('hides every __channel_proxy__ row from the response, even at scale', async () => {
    const { db, server, client, transport, holder } = await setup()
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' },
    }))
    holder.current = caller.agent_id as string

    // Seed 50 channel proxy rows + the existing business agent (caller).
    for (let i = 0; i < 50; i += 1) {
      insertAgent(db, {
        agent_id: `proxy-${i}`,
        role: '__channel_proxy__',
        name: `channel-proxy-${i}`,
        delivery: { kind: 'claude-channel', channel_session_id: `csid-${i}` },
      })
    }

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const agents = list.agents as Array<Record<string, unknown>>
    // Only the business agent should appear.
    expect(agents).toHaveLength(1)
    expect(agents[0].agent_id).toBe(caller.agent_id)
    expect(agents.find(a => a.role === '__channel_proxy__')).toBeUndefined()
    expect(agents.find(a => typeof a.name === 'string' && (a.name as string).startsWith('channel-proxy-'))).toBeUndefined()

    // Response size sanity check: with 50 proxies hidden, the JSON must be small.
    const wireSize = JSON.stringify(list).length
    expect(wireSize).toBeLessThan(5000)

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('a channel-proxy caller does not see itself or sibling proxies', async () => {
    const { db, server, client, transport, holder } = await setup()
    // Seed three proxy rows; pick one as the caller.
    insertAgent(db, {
      agent_id: 'P1',
      role: '__channel_proxy__',
      name: 'channel-proxy-1',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-1' },
    })
    insertAgent(db, {
      agent_id: 'P2',
      role: '__channel_proxy__',
      name: 'channel-proxy-2',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-2' },
    })
    insertAgent(db, {
      agent_id: 'P3',
      role: '__channel_proxy__',
      name: 'channel-proxy-3',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-3' },
    })
    holder.current = 'P1'

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const agents = list.agents as Array<Record<string, unknown>>
    expect(agents.find(a => a.agent_id === 'P1')).toBeUndefined()
    expect(agents.find(a => a.role === '__channel_proxy__')).toBeUndefined()

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('still includes business agents alongside hidden proxies (mixed team)', async () => {
    const { db, server, client, transport, holder } = await setup()
    const caller = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' },
    }))
    holder.current = caller.agent_id as string

    // Add a sibling business agent + a couple of proxies.
    const repo = new AgentsRepo(db)
    const peer = repo.register({ model: 'sonnet', role: 'frontend', name: 'bob', team: 'default' })
    insertAgent(db, {
      agent_id: 'proxy-x',
      role: '__channel_proxy__',
      name: 'channel-proxy-x',
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-x' },
    })

    const list = await parseTool(await client.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const agents = list.agents as Array<Record<string, unknown>>
    const ids = agents.map(a => a.agent_id).sort()
    expect(ids).toEqual([caller.agent_id, peer.agent_id].sort())

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

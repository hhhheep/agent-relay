import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

interface AgentClient {
  client: Client
  sessionId: string
  close: () => Promise<void>
}

async function makeAgent(baseUrl: string, name: string): Promise<AgentClient> {
  const transport = new StreamableHTTPClientTransport(new URL(baseUrl))
  const client = new Client({ name, version: '0.0.0' }, { capabilities: {} })
  await client.connect(transport)
  const sessionId = transport.sessionId!
  if (!sessionId) throw new Error('expected session id after connect')
  return {
    client,
    sessionId,
    close: async () => { await client.close() }
  }
}

function parseTool(res: unknown): any {
  const r = res as { content: Array<{ type: string; text: string }> }
  return JSON.parse(r.content[0].text)
}

describe('three-agent end-to-end', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('three roles register and broadcast fans out to inboxes', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = `http://${host}:${port}/mcp`

    const agentA = await makeAgent(url, 'opencode')
    const agentB = await makeAgent(url, 'claude-code')
    const agentC = await makeAgent(url, 'codex-cli')

    try {
      // Three distinct sessions (= agent_ids)
      expect(new Set([agentA.sessionId, agentB.sessionId, agentC.sessionId]).size).toBe(3)

      // register_agent for each
      const regA = parseTool(await agentA.client.callTool({
        name: 'register_agent',
        arguments: { agent_type: 'custom', model: 'opus', role: 'backend', name: 'alice' }
      }))
      const regB = parseTool(await agentB.client.callTool({
        name: 'register_agent',
        arguments: { agent_type: 'custom', model: 'sonnet', role: 'frontend', name: 'bob' }
      }))
      const regC = parseTool(await agentC.client.callTool({
        name: 'register_agent',
        arguments: { agent_type: 'custom', model: 'gpt', role: 'qa', name: 'carol' }
      }))
      expect(typeof regA.agent_id).toBe('string')
      expect(typeof regB.agent_id).toBe('string')
      expect(typeof regC.agent_id).toBe('string')
      // agent_id is now decoupled from session id — it's a stable identity UUID.
      expect(regA.agent_id).not.toBe(agentA.sessionId)
      expect(regA.team).toBe('default')

      // list_agents sees all three
      const listA = parseTool(await agentA.client.callTool({ name: 'list_agents', arguments: {} }))
      const ids = listA.agents.map((a: { agent_id: string }) => a.agent_id).sort()
      expect(ids).toEqual([regA.agent_id, regB.agent_id, regC.agent_id].sort())

      // broadcast from A → recipients = [B, C]
      const bcast = parseTool(await agentA.client.callTool({
        name: 'broadcast',
        arguments: { body: 'all-hands' }
      }))
      expect(new Set(bcast.recipients)).toEqual(new Set([regB.agent_id, regC.agent_id]))
      expect(bcast.recipients).not.toContain(regA.agent_id)

      // B and C each see the broadcast in their inbox
      const inboxB = parseTool(await agentB.client.callTool({ name: 'get_inbox', arguments: {} }))
      const inboxC = parseTool(await agentC.client.callTool({ name: 'get_inbox', arguments: {} }))
      expect(inboxB.messages.some((m: { body: string }) => m.body === 'all-hands')).toBe(true)
      expect(inboxC.messages.some((m: { body: string }) => m.body === 'all-hands')).toBe(true)

      const tools = await agentA.client.listTools()
      expect(tools.tools.map(tool => tool.name)).not.toEqual(
        expect.arrayContaining(['task_add', 'task_claim', 'task_complete', 'task_list'])
      )
    } finally {
      await agentA.close()
      await agentB.close()
      await agentC.close()
      await app.close()
    }
  }, 20000)
})

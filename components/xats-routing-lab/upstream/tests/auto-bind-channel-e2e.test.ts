import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { runRegistrationSequence } from '../plugins/cross-agent-teams-channel/src/daemon-client.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-auto-bind-e2e-'))

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

describe('auto-bind-channel-on-register e2e', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    vi.restoreAllMocks()
  })

  it('host registers first, then proxy registers — host delivery becomes claude-channel with proxy csid (6.16)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = `http://${host}:${port}/mcp`

    // host registers first (no proxy row yet). Use a unique ui_pid.
    const uiPid = 900000 + Math.floor(Math.random() * 1000)
    const hostTransport = new StreamableHTTPClientTransport(new URL(url))
    const hostClient = new Client({ name: 'fake-claude-code', version: '0.0.0' })
    await hostClient.connect(hostTransport)
    const resp1 = await parseTool(await hostClient.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'claude-code', model: 'opus-4-7', name: 'alice', team: 'default', ui_pid: uiPid },
    }))
    expect(resp1.agent_id).toBeDefined()
    expect(resp1.channel_session_id).toBeUndefined()

    // proxy registers via the real proxy client; pass claude_ui_pid by exposing it
    // through runRegistrationSequence (which now pulls process.ppid — we can't
    // override it easily from tests, so fall back to a direct register_agent call).
    const proxyTransport = new StreamableHTTPClientTransport(new URL(url))
    const proxyClient = new Client({ name: 'fake-proxy', version: '0.0.0' })
    await proxyClient.connect(proxyTransport)
    const csid = 'csid-' + Math.random().toString(36).slice(2, 10)
    const proxyResp = await parseTool(await proxyClient.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        agent_type_name: 'cross-agent-teams-channel',
        model: 'proxy',
        role: '__channel_proxy__',
        name: `channel-proxy-${uiPid}`,
        team: 'default',
        claude_ui_pid: uiPid,
        delivery: { kind: 'claude-channel', channel_session_id: csid },
      },
    }))
    expect(proxyResp.agent_id).toBeDefined()
    await proxyClient.callTool({
      name: 'subscribe_channel_wake',
      arguments: { channel_session_id: csid },
    })

    // After proxy register, reactive rebind should have updated alice's row
    const listResp = await parseTool(await hostClient.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const agents = listResp.agents as Array<Record<string, unknown>>
    const alice = agents.find(a => a.name === 'alice')!
    expect((alice.delivery as { kind: string }).kind).toBe('claude-channel')
    expect((alice.delivery as { channel_session_id: string }).channel_session_id).toBe(csid)

    await hostTransport.close()
    await hostClient.close()
    await proxyTransport.close()
    await proxyClient.close()
    await app.close()
  }, 30_000)

  it('proxy restart with new csid rebinds all bound hosts (6.17)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = `http://${host}:${port}/mcp`

    const uiPid = 950000 + Math.floor(Math.random() * 1000)
    const hostTransport = new StreamableHTTPClientTransport(new URL(url))
    const hostClient = new Client({ name: 'fake-claude-code', version: '0.0.0' })
    await hostClient.connect(hostTransport)
    await parseTool(await hostClient.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'claude-code', model: 'opus-4-7', name: 'alice', team: 'default', ui_pid: uiPid },
    }))

    // proxy run 1
    const proxy1 = await proxyRegister(url, uiPid, 'csid-run1')
    expect(proxy1.agent_id).toBeDefined()

    // proxy run 2 (new csid, same ppid)
    await proxy1.close()
    const proxy2 = await proxyRegister(url, uiPid, 'csid-run2')
    expect(proxy2.agent_id).toBeDefined()

    const listResp = await parseTool(await hostClient.callTool({
      name: 'list_agents',
      arguments: {},
    }))
    const agents = listResp.agents as Array<Record<string, unknown>>
    const alice = agents.find(a => a.name === 'alice')!
    expect((alice.delivery as { channel_session_id: string }).channel_session_id).toBe('csid-run2')

    await proxy2.close()
    await hostTransport.close()
    await hostClient.close()
    await app.close()
  }, 30_000)
})

async function proxyRegister(
  url: string,
  claudeUiPid: number,
  csid: string
): Promise<{ agent_id: string; close: () => Promise<void> }> {
  const transport = new StreamableHTTPClientTransport(new URL(url))
  const client = new Client({ name: 'fake-proxy', version: '0.0.0' })
  await client.connect(transport)
  const resp = await client.callTool({
    name: 'register_agent',
    arguments: {
      agent_type: 'custom',
      agent_type_name: 'cross-agent-teams-channel',
      model: 'proxy',
      role: '__channel_proxy__',
      name: `channel-proxy-${claudeUiPid}`,
      team: 'default',
      claude_ui_pid: claudeUiPid,
      delivery: { kind: 'claude-channel', channel_session_id: csid },
    },
  }) as { content: Array<{ text: string }> }
  const obj = JSON.parse(resp.content[0].text) as { agent_id: string }
  await client.callTool({
    name: 'subscribe_channel_wake',
    arguments: { channel_session_id: csid },
  })
  void runRegistrationSequence // ensure import not dead-tree-shaken
  return {
    agent_id: obj.agent_id,
    close: async () => {
      try { await client.close() } catch { /* best-effort */ }
      try { await transport.close() } catch { /* best-effort */ }
    }
  }
}

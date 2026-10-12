import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-collapse-self-'))

const { detectTmuxPaneMock } = vi.hoisted(() => ({
  detectTmuxPaneMock: vi.fn(),
}))

vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: detectTmuxPaneMock,
}))

async function setupInMemory() {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  applySchema(db)
  const server = new McpServer({ name: 'test', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-collapse'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
    undefined,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'test-client', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct }
}

describe('collapse-register-self-tools', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
  })

  it('register_agent({agent_type:"codex"}) without thread_id is rejected by Zod schema', async () => {
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)

    const resp = await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'codex', name: 'gpt', model: 'gpt-5' },
    }) as { isError?: boolean; content: Array<{ text: string }> }
    expect(resp.isError).toBe(true)
    const text = resp.content[0].text
    expect(text).toContain('thread_id')
    expect(text).toContain('pre_register_codex_pane')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register_agent({agent_type:"codex"}) with empty-string thread_id is rejected', async () => {
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)

    const resp = await client.callTool({
      name: 'register_agent',
      arguments: { agent_type: 'codex', name: 'gpt', model: 'gpt-5', thread_id: '' },
    }) as { isError?: boolean; content: Array<{ text: string }> }
    expect(resp.isError).toBe(true)
    expect(resp.content[0].text).toContain('thread_id')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register_agent({agent_type:"custom"}) without model succeeds and stores NULL', async () => {
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)

    const res = await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'custom',
        agent_type_name: 'cursor',
        name: 'no-model-cursor',
      },
    })
    const text = (res as { content: Array<{ text: string }> }).content[0].text
    const parsed = JSON.parse(text) as { agent_id?: string; error?: string }
    expect(parsed.error).toBeUndefined()
    expect(parsed.agent_id).toBeDefined()

    const row = db.prepare('SELECT model FROM agents WHERE agent_id = ?').get(parsed.agent_id) as { model: string | null }
    expect(row.model).toBeNull()

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('tools/list does NOT contain register_claude_self or register_codex_self', async () => {
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)

    const list = await client.listTools()
    const names = list.tools.map(t => t.name)
    expect(names).not.toContain('register_claude_self')
    expect(names).not.toContain('register_codex_self')
    expect(names).toContain('register_agent')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register_agent description contains DETECTION block literal substrings', async () => {
    const { dir, db, server, client, transport } = await setupInMemory()
    cleanups.push(dir)

    const list = await client.listTools()
    const tool = list.tools.find(t => t.name === 'register_agent')!
    const d = tool.description!
    expect(d).toContain('CODEX_THREAD_ID')
    // CLAUDECODE OR CLAUDE_CODE_ENTRYPOINT
    expect(d).toMatch(/CLAUDECODE|CLAUDE_CODE_ENTRYPOINT/)
    // opencode env-var probe (sanctioned); must NOT promote the PATH-based probe
    expect(d).toContain('OPENCODE_XATS_BASE_URL')
    expect(d).toContain('agent_type="custom"')
    expect(d).toContain('agent_type_name')
    // Active opencode probe was removed because `command -v opencode` checks
    // what is installed on the host, not what runtime the LLM is inside —
    // a reliable trigger for misclassification (cursor mis-detected as opencode).
    expect(d).not.toContain('command -v opencode')
    // Must NOT name the removed tools
    expect(d).not.toContain('register_claude_self')
    expect(d).not.toContain('register_codex_self')
    // opencode branch must reference base_url argument
    expect(d).toMatch(/agent_type="opencode".*base_url/s)

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('MCP instructions contains register_agent and CODEX_THREAD_ID, omits removed tool names', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'init-test', version: '0.0.0' })
    await c.connect(t)
    void c.getServerVersion()
    // Read instructions via raw initialize since SDK does not expose instructions directly
    await t.close()

    const res = await fetch(`http://${host}:${port}/mcp`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'accept': 'application/json, text/event-stream',
      },
      body: JSON.stringify({
        jsonrpc: '2.0', id: 1, method: 'initialize',
        params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } },
      }),
    })
    const ct = res.headers.get('content-type') || ''
    let body: { result: { instructions: string } }
    if (ct.includes('text/event-stream')) {
      const text = await res.text()
      const lines = text.split(/\n/).filter(l => l.startsWith('data:'))
      const last = lines[lines.length - 1]
      body = JSON.parse(last.slice(5).trim())
    } else {
      body = await res.json() as typeof body
    }
    const ins = body.result.instructions
    expect(ins).toContain('register_agent')
    expect(ins).toContain('CODEX_THREAD_ID')
    expect(ins).toContain('OPENCODE_XATS_BASE_URL')
    expect(ins).toContain('agent_type="custom"')
    expect(ins).toContain('agent_type_name')
    expect(ins).not.toContain('register_claude_self')
    expect(ins).not.toContain('register_codex_self')
    await app.close()
  })
})

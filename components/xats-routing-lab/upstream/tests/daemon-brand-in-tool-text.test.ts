import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

describe('daemon brand in tool text', () => {
  it('no tool description contains legacy ts-agent-teams', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'catm-brand-'))
    const started = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    try {
      const url = new URL(`http://${started.host}:${started.port}/mcp`)
      const transport = new StreamableHTTPClientTransport(url)
      const client = new Client({ name: 'test', version: '0.0.0' })
      await client.connect(transport)
      try {
        const list = await client.listTools()
        for (const t of list.tools) {
          expect(t.description ?? '').not.toContain('ts-agent-teams')
        }
        const resp = await client.callTool({
          name: 'register_agent',
          arguments: { agent_type: 'custom', model: 'test', role: 'tester', name: 'hint-probe', team: 'default' }
        })
        const text = (resp as { content: Array<{ text: string }> }).content[0].text
        expect(text).not.toContain('ts-agent-teams')
      } finally {
        await transport.close()
      }
    } finally {
      await started.app.close()
      rmSync(dir, { recursive: true, force: true })
    }
  }, 15_000)
})

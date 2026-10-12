import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

describe('daemon MCP server identity', () => {
  it('reports cross-agent-teams-mcp during initialize', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'catm-srv-'))
    const started = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    try {
      const url = `http://${started.host}:${started.port}/mcp`
      const client = new Client({ name: 'test', version: '0.0.0' })
      const transport = new StreamableHTTPClientTransport(new URL(url))
      await client.connect(transport)
      const info = client.getServerVersion()
      expect(info?.name).toBe('cross-agent-teams-mcp')
      await client.close()
    } finally {
      await started.app.close()
      rmSync(dir, { recursive: true, force: true })
    }
  }, 15_000)
})

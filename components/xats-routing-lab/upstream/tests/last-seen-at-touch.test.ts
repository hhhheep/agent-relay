import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import Database from 'better-sqlite3'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

function readLastSeen(dbPath: string, agent_id: string): string {
  const db = new Database(dbPath, { readonly: true })
  const row = db.prepare('SELECT last_seen_at FROM agents WHERE agent_id=?').get(agent_id) as { last_seen_at: string }
  db.close()
  return row.last_seen_at
}

function backdateLastSeen(dbPath: string, agent_id: string, iso: string): void {
  const db = new Database(dbPath)
  db.prepare('UPDATE agents SET last_seen_at=? WHERE agent_id=?').run(iso, agent_id)
  db.close()
}

describe('last_seen_at bumped on every tool invocation', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('list_agents invocation updates last_seen_at within the last 2 seconds', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = `http://${host}:${port}/mcp`
    const transport = new StreamableHTTPClientTransport(new URL(url))
    const client = new Client({ name: 'probe', version: '0.0.0' }, { capabilities: {} })
    await client.connect(transport)
    try {
      const regResp = await client.callTool({ name: 'register_agent', arguments: { agent_type: 'custom', name: 'tester-4', model: 'm', role: 'r' } })
      const reg = JSON.parse((regResp.content as Array<{ text: string }>)[0].text) as { agent_id: string }
      const agentId = reg.agent_id
      const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString()
      backdateLastSeen(dbPath, agentId, oneHourAgo)
      const before = readLastSeen(dbPath, agentId)
      expect(before).toBe(oneHourAgo)
      await client.callTool({ name: 'list_agents', arguments: {} })
      const after = readLastSeen(dbPath, agentId)
      const ageMs = Date.now() - new Date(after).getTime()
      expect(ageMs).toBeLessThan(2000)
    } finally {
      await client.close()
      await app.close()
    }
  }, 15000)
})

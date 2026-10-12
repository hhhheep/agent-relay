import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-list-agents-desc-'))

interface ToolInfo { name: string; description?: string }

async function listAgentsDesc(dir: string): Promise<string> {
  const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
  const url = new URL(`http://${host}:${port}/mcp`)
  const t = new StreamableHTTPClientTransport(url)
  const c = new Client({ name: 'test', version: '0' })
  await c.connect(t)
  const resp = await c.listTools()
  const tool = (resp.tools as unknown as ToolInfo[]).find(x => x.name === 'list_agents')
  await t.close()
  await app.close()
  expect(tool).toBeDefined()
  return tool!.description ?? ''
}

describe('list_agents description: forbids pre-flight verification', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  it('declares caller-team scope', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await listAgentsDesc(dir)
    expect(desc).toMatch(/caller'?s team|caller-team only/i)
  })

  it('declares CANNOT see cross-team agents in jussive prose', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await listAgentsDesc(dir)
    // Require CANNOT and cross-team to co-occur in the same sentence.
    const sentences = desc.split(/(?<=[.!?])\s+/)
    const hit = sentences.some(s => /cannot/i.test(s) && /cross-team/i.test(s))
    expect(hit).toBe(true)
  })

  it('forbids pre-flight verification before send_message and references unknown_recipient', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await listAgentsDesc(dir)
    expect(desc).toContain('send_message')
    // DO NOT (or MUST NOT) together with `pre` in the same sentence.
    const sentences = desc.split(/(?<=[.!?])\s+/)
    const directive = sentences.some(
      s => /do not|must not/i.test(s) && /\bpre/i.test(s)
    )
    expect(directive).toBe(true)
    expect(desc).toContain('unknown_recipient')
  })
})

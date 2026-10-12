import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-desc-'))

describe('send_message description', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  it('mentions to_agent_name, send_message_by_id pointer, and to_team guardrail', async () => {
    const dir = tmp(); dirs.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0' })
    await c.connect(t)
    const tools = await c.listTools()
    const sm = tools.tools.find((x) => x.name === 'send_message')
    expect(sm).toBeDefined()
    const desc = sm!.description ?? ''
    expect(desc).toMatch(/to_agent_name/)
    expect(desc).toMatch(/send_message_by_id/)
    expect(desc).toMatch(/to_team/)
    await t.close(); await app.close()
  })

  it('mentions need_reply default and no-reply opt-out', async () => {
    const dir = tmp(); dirs.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0' })
    await c.connect(t)
    const tools = await c.listTools()
    const sm = tools.tools.find((x) => x.name === 'send_message')
    expect(sm).toBeDefined()
    const desc = sm!.description ?? ''
    expect(desc).toMatch(/need_reply/)
    expect(desc).toMatch(/need_reply:false/)
    expect(desc).toMatch(/no-response-needed|no reply/i)
    await t.close(); await app.close()
  })

  async function sendMessageDesc(dir: string): Promise<string> {
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0' })
    await c.connect(t)
    const tools = await c.listTools()
    const sm = tools.tools.find((x) => x.name === 'send_message')
    await t.close(); await app.close()
    expect(sm).toBeDefined()
    return sm!.description ?? ''
  }

  it('forbids list_agents pre-verification with DO NOT + pre in the same sentence as list_agents', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await sendMessageDesc(dir)
    expect(desc).toContain('list_agents')
    const sentences = desc.split(/(?<=[.!?])\s+/)
    const directive = sentences.some(
      s => /do not|must not/i.test(s) && /\bpre/i.test(s) && /list_agents/.test(s)
    )
    expect(directive).toBe(true)
  })

  it('references unknown_recipient as the canonical miss signal', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await sendMessageDesc(dir)
    expect(desc).toContain('unknown_recipient')
  })

  it('makes clear the no-pre-verification rule applies to both same-team and cross-team sends', async () => {
    const dir = tmp(); dirs.push(dir)
    const desc = await sendMessageDesc(dir)
    expect(desc).toMatch(/same-team/i)
    expect(desc).toMatch(/cross-team/i)
  })
})

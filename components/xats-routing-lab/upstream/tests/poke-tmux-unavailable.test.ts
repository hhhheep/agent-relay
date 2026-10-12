import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { startServer } from '../src/daemon/server.js'
import { _setTmuxAvailableForTest, _resetTmuxAvailableCache } from '../src/daemon/tmux-cli.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-poke-no-tmux-hidden-'))

describe('poke public tool with tmux unavailable', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    _resetTmuxAvailableCache()
  })

  it('does not expose manual poke even when tmux is unavailable', async () => {
    _setTmuxAvailableForTest(false)

    const dir = tmp()
    cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const t = new StreamableHTTPClientTransport(new URL(`http://${host}:${port}/mcp`))
    const c = new Client({ name: 'test', version: '0.0.0' })
    await c.connect(t)

    const tools = await c.listTools()
    expect(tools.tools.find(tool => tool.name === 'poke')).toBeUndefined()

    await t.terminateSession()
    await c.close()
    await app.close()
  })
})

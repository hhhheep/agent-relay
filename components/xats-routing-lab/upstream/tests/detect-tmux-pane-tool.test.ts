import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'

const detectTmuxPaneMock = vi.fn()

vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: detectTmuxPaneMock,
}))

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-detect-pane-tool-'))

describe('detect_tmux_pane tool', () => {
  const cleanups: string[] = []

  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
  })

  it('is exposed and returns the detector result', async () => {
    detectTmuxPaneMock.mockResolvedValue({
      ok: true,
      pane: {
        pane_id: '%1902',
        session_name: 's1',
        window_index: 0,
        pane_index: 3,
        active: true,
        tty: 'ttys026',
        current_path: '/Users/me/project',
        current_command: 'codex-aarch64-a',
        title: 'project',
        matched_processes: ['32657 99672 S+ codex --remote ws://127.0.0.1:8799'],
        score: 79,
      },
      candidates: [],
    })

    const { startServer } = await import('../src/daemon/server.js')
    const dir = tmp()
    cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = new URL(`http://${host}:${port}/mcp`)
    const t = new StreamableHTTPClientTransport(url)
    const c = new Client({ name: 'test', version: '0.0.0' })
    await c.connect(t)

    const tools = await c.listTools()
    const tool = tools.tools.find(x => x.name === 'detect_tmux_pane')
    expect(tool).toBeDefined()
    expect(tool!.inputSchema).toMatchObject({
      type: 'object',
      properties: expect.objectContaining({
        agent: expect.anything(),
        cwd: expect.anything(),
        tty: expect.anything(),
        title_contains: expect.anything(),
        process_pattern: expect.anything(),
      }),
      required: expect.arrayContaining(['agent']),
    })

    const resp = await c.callTool({
      name: 'detect_tmux_pane',
      arguments: { agent: 'codex', cwd: '/Users/me/project' },
    })
    const result = JSON.parse((resp.content as Array<{ text: string }>)[0].text) as {
      ok: boolean
      pane: { pane_id: string }
    }
    expect(result).toMatchObject({
      ok: true,
      pane: { pane_id: '%1902' },
    })
    expect(detectTmuxPaneMock).toHaveBeenCalledWith({
      agent: 'codex',
      cwd: '/Users/me/project',
      tty: undefined,
      title_contains: undefined,
      process_pattern: undefined,
    })

    await t.terminateSession()
    await c.close()
    await app.close()
  })
})

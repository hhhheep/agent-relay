import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawn, execFileSync, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createServer, type Server as HttpServer } from 'node:http'
import { randomUUID } from 'node:crypto'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-proxy-cli-'))

async function buildPluginOnce(): Promise<string> {
  const pluginRoot = resolve(__dirname, '..')
  const cliJs = join(pluginRoot, 'dist', 'cli.js')
  if (!existsSync(cliJs)) {
    execFileSync('pnpm', ['exec', 'tsc', '-p', 'tsconfig.build.json'], {
      cwd: pluginRoot,
      stdio: 'inherit'
    })
  }
  return cliJs
}

interface RecordedCall {
  name: string
  args: Record<string, unknown>
}

interface FakeDaemon {
  server: HttpServer
  port: number
  calls: RecordedCall[]
  close: () => Promise<void>
}

// Minimal JSON-RPC over HTTP fake daemon. Supports the three calls the proxy
// makes on startup (initialize, register_agent, subscribe_channel_wake) plus
// echo (for the heartbeat). No bind_channel — the proxy no longer calls it.
async function startFakeDaemon(): Promise<FakeDaemon> {
  const calls: RecordedCall[] = []

  const server = createServer((req, res) => {
    if (req.method !== 'POST') {
      res.statusCode = 405
      res.end()
      return
    }
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8')
      let msg: Record<string, unknown>
      try { msg = JSON.parse(raw) as Record<string, unknown> } catch {
        res.statusCode = 400
        res.end('bad json')
        return
      }

      const method = msg.method as string | undefined
      const id = msg.id
      const params = (msg.params as Record<string, unknown> | undefined) ?? {}

      const writeJson = (body: Record<string, unknown>, extraHeaders: Record<string, string> = {}) => {
        res.setHeader('content-type', 'application/json')
        for (const [k, v] of Object.entries(extraHeaders)) res.setHeader(k, v)
        res.statusCode = 200
        res.end(JSON.stringify(body))
      }

      const toolResult = (obj: unknown) => writeJson({
        jsonrpc: '2.0',
        id,
        result: { content: [{ type: 'text', text: JSON.stringify(obj) }] }
      })

      if (method === 'initialize') {
        const newSid = randomUUID()
        writeJson({
          jsonrpc: '2.0',
          id,
          result: {
            protocolVersion: (params.protocolVersion as string) ?? '2024-11-05',
            capabilities: {},
            serverInfo: { name: 'fake-daemon', version: '0.0.0' }
          }
        }, { 'mcp-session-id': newSid })
        return
      }

      if (method === 'notifications/initialized') {
        res.statusCode = 202
        res.end()
        return
      }

      if (method === 'tools/call') {
        const name = params.name as string
        const args = (params.arguments as Record<string, unknown>) ?? {}
        calls.push({ name, args })
        if (name === 'register_agent') {
          toolResult({ agent_id: randomUUID() })
          return
        }
        if (name === 'subscribe_channel_wake') {
          toolResult({ ok: true })
          return
        }
        if (name === 'echo') {
          toolResult({ ok: true, echoed: args.msg ?? null })
          return
        }
        writeJson({ jsonrpc: '2.0', id, error: { code: -32601, message: `unknown tool ${name}` } })
        return
      }

      if (method === 'ping') {
        writeJson({ jsonrpc: '2.0', id, result: {} })
        return
      }

      writeJson({ jsonrpc: '2.0', id, result: {} })
    })
  })
  await new Promise<void>(r => server.listen(0, '127.0.0.1', () => r()))
  const addr = server.address()
  const port = addr && typeof addr === 'object' ? addr.port : 0
  return {
    server,
    port,
    calls,
    close: () => new Promise<void>((r) => server.close(() => r()))
  }
}

describe('proxy CLI entrypoint (self-binding)', () => {
  const cleanups: string[] = []
  const killers: ChildProcessWithoutNullStreams[] = []
  const daemons: FakeDaemon[] = []
  afterEach(async () => {
    killers.forEach(p => { try { p.kill('SIGTERM') } catch { /* best-effort */ } })
    killers.length = 0
    for (const d of daemons) { try { await d.close() } catch { /* ignore */ } }
    daemons.length = 0
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('runs register_agent -> subscribe_channel_wake (no bind_channel) when spawned with only --daemon-url', async () => {
    const cliJs = await buildPluginOnce()
    const daemon = await startFakeDaemon(); daemons.push(daemon)
    const url = `http://127.0.0.1:${daemon.port}/mcp`

    const proc = spawn(process.execPath, [cliJs, '--daemon-url', url], {
      env: { ...process.env },
      stdio: ['pipe', 'pipe', 'pipe']
    })
    killers.push(proc)
    const stderrChunks: Buffer[] = []
    proc.stderr.on('data', (c: Buffer) => stderrChunks.push(c))

    const deadline = Date.now() + 10_000
    const seen = () => daemon.calls.map(c => c.name)
    while (Date.now() < deadline) {
      const names = seen()
      if (names.includes('register_agent') && names.includes('subscribe_channel_wake')) break
      await new Promise(r => setTimeout(r, 50))
    }
    const order = daemon.calls.map(c => c.name)
    const idxReg = order.indexOf('register_agent')
    const idxSub = order.indexOf('subscribe_channel_wake')
    expect(idxReg, `stderr=${Buffer.concat(stderrChunks).toString()}`).toBeGreaterThanOrEqual(0)
    expect(idxSub).toBeGreaterThan(idxReg)
    // bind_channel must NOT be called by the proxy — Claude does self-binding.
    expect(order.includes('bind_channel')).toBe(false)

    // subscribe_channel_wake must carry a non-empty channel_session_id.
    const subCall = daemon.calls.find(c => c.name === 'subscribe_channel_wake')!
    expect(typeof subCall.args.channel_session_id).toBe('string')
    expect((subCall.args.channel_session_id as string).length).toBeGreaterThan(0)

    // register_agent must carry claude_ui_pid and delivery={claude-channel,<csid>}
    const regCall = daemon.calls.find(c => c.name === 'register_agent')!
    expect(typeof regCall.args.claude_ui_pid).toBe('number')
    expect(regCall.args.claude_ui_pid).toBeGreaterThan(0)
    const delivery = regCall.args.delivery as { kind: string; channel_session_id: string } | undefined
    expect(delivery?.kind).toBe('claude-channel')
    expect(delivery?.channel_session_id).toBe(subCall.args.channel_session_id)

    // Close stdio — proxy must exit cleanly.
    proc.stdin.end()
    const exitCode: number | null = await new Promise((resolvePromise) => {
      const t = setTimeout(() => { try { proc.kill('SIGTERM') } catch { /* ignore */ } }, 5000)
      proc.once('exit', (code) => { clearTimeout(t); resolvePromise(code) })
    })
    expect([0, null]).toContain(exitCode)
  }, 60_000)

  it('generates a fresh channel_session_id on each startup (no persistence file written)', async () => {
    const cliJs = await buildPluginOnce()
    const cacheDir = tmp(); cleanups.push(cacheDir)
    const daemon1 = await startFakeDaemon(); daemons.push(daemon1)

    const spawnAndCapture = async (port: number, daemon: FakeDaemon): Promise<string> => {
      const proc = spawn(process.execPath, [cliJs, '--daemon-url', `http://127.0.0.1:${port}/mcp`], {
        env: { ...process.env, XDG_CACHE_HOME: cacheDir },
        stdio: ['pipe', 'pipe', 'pipe']
      })
      killers.push(proc)
      const deadline = Date.now() + 10_000
      while (Date.now() < deadline) {
        const sub = daemon.calls.find(c => c.name === 'subscribe_channel_wake')
        if (sub) {
          const csid = sub.args.channel_session_id as string
          try { proc.kill('SIGTERM') } catch { /* ignore */ }
          await new Promise<void>((r) => {
            const to = setTimeout(() => { try { proc.kill('SIGKILL') } catch { /* ignore */ } ; r() }, 3000)
            proc.once('exit', () => { clearTimeout(to); r() })
          })
          return csid
        }
        await new Promise(r => setTimeout(r, 50))
      }
      proc.kill('SIGKILL')
      throw new Error('timeout waiting for subscribe_channel_wake')
    }

    const csid1 = await spawnAndCapture(daemon1.port, daemon1)

    const daemon2 = await startFakeDaemon(); daemons.push(daemon2)
    const csid2 = await spawnAndCapture(daemon2.port, daemon2)

    expect(csid1).not.toBe(csid2)
    // No persistence file should exist.
    expect(existsSync(join(cacheDir, 'ts-agent-teams-channel'))).toBe(false)
  }, 60_000)

  it('exits non-zero with diagnostic when --daemon-url is missing', async () => {
    const cliJs = await buildPluginOnce()
    const proc = spawn(process.execPath, [cliJs], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, CROSS_AGENT_TEAMS_MCP_DAEMON_URL: '' }
    })
    killers.push(proc)
    const stderrChunks: Buffer[] = []
    proc.stderr.on('data', (c: Buffer) => stderrChunks.push(c))
    const exitCode: number | null = await new Promise((resolvePromise) => {
      const t = setTimeout(() => { try { proc.kill('SIGKILL') } catch { /* ignore */ } }, 5000)
      proc.once('exit', (code) => { clearTimeout(t); resolvePromise(code) })
    })
    expect(exitCode).not.toBe(0)
    const stderr = Buffer.concat(stderrChunks).toString()
    expect(stderr).toMatch(/daemon-url|daemon_url|CROSS_AGENT_TEAMS_MCP_DAEMON_URL/i)
  }, 20_000)
})

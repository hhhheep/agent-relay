import { describe, it, expect } from 'vitest'
import { spawn } from 'node:child_process'
import { execSync } from 'node:child_process'
import { resolve } from 'node:path'
import { existsSync } from 'node:fs'

const DIST_BIN = resolve(__dirname, '..', 'dist', 'channel-cli.js')

function listChildPids(parentPid: number): number[] {
  try {
    const out = execSync(`pgrep -P ${parentPid}`, { encoding: 'utf8' }).trim()
    if (!out) return []
    return out.split('\n').map(s => parseInt(s, 10)).filter(n => Number.isFinite(n))
  } catch {
    return []
  }
}

describe('channel-cli runtime smoke against unreachable daemon', () => {
  it('does not hang and does not spawn any child node process', async () => {
    expect(existsSync(DIST_BIN), 'dist/channel-cli.js must be built before running this test').toBe(true)

    const proc = spawn('node', [DIST_BIN, '--daemon-url', 'http://127.0.0.1:1'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    })

    const exitPromise = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((res) => {
      proc.once('exit', (code, signal) => res({ code, signal }))
    })

    // Send a minimal MCP `initialize` request so the host-facing transport sees real traffic.
    const initRequest = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'smoke-test', version: '0.0.0' },
      },
    }) + '\n'
    proc.stdin.write(initRequest)

    // Bounded budget: give the proxy time to attempt registration against the unreachable port.
    await new Promise(r => setTimeout(r, 1500))

    // Snapshot any child processes spawned under the proxy.
    const children = listChildPids(proc.pid!)

    // Close stdin to signal end-of-input; this drives stdioTransport.onclose → shutdown.
    proc.stdin.end()

    // If it does not exit on its own within the budget, send SIGTERM to avoid hanging.
    const timeoutHandle = setTimeout(() => {
      try { proc.kill('SIGTERM') } catch { /* best-effort */ }
    }, 3000)

    const result = await exitPromise
    clearTimeout(timeoutHandle)

    // The CLI must NOT silently spawn a daemon under itself.
    expect(children, `proxy spawned unexpected children: ${children.join(',')}`).toEqual([])

    // Exits non-zero: either a non-zero exit code, or terminated by signal.
    const nonZero = (result.code !== null && result.code !== 0) || result.signal !== null
    expect(nonZero, `proxy exited cleanly with code 0 — expected non-zero exit when daemon unreachable (code=${result.code}, signal=${result.signal})`).toBe(true)
  }, 10000)
})

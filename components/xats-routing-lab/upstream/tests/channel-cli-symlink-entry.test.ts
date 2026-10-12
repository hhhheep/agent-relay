import { describe, expect, it } from 'vitest'
import { mkdtempSync, symlinkSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'

// Regression: when `dist/channel-cli.js` is launched via an npm `.bin`
// symlink (the path used by `npx -p cross-agent-teams-mcp cross-agent-teams-channel`
// and by `npm install -g`), the previous `import.meta.url === file://${argv[1]}`
// guard never matched and `main()` silently never ran — the proxy exited
// immediately and Claude Code reported "MCP error -32000: Connection closed".
//
// The fixed guard realpath-resolves argv[1] before comparison.  This test
// proves that when invoked through a symlink, the proxy actually starts and
// blocks on stdin (as a real MCP stdio server would), instead of returning
// from module load.
describe('channel-cli entry guard tolerates symlink launches', () => {
  it('main() runs when launched via a symlinked path', async () => {
    const dist = resolve(__dirname, '..', 'dist', 'channel-cli.js')
    const dir = mkdtempSync(join(tmpdir(), 'xats-channel-symlink-'))
    const link = join(dir, 'cross-agent-teams-channel')
    symlinkSync(dist, link)

    try {
      // Launch via the symlink with a guaranteed-unreachable daemon.  If main
      // runs, the proxy enters runReconnectingProxy and stays alive briefly
      // (it does not exit on module load).  We give it 1.5s to demonstrate
      // it is alive, then SIGTERM and capture the exit.
      const child = spawn('node', [link, '--daemon-url', 'http://127.0.0.1:1'], {
        stdio: ['pipe', 'pipe', 'pipe']
      })

      let exitedEarly = false
      const earlyExit = new Promise<void>((res) => {
        child.on('exit', () => {
          exitedEarly = true
          res()
        })
      })

      const aliveCheck = new Promise<void>((res) => setTimeout(res, 1500))

      await Promise.race([earlyExit, aliveCheck])

      // If main() never ran, the process exits immediately on module load
      // (exit code 0, no work done).  We expect it to still be alive after
      // 1.5s — the reconnecting proxy is retrying against the dead daemon.
      expect(exitedEarly).toBe(false)

      // Clean up.
      child.kill('SIGTERM')
      await new Promise<void>((res) => child.on('exit', () => res()))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }, 10_000)
})

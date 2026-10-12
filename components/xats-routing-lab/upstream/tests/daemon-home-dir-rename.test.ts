import { describe, it, expect } from 'vitest'
import { spawn } from 'node:child_process'
import { mkdtempSync, existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

describe('daemon home dir honors CROSS_AGENT_TEAMS_MCP_HOME', () => {
  it('writes pid file to env-specified home and cleans up on shutdown', async () => {
    const home = mkdtempSync(join(tmpdir(), 'catm-home-'))
    const pidPath = join(home, 'daemon.pid')
    const proc = spawn(
      process.execPath,
      ['dist/cli.js', 'daemon', '--port', '0'],
      {
        env: { ...process.env, CROSS_AGENT_TEAMS_MCP_HOME: home, TS_AGENT_TEAMS_HOME: '' },
        stdio: ['ignore', 'pipe', 'pipe']
      }
    )
    try {
      const deadline = Date.now() + 5000
      while (Date.now() < deadline && !existsSync(pidPath)) {
        await new Promise(r => setTimeout(r, 50))
      }
      expect(existsSync(pidPath)).toBe(true)
    } finally {
      proc.kill('SIGTERM')
      await new Promise<void>(r => { proc.once('exit', () => r()) })
      rmSync(home, { recursive: true, force: true })
    }
  }, 15_000)
})

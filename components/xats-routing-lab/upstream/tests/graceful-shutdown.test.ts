import { describe, it, expect, afterEach } from 'vitest'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('graceful shutdown', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('SIGTERM triggers exit 0 and removes pid file', async () => {
    const dir = tmp(); cleanups.push(dir)
    const pidPath = join(dir, 'daemon.pid')
    const dbPath = join(dir, 'data.db')
    const child = spawn('node', ['--import', 'tsx/esm', 'src/cli.ts', 'daemon', '--port', '0',
      '--pid-file', pidPath, '--db', dbPath], { stdio: ['ignore', 'pipe', 'pipe'] })
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('daemon did not start in 5s')), 5000)
      child.stdout.on('data', (b: Buffer) => {
        if (b.toString().includes('listening')) { clearTimeout(t); resolve() }
      })
      child.on('exit', (code) => { clearTimeout(t); reject(new Error(`child exited early code=${code}`)) })
    })
    expect(existsSync(pidPath)).toBe(true)
    child.kill('SIGTERM')
    const exitCode = await new Promise<number>(resolve => child.once('exit', (c) => resolve(c ?? -1)))
    expect(exitCode).toBe(0)
    expect(existsSync(pidPath)).toBe(false)
  }, 15000)
})

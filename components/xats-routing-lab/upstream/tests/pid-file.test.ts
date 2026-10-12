import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { acquirePidFile, releasePidFile } from '../src/daemon/pid.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('pid file', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('fresh acquire writes pid and port', () => {
    const dir = tmp(); cleanups.push(dir)
    const path = join(dir, 'daemon.pid')
    const r = acquirePidFile(path, 9100)
    expect(r.ok).toBe(true)
    const parsed = JSON.parse(readFileSync(path, 'utf8'))
    expect(parsed.pid).toBe(process.pid)
    expect(parsed.port).toBe(9100)
    releasePidFile(path)
  })

  it('stale pid file is overwritten', () => {
    const dir = tmp(); cleanups.push(dir)
    const path = join(dir, 'daemon.pid')
    writeFileSync(path, JSON.stringify({ pid: 999999, port: 1 }))
    const r = acquirePidFile(path, 9100)
    expect(r.ok).toBe(true)
    expect(JSON.parse(readFileSync(path, 'utf8')).pid).toBe(process.pid)
    releasePidFile(path)
  })

  it('live pid file refuses', () => {
    const dir = tmp(); cleanups.push(dir)
    const path = join(dir, 'daemon.pid')
    // Use a different pid: fork a child or use a known-alive pid. Since process.pid is this same pid, use pid 1 (init) which is always alive on unix.
    writeFileSync(path, JSON.stringify({ pid: 1, port: 1 }))
    const r = acquirePidFile(path, 9100)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.reason).toBe('already_running')
  })

  it('releasePidFile removes the file', () => {
    const dir = tmp(); cleanups.push(dir)
    const path = join(dir, 'daemon.pid')
    acquirePidFile(path, 9100)
    releasePidFile(path)
    expect(existsSync(path)).toBe(false)
  })
})

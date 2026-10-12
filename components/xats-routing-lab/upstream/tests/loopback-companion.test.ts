import { describe, expect, it, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-loopback-companion-'))

interface FetchedHealth { ok: boolean; uptime_seconds: number }

async function getHealth(host: string, port: number): Promise<FetchedHealth> {
  const hostPart = host.includes(':') ? `[${host}]` : host
  const r = await fetch(`http://${hostPart}:${port}/health`)
  return await r.json() as FetchedHealth
}

describe('loopback companion listener', () => {
  const dirs: string[] = []
  afterEach(() => {
    for (const d of dirs) rmSync(d, { recursive: true, force: true })
    dirs.length = 0
  })

  it('adds a 127.0.0.1 listener on the same port when primary host is non-loopback-covering', async () => {
    const dir = tmp(); dirs.push(dir)
    // ::1 is a loopback-class address but distinct from 127.0.0.1, so
    // binding to it does NOT also serve 127.0.0.1 — exactly the case the
    // companion listener exists to cover.
    const started = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      host: '::1',
    })
    try {
      expect(started.host).toBe('::1')
      expect(started.loopbackCompanion).toBeDefined()
      const primary = await getHealth('::1', started.port)
      const loopback = await getHealth('127.0.0.1', started.port)
      expect(primary.ok).toBe(true)
      expect(loopback.ok).toBe(true)
    } finally {
      await started.app.close()
    }
  }, 15000)

  it('skips the companion when primary host already covers 127.0.0.1', async () => {
    const dir = tmp(); dirs.push(dir)
    const started = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      host: '127.0.0.1',
    })
    try {
      expect(started.loopbackCompanion).toBeUndefined()
    } finally {
      await started.app.close()
    }
  }, 15000)

  it('honors loopbackCompanion=false to disable the companion', async () => {
    const dir = tmp(); dirs.push(dir)
    const started = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      host: '::1',
      loopbackCompanion: false,
    })
    try {
      expect(started.loopbackCompanion).toBeUndefined()
    } finally {
      await started.app.close()
    }
  }, 15000)

  it('fails fatally when 127.0.0.1:<port> is already taken', async () => {
    const dir = tmp(); dirs.push(dir)
    // Hold 127.0.0.1:<port> via a sacrificial listener so the companion bind collides.
    const blocker = createServer()
    await new Promise<void>((resolve, reject) => {
      blocker.once('error', reject)
      blocker.listen(0, '127.0.0.1', () => resolve())
    })
    const port = (blocker.address() as AddressInfo).port
    try {
      await expect(startServer({
        dbPath: join(dir, 'data.db'),
        port,
        host: '::1',
      })).rejects.toThrow(/loopback_companion_bind_failed/)
    } finally {
      await new Promise<void>((resolve) => blocker.close(() => resolve()))
    }
  }, 15000)
})

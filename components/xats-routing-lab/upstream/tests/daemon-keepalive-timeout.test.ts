import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-ka-'))

describe('keep-alive timeout', () => {
  const cleanups: string[] = []
  const savedEnv = { ...process.env }
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0
    for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k]
    for (const [k, v] of Object.entries(savedEnv)) process.env[k] = v
  })

  it('defaults to 120000 when env unset', async () => {
    delete process.env.KEEP_ALIVE_TIMEOUT_MS
    const dir = tmp(); cleanups.push(dir)
    const { app } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    expect(app.server.keepAliveTimeout).toBe(120000)
    expect(app.server.headersTimeout).toBe(121000)
    await app.close()
  })

  it('honors env override', async () => {
    process.env.KEEP_ALIVE_TIMEOUT_MS = '60000'
    const dir = tmp(); cleanups.push(dir)
    const { app } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    expect(app.server.keepAliveTimeout).toBe(60000)
    expect(app.server.headersTimeout).toBe(61000)
    await app.close()
  })

  it('invalid env falls back to default', async () => {
    process.env.KEEP_ALIVE_TIMEOUT_MS = 'not-a-number'
    const dir = tmp(); cleanups.push(dir)
    const { app } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    expect(app.server.keepAliveTimeout).toBe(120000)
    await app.close()
  })
})

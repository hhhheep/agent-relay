import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('bind-localhost', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('binds only to 127.0.0.1', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    expect(host).toBe('127.0.0.1')
    const addr = app.server.address()
    expect(addr && typeof addr === 'object').toBe(true)
    if (addr && typeof addr === 'object') {
      expect(addr.address).toBe('127.0.0.1')
      expect(addr.port).toBe(port)
    }
    await app.close()
  })
})

import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('bearer auth', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('allows requests when no token configured', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db') })
    const res = await app.inject({ method: 'POST', url: '/mcp', payload: { jsonrpc: '2.0', id: 1, method: 'ping' } })
    expect(res.statusCode).not.toBe(401)
    await app.close()
  })

  it('accepts matching Authorization header', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db'), token: 's3cret' })
    const res = await app.inject({
      method: 'POST', url: '/mcp',
      headers: { authorization: 'Bearer s3cret' },
      payload: { jsonrpc: '2.0', id: 1, method: 'ping' }
    })
    expect(res.statusCode).not.toBe(401)
    await app.close()
  })

  it('returns 401 invalid_token on mismatch', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db'), token: 's3cret' })
    const res = await app.inject({
      method: 'POST', url: '/mcp',
      headers: { authorization: 'Bearer wrong' },
      payload: { jsonrpc: '2.0', id: 1, method: 'ping' }
    })
    expect(res.statusCode).toBe(401)
    // Body must not be a bare {error} object that would poison a strict client.
    expect(res.body).toBe('')
    await app.close()
  })

  it('health endpoint is exempt from auth', async () => {
    const dir = tmp(); cleanups.push(dir)
    const app = await buildServer({ dbPath: join(dir, 'data.db'), token: 's3cret' })
    const res = await app.inject({ method: 'GET', url: '/health' })
    expect(res.statusCode).toBe(200)
    await app.close()
  })
})

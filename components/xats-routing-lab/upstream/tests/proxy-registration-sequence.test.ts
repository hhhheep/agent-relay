import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'
import { runRegistrationSequence } from '../plugins/cross-agent-teams-channel/src/daemon-client.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-proxy-reg-'))

describe('proxy registration sequence (self-binding)', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('runs register_agent → subscribe_channel_wake (no bind_channel)', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({ dbPath: join(dir, 'data.db'), port: 0 })
    const url = `http://${host}:${port}/mcp`

    const seq = await runRegistrationSequence({
      daemonUrl: url,
      channel_session_id: 'csid-abc',
      backoffInitialMs: 10,
      backoffMaxMs: 50
    })
    expect(seq.order).toEqual(['register_agent', 'subscribe_channel_wake'])
    expect(seq.lastSubscribeResult).toEqual({ ok: true })

    await seq.close()
    await app.close()
  }, 20000)

  it('succeeds on loopback when the daemon has a custom --device and the proxy passes no device', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      localDevice: 'customlabel'
    })
    const url = `http://${host}:${port}/mcp`

    // No `device` field in config → daemon-client omits the field →
    // daemon's loopback auto-fill resolves device to `customlabel`.
    const seq = await runRegistrationSequence({
      daemonUrl: url,
      channel_session_id: 'csid-xyz',
      backoffInitialMs: 10,
      backoffMaxMs: 50
    })
    expect(seq.order).toEqual(['register_agent', 'subscribe_channel_wake'])
    expect(seq.lastSubscribeResult).toEqual({ ok: true })

    await seq.close()
    await app.close()
  }, 20000)

  it('never puts an identity key on the proxy own row', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app, port, host } = await startServer({ dbPath, port: 0 })
    const url = `http://${host}:${port}/mcp`

    // The key belongs to the host agent's identity, not to the proxy — even
    // with one exported into the proxy's environment.
    const previous = process.env.XATS_IDENTITY_KEY
    process.env.XATS_IDENTITY_KEY = 'abc-123'
    try {
      const seq = await runRegistrationSequence({
        daemonUrl: url,
        channel_session_id: 'csid-key',
        backoffInitialMs: 10,
        backoffMaxMs: 50
      })
      await seq.close()
    } finally {
      if (previous === undefined) delete process.env.XATS_IDENTITY_KEY
      else process.env.XATS_IDENTITY_KEY = previous
    }

    const db = openDb(dbPath)
    const rows = db.prepare(
      `SELECT identity_key FROM agents WHERE role='__channel_proxy__'`
    ).all() as Array<{ identity_key: string | null }>
    expect(rows).toHaveLength(1)
    expect(rows[0].identity_key).toBeNull()
    db.close()

    await app.close()
  }, 20000)

  it('closes the MCP transport when register_agent fails', async () => {
    const dir = tmp(); cleanups.push(dir)
    const { app, port, host } = await startServer({
      dbPath: join(dir, 'data.db'),
      port: 0,
      localDevice: 'local-device'
    })
    const url = `http://${host}:${port}/mcp`

    await expect(runRegistrationSequence({
      daemonUrl: url,
      channel_session_id: 'csid-fail',
      device: 'remote-device',
      backoffInitialMs: 10,
      backoffMaxMs: 50
    })).rejects.toThrow(/register_agent failed/)

    await new Promise(r => setTimeout(r, 100))
    const health = await fetch(`http://${host}:${port}/health`)
    const body = await health.json() as {
      mcp_sessions: { total: number; orphan: number; registered: number }
    }
    expect(body.mcp_sessions).toMatchObject({
      total: 0,
      orphan: 0,
      registered: 0,
    })

    await app.close()
  }, 20000)
})

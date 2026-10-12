import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { startServer } from '../src/daemon/server.js'
import { openDb } from '../src/storage/db.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'
import { SseFanout } from '../src/daemon/sse-fanout.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-del-'))

async function boot(dir: string) {
  const dbPath = join(dir, 'data.db')
  const { app, port, host } = await startServer({
    dbPath,
    port: 0,
    host: '127.0.0.1',
    localDevice: 'local',
    fanout: new SseFanout(),
    channelWakeFanout: new ChannelWakeFanout(),
  })
  const seedDb = openDb(dbPath)
  return {
    base: `http://${host}:${port}`,
    app,
    repo: new AgentsRepo(seedDb),
    close: async () => {
      await app.close()
      seedDb.close()
    },
  }
}

function seed(repo: AgentsRepo, name: string, device = 'local'): string {
  return repo.register({ agent_type: 'custom', name, team: 'default', device }).agent_id
}

async function del(base: string, id: string): Promise<Response> {
  return fetch(`${base}/api/agents/${id}`, { method: 'DELETE' })
}

async function listNames(base: string): Promise<string[]> {
  const res = await fetch(`${base}/api/agents?team=default`)
  const data = await res.json() as { agents: Array<{ name: string }> }
  return data.agents.map(a => a.name)
}

describe('DELETE /api/agents/:agent_id', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('removes an existing row and echoes its identity', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const id = seed(h.repo, 'alice')
    seed(h.repo, 'bob')

    const res = await del(h.base, id)
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({
      deleted: true,
      agent_id: id,
      team: 'default',
      name: 'alice',
    })
    expect(await listNames(h.base)).toEqual(['bob'])
    await h.close()
  })

  it('reports 404 unknown_agent for an id that never existed', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)

    const res = await del(h.base, 'does-not-exist')
    expect(res.status).toBe(404)
    expect(await res.json()).toEqual({ error: 'unknown_agent' })
    await h.close()
  })

  it('reports 404 on a repeated removal rather than succeeding twice', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const id = seed(h.repo, 'alice')

    expect((await del(h.base, id)).status).toBe(200)
    const second = await del(h.base, id)
    expect(second.status).toBe(404)
    expect(await second.json()).toEqual({ error: 'unknown_agent' })
    await h.close()
  })

  it('removes a row whose device label differs from the daemon localDevice', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const id = seed(h.repo, 'stray', 'other-host')

    const res = await del(h.base, id)
    expect(res.status).toBe(200)
    expect((await res.json()).agent_id).toBe(id)
    expect(await listNames(h.base)).toEqual([])
    await h.close()
  })

  it('removes a row that reports online rather than refusing on liveness', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const id = seed(h.repo, 'fresh')
    // A just-registered row with no pid and no pane falls through isAgentLive to
    // the last_seen_at window, so it reads online: true.
    const res0 = await fetch(`${h.base}/api/agents?team=default`)
    const listed = (await res0.json() as { agents: Array<{ online: boolean }> }).agents
    expect(listed[0].online).toBe(true)

    expect((await del(h.base, id)).status).toBe(200)
    expect(await listNames(h.base)).toEqual([])
    await h.close()
  })

  it('refuses a remote origin with 403 and leaves the row intact', async () => {
    const dir = tmp(); cleanups.push(dir)
    const h = await boot(dir)
    const id = seed(h.repo, 'alice')

    const res = await h.app.inject({
      method: 'DELETE',
      url: `/api/agents/${id}`,
      remoteAddress: '10.0.0.42',
    })
    expect(res.statusCode).toBe(403)
    expect(res.json()).toEqual({ error: 'remote_forbidden' })
    expect(await listNames(h.base)).toEqual(['alice'])
    await h.close()
  })
})

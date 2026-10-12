import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-ra-cc-mismatch-'))

const { detectTmuxPaneMock, bindRuntimeIdentityMock } = vi.hoisted(() => ({
  detectTmuxPaneMock: vi.fn(),
  bindRuntimeIdentityMock: vi.fn(),
}))

vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: detectTmuxPaneMock,
}))
vi.mock('../src/daemon/runtime-identity.js', () => ({
  bindRuntimeIdentity: bindRuntimeIdentityMock,
}))

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

async function setup(channelWakeFanout?: ChannelWakeFanout) {
  const dir = tmp()
  const dbPath = join(dir, 'data.db')
  const db = openDb(dbPath)
  applySchema(db)
  const server = new McpServer({ name: 'test-server', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-ra-cc-mismatch'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
    channelWakeFanout,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'fake-claude-code', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct, repo: new AgentsRepo(db) }
}

function seedLiveProxy(
  repo: AgentsRepo,
  fanout: ChannelWakeFanout,
  args: { team?: string; csid: string; claude_ui_pid: number; name?: string }
): void {
  repo.register({
    agent_type: 'custom',
    agent_type_name: 'cross-agent-teams-channel',
    model: 'proxy',
    role: '__channel_proxy__',
    name: args.name ?? `proxy-${args.claude_ui_pid}`,
    team: args.team ?? 'default',
    claude_ui_pid: args.claude_ui_pid,
    delivery: { kind: 'claude-channel', channel_session_id: args.csid },
  })
  fanout.attach(args.csid, () => { /* sink */ }, `sess-${args.csid}`)
}

function baseArgs(extra: Record<string, unknown>): Record<string, unknown> {
  return {
    agent_type: 'claude-code',
    model: 'opus',
    name: 'opus',
    ...extra,
  }
}

describe('register_agent({agent_type:"claude-code"}) csid vs ui_pid consistency check', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    detectTmuxPaneMock.mockReset()
    bindRuntimeIdentityMock.mockReset()
  })

  it('rejects with channel_session_id_ui_pid_mismatch when csid differs from live proxy', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport, repo } = await setup(fanout)
    cleanups.push(dir)
    seedLiveProxy(repo, fanout, { csid: 'csid-real', claude_ui_pid: 42000 })
    fanout.attach('csid-stale', () => { /* sink */ }, 'sess-stale')

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({ ui_pid: 42000, channel_session_id: 'csid-stale' }),
    }))
    expect(obj.error).toBe('channel_session_id_ui_pid_mismatch')
    expect(obj.detail).toEqual({
      ui_pid_matched_csid: 'csid-real',
      supplied_csid: 'csid-stale',
    })
    const row = db.prepare(
      `SELECT COUNT(*) AS n FROM agents WHERE name='opus'`
    ).get() as { n: number }
    expect(row.n).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('proceeds and binds when supplied csid matches the live proxy', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport, repo } = await setup(fanout)
    cleanups.push(dir)
    seedLiveProxy(repo, fanout, { csid: 'csid-ok', claude_ui_pid: 42000 })

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({ ui_pid: 42000, channel_session_id: 'csid-ok' }),
    }))
    expect(obj.agent_id).toBeDefined()
    expect(obj.error).toBeUndefined()
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE name='opus'`
    ).get() as { delivery_kind: string; delivery_payload: string | null }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-ok' })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('proceeds to explicit-bind when no live proxy row exists for ui_pid', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport } = await setup(fanout)
    cleanups.push(dir)
    fanout.attach('csid-explicit', () => { /* sink */ }, 'sess-explicit')

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({ ui_pid: 88888, channel_session_id: 'csid-explicit' }),
    }))
    expect(obj.agent_id).toBeDefined()
    expect(obj.error).toBeUndefined()
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE name='opus'`
    ).get() as { delivery_kind: string; delivery_payload: string | null }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-explicit' })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('proceeds to explicit-bind when proxy row is outside the 5-minute live window', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport, repo } = await setup(fanout)
    cleanups.push(dir)
    repo.register({
      agent_type: 'custom',
      model: 'proxy',
      role: '__channel_proxy__',
      name: 'proxy-stale-ra',
      team: 'default',
      claude_ui_pid: 42000,
      delivery: { kind: 'claude-channel', channel_session_id: 'csid-stale-proxy' },
    })
    const oldIso = new Date(Date.now() - 6 * 60 * 1000).toISOString()
    db.prepare(`UPDATE agents SET last_seen_at=? WHERE name='proxy-stale-ra'`).run(oldIso)
    fanout.attach('csid-explicit', () => { /* sink */ }, 'sess-explicit')

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({ ui_pid: 42000, channel_session_id: 'csid-explicit' }),
    }))
    expect(obj.agent_id).toBeDefined()
    expect(obj.error).toBeUndefined()
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE name='opus'`
    ).get() as { delivery_kind: string; delivery_payload: string | null }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-explicit' })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('mismatch check ignores team: proxy in team A rejects caller in team B with wrong csid', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport, repo } = await setup(fanout)
    cleanups.push(dir)
    // Proxy always registers into team='default' per channel-proxy startup spec;
    // the caller here registers into a different team.  Mismatch lookup is keyed
    // on claude_ui_pid alone, so the wrong csid IS rejected across team boundaries.
    seedLiveProxy(repo, fanout, { csid: 'csid-real', claude_ui_pid: 42000, team: 'default' })

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({
        team: 'alpha',
        ui_pid: 42000,
        channel_session_id: 'csid-wrong',
      }),
    }))
    expect(obj.error).toBe('channel_session_id_ui_pid_mismatch')
    expect(obj.detail).toEqual({
      ui_pid_matched_csid: 'csid-real',
      supplied_csid: 'csid-wrong',
    })
    const row = db.prepare(
      `SELECT COUNT(*) as c FROM agents WHERE name='opus'`
    ).get() as { c: number }
    expect(row.c).toBe(0)
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('does not fire when only csid is supplied without ui_pid', async () => {
    bindRuntimeIdentityMock.mockResolvedValue({ error: 'pid_not_found' })
    detectTmuxPaneMock.mockResolvedValue({ error: 'not_found', candidates: [] })
    const fanout = new ChannelWakeFanout()
    const { dir, db, server, client, transport, repo } = await setup(fanout)
    cleanups.push(dir)
    seedLiveProxy(repo, fanout, { csid: 'csid-proxy', claude_ui_pid: 42000 })
    fanout.attach('csid-explicit', () => { /* sink */ }, 'sess-explicit')

    const obj = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: baseArgs({ channel_session_id: 'csid-explicit' }),
    }))
    expect(obj.agent_id).toBeDefined()
    expect(obj.error).toBeUndefined()
    const row = db.prepare(
      `SELECT delivery_kind, delivery_payload FROM agents WHERE name='opus'`
    ).get() as { delivery_kind: string; delivery_payload: string | null }
    expect(row.delivery_kind).toBe('claude-channel')
    expect(JSON.parse(row.delivery_payload as string)).toEqual({ channel_session_id: 'csid-explicit' })
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

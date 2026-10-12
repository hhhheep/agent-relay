import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'
import { AgentsRepo } from '../src/storage/agents-repo.js'

// The identity-key arm ends in the same register path the other reconnect arms
// use, so the tmux / pid machinery is stubbed exactly as in reconnect-tool.
vi.mock('../src/daemon/tmux-pane-detect.js', () => ({
  detectTmuxPane: vi.fn(async () => ({ error: 'not_found', candidates: [] })),
}))
vi.mock('../src/daemon/runtime-identity.js', () => ({
  bindRuntimeIdentity: vi.fn(async () => ({
    ok: true,
    tmux_pane_id: '%1',
    verification_mode: 'verified_pid_tty_pane',
    tty: 'ttys001',
    ui_pid: 25079,
  })),
}))
vi.mock('../src/mcp/auto-bind-codex-pane.js', () => ({
  autoBindCodexPane: vi.fn(async () => false),
}))
vi.mock('../src/mcp/register-codex-self.js', () => ({
  RegisterCodexSelfService: class {
    constructor(private readonly registerSvc: {
      register: (input: Record<string, unknown>) => Record<string, unknown>
    }) {}

    async register(
      input: Record<string, unknown>
    ): Promise<Record<string, unknown>> {
      const threadId = input.thread_id as string
      const wsUrl = (input.ws_url as string | undefined)?.trim()
        || 'ws://127.0.0.1:8799'
      const result = this.registerSvc.register({
        connection_id: input.connection_id,
        agent_type: 'codex',
        model: input.model,
        device: input.device,
        name: input.name,
        role: input.role,
        team: input.team,
        identity_key: input.identity_key,
        delivery: {
          kind: 'codex-appserver',
          thread_id: threadId,
          ws_url: wsUrl,
        },
      })
      return 'error' in result
        ? result
        : { ...result, thread_id: threadId, ws_url: wsUrl }
    }
  },
}))

import { autoBindCodexPane } from '../src/mcp/auto-bind-codex-pane.js'
import { bindRuntimeIdentity } from '../src/daemon/runtime-identity.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-reconnect-identity-key-'))
const THREAD_OLD = '11111111-1111-4111-8111-111111111111'
const THREAD_NEW = '22222222-2222-4222-8222-222222222222'

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

async function setup(opts: { localDevice?: string } = {}) {
  const dir = tmp()
  const db = openDb(join(dir, 'data.db'))
  const localDevice = opts.localDevice ?? 'local'
  applySchema(db, { localDevice })
  const server = new McpServer({ name: 'test-server', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-identity-key'
  registerBusinessTools(
    server,
    db,
    () => holder.current ?? sessionId,
    undefined,
    (agentId) => { holder.current = agentId },
    () => sessionId,
    undefined,
    undefined,
    undefined,
    undefined,
    { localDevice },
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'claude-code', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct, holder, repo: new AgentsRepo(db) }
}

function seedClaudeAgent(
  db: ReturnType<typeof openDb>,
  args: {
    agent_id: string
    name: string
    device?: string
    team?: string
    role?: string
    runtime_ui_pid: number | null
    identity_key?: string
    last_processed_event_id?: number
    tmux_pane_id?: string
    runtime_tty?: string
  }
): void {
  db.prepare(
    `INSERT INTO agents (
       agent_id, agent_type, device, team, role, name, registered_at, last_seen_at,
       runtime_ui_pid, identity_key, last_processed_event_id, tmux_pane_id, runtime_tty
     ) VALUES (?, 'claude-code', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    args.agent_id,
    args.device ?? 'local',
    args.team ?? 'aoe',
    args.role ?? 'worker',
    args.name,
    '2026-01-01T00:00:00Z',
    '2026-01-01T00:00:00Z',
    args.runtime_ui_pid,
    args.identity_key ?? null,
    args.last_processed_event_id ?? 0,
    args.tmux_pane_id ?? null,
    args.runtime_tty ?? null,
  )
}

function seedCodexAgent(
  db: ReturnType<typeof openDb>,
  args: {
    agent_id: string
    name: string
    thread_id: string
    identity_key: string
    last_processed_event_id?: number
  }
): void {
  db.prepare(
    `INSERT INTO agents (
       agent_id, agent_type, device, team, role, name, registered_at, last_seen_at,
       delivery_kind, delivery_payload, identity_key, last_processed_event_id
     ) VALUES (?, 'codex', 'local', 'aoe', 'worker', ?, ?, ?, 'codex-appserver', ?, ?, ?)`
  ).run(
    args.agent_id,
    args.name,
    '2026-01-01T00:00:00Z',
    '2026-01-01T00:00:00Z',
    JSON.stringify({ thread_id: args.thread_id, ws_url: 'ws://127.0.0.1:8799' }),
    args.identity_key,
    args.last_processed_event_id ?? 0,
  )
}

describe('reconnect by identity_key', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
    vi.mocked(autoBindCodexPane).mockClear()
    vi.mocked(bindRuntimeIdentity).mockClear()
  })

  it('recovers the identity after a restart, keeping agent_id and the unread cursor', async () => {
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'X',
      name: 'tester',
      runtime_ui_pid: 111,
      identity_key: 'K',
      last_processed_event_id: 42,
    })

    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 25079 },
    }))
    expect(res).toMatchObject({
      ok: true,
      agent_id: 'X',
      name: 'tester',
      team: 'aoe',
    })

    const row = db.prepare(
      `SELECT agent_id, last_processed_event_id, runtime_ui_pid, identity_key
       FROM agents WHERE name='tester'`
    ).get() as {
      agent_id: string
      last_processed_event_id: number
      runtime_ui_pid: number
      identity_key: string
    }
    expect(row.agent_id).toBe('X')
    expect(row.last_processed_event_id).toBe(42)
    expect(row.runtime_ui_pid).toBe(25079)
    expect(row.identity_key).toBe('K')
    expect(holder.current).toBe('X')

    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('overwrites the dead pane binding with the values verified from the new pid', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'X',
      name: 'tester',
      runtime_ui_pid: 111,
      identity_key: 'K',
      tmux_pane_id: '%dead',
      runtime_tty: 'ttys999',
    })

    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 25079 },
    }))
    expect(res).toMatchObject({ ok: true, agent_id: 'X' })
    // The envelope carries the csid slot the shared register path rebinds.
    expect('channel_session_id' in res).toBe(true)

    const row = db.prepare(
      `SELECT tmux_pane_id, runtime_tty, runtime_ui_pid
       FROM agents WHERE agent_id='X'`
    ).get() as {
      tmux_pane_id: string
      runtime_tty: string
      runtime_ui_pid: number
    }
    // Values come from the mocked bindRuntimeIdentity, driven by the new pid.
    expect(row.tmux_pane_id).toBe('%1')
    expect(row.runtime_tty).toBe('ttys001')
    expect(row.runtime_ui_pid).toBe(25079)
    expect(vi.mocked(bindRuntimeIdentity).mock.calls[0][0]).toMatchObject({
      ui_pid: 25079,
    })

    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('returns need_register for an unknown key without touching a row', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 4242 },
    }))
    expect(res).toMatchObject({ need_register: true })
    expect(typeof res.reason).toBe('string')
    const count = db.prepare(`SELECT COUNT(*) AS c FROM agents`).get() as { c: number }
    expect(count.c).toBe(0)
    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('does not match a row on another device', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'X',
      name: 'tester',
      device: 'gx',
      runtime_ui_pid: null,
      identity_key: 'K',
    })
    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 4242 },
    }))
    expect(res).toMatchObject({ need_register: true })
    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('does not match a __channel_proxy__ row', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'P',
      name: 'channel-proxy-1',
      role: '__channel_proxy__',
      runtime_ui_pid: null,
      identity_key: 'K',
    })
    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 4242 },
    }))
    expect(res).toMatchObject({ need_register: true })
    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('lets the key win over the row the accompanying ui_pid points at', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'A',
      name: 'keyed',
      runtime_ui_pid: null,
      identity_key: 'K',
    })
    seedClaudeAgent(db, {
      agent_id: 'B',
      name: 'pid-squatter',
      runtime_ui_pid: 25079,
    })
    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', ui_pid: 25079 },
    }))
    expect(res).toMatchObject({ ok: true, agent_id: 'A', name: 'keyed' })
    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('rewrites the codex delivery thread and binds the pane via pre-registration', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedCodexAgent(db, {
      agent_id: 'C',
      name: 'codex-tester',
      thread_id: THREAD_OLD,
      identity_key: 'K',
      last_processed_event_id: 7,
    })

    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K', thread_id: THREAD_NEW },
    }))
    expect(res).toMatchObject({
      ok: true,
      agent_id: 'C',
      name: 'codex-tester',
      team: 'aoe',
      thread_id: THREAD_NEW,
    })

    const row = db.prepare(
      `SELECT delivery_payload, last_processed_event_id
       FROM agents WHERE agent_id='C'`
    ).get() as { delivery_payload: string; last_processed_event_id: number }
    expect(JSON.parse(row.delivery_payload).thread_id).toBe(THREAD_NEW)
    expect(row.last_processed_event_id).toBe(7)

    // No ui_pid was supplied, so the pane falls through to the launcher's
    // pending pre-registration lookup instead of pid-based binding.
    expect(vi.mocked(autoBindCodexPane)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(autoBindCodexPane).mock.calls[0][0]).toMatchObject({
      callerAgentId: 'C',
    })
    expect(vi.mocked(bindRuntimeIdentity)).not.toHaveBeenCalled()

    await transport.close(); await client.close(); db.close(); await server.close()
  })

  it('preserves the stored agent_type when only the key is supplied', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedClaudeAgent(db, {
      agent_id: 'X',
      name: 'tester',
      runtime_ui_pid: 111,
      identity_key: 'K',
    })
    const res = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: { identity_key: 'K' },
    }))
    expect(res).toMatchObject({ ok: true, agent_id: 'X', name: 'tester' })
    const row = db.prepare(
      `SELECT agent_type FROM agents WHERE agent_id='X'`
    ).get() as { agent_type: string | null }
    expect(row.agent_type).toBe('claude-code')
    await transport.close(); await client.close(); db.close(); await server.close()
  })
})

describe('reconnect identity_key schema composition', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('accepts identity_key alone and combined with one runtime key', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    const shapes = [
      { identity_key: 'K' },
      { identity_key: 'K', ui_pid: 4242 },
      { identity_key: 'K', thread_id: THREAD_NEW },
    ]
    for (const args of shapes) {
      const resp = await client.callTool({ name: 'reconnect', arguments: args }) as {
        isError?: boolean
      }
      expect(resp.isError, `reconnect(${JSON.stringify(args)})`).toBeFalsy()
    }
    await transport.close(); await client.close(); await server.close()
  })

  it('rejects blank keys, two runtime keys, and the base_url combination', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    const bad = [
      { identity_key: '' },
      { identity_key: '   ' },
      { identity_key: 'K', ui_pid: 4242, thread_id: THREAD_NEW },
      { identity_key: 'K', base_url: 'http://127.0.0.1:18888' },
    ]
    for (const args of bad) {
      const resp = await client.callTool({ name: 'reconnect', arguments: args }) as {
        isError?: boolean
      }
      expect(resp.isError, `reconnect(${JSON.stringify(args)})`).toBe(true)
    }
    await transport.close(); await client.close(); await server.close()
  })

  it('keeps the exactly-one rule for calls without an identity_key', async () => {
    const { dir, server, client, transport } = await setup()
    cleanups.push(dir)
    for (const args of [{}, { ui_pid: 4242, thread_id: THREAD_NEW }]) {
      const resp = await client.callTool({ name: 'reconnect', arguments: args }) as {
        isError?: boolean
      }
      expect(resp.isError, `reconnect(${JSON.stringify(args)})`).toBe(true)
    }
    const single = await client.callTool({
      name: 'reconnect',
      arguments: { ui_pid: 4242 },
    }) as { isError?: boolean }
    expect(single.isError).toBeFalsy()
    await transport.close(); await client.close(); await server.close()
  })
})

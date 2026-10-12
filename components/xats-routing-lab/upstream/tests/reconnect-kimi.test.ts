import { describe, it, expect, afterEach, vi } from 'vitest'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerBusinessTools } from '../src/mcp/tools.js'
import { validateKimiSession } from '../src/mcp/reconnect.js'
import { RegisterAgentService } from '../src/mcp/register-agent.js'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-reconnect-kimi-'))

const KIMI_BASE_URL = 'http://127.0.0.1:58627'

async function parseTool(resp: unknown): Promise<Record<string, unknown>> {
  const r = resp as { content: Array<{ text: string }> }
  return JSON.parse(r.content[0].text)
}

interface SeenCall {
  method: string
  url: string
  headers: Record<string, string>
}

function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, msg: 'ok', data })
}

// Stubbed kimi server: GET /api/v1/sessions/<id> answers per `session`.
function makeKimiFetch(args: {
  seen: SeenCall[]
  session?: { status?: number; body?: string }
  reject?: boolean
}): typeof fetch {
  return (async (url: string, init?: RequestInit) => {
    if (args.reject) throw new Error('ECONNREFUSED')
    args.seen.push({
      method: init?.method ?? 'GET',
      url,
      headers: (init?.headers ?? {}) as Record<string, string>,
    })
    const body = args.session?.body ?? envelope({ id: 'any' })
    return new Response(body.length > 0 ? body : null, {
      status: args.session?.status ?? 200,
    })
  }) as unknown as typeof fetch
}

function seedKimiAgent(
  db: ReturnType<typeof openDb>,
  args: {
    agent_id: string
    session_id: string
    base_url?: string
    device?: string
    team?: string
    name: string
    last_seen_at: string
    registered_at?: string
    last_processed_event_id?: number
    auth_token_ref?: string
  }
): void {
  db.prepare(
    `INSERT INTO agents (
       agent_id, agent_type, device, team, role, name, registered_at, last_seen_at,
       delivery_kind, delivery_payload, last_processed_event_id
     ) VALUES (?, 'kimi-code', ?, ?, 'worker', ?, ?, ?, 'kimi-server', ?, ?)`
  ).run(
    args.agent_id,
    args.device ?? 'local',
    args.team ?? 'default',
    args.name,
    args.registered_at ?? args.last_seen_at,
    args.last_seen_at,
    JSON.stringify({
      session_id: args.session_id,
      base_url: args.base_url ?? KIMI_BASE_URL,
      ...(args.auth_token_ref === undefined ? {} : { auth_token_ref: args.auth_token_ref }),
    }),
    args.last_processed_event_id ?? 0,
  )
}

async function setup(opts: { registerSvc?: RegisterAgentService; db?: ReturnType<typeof openDb>; dir?: string } = {}) {
  const dir = opts.dir ?? tmp()
  const db = opts.db ?? openDb(join(dir, 'data.db'))
  if (opts.db === undefined) applySchema(db, { localDevice: 'local' })
  const server = new McpServer({ name: 'test-server', version: '0.0.0' })
  const holder: { current: string | undefined } = { current: undefined }
  const sessionId = 'session-reconnect-kimi'
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
    { localDevice: 'local' },
    undefined,
    opts.registerSvc,
  )
  const [ct, st] = InMemoryTransport.createLinkedPair()
  await server.connect(st)
  const client = new Client({ name: 'kimi-code', version: '0.0.0' })
  await client.connect(ct)
  return { dir, db, server, client, transport: ct, holder }
}

describe('reconnect kimi-code by (base_url, session_id)', () => {
  const cleanups: string[] = []
  const envKeys: string[] = []
  afterEach(() => {
    for (const k of envKeys) delete process.env[k]
    envKeys.length = 0
    vi.unstubAllGlobals()
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('recovers a single local kimi identity, revalidating with the stored auth_token_ref', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({
      seen,
      session: { body: envelope({ id: 'session_kimi_1' }) },
    }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    const registeredAt = '2024-01-01T00:00:00.000Z'
    const lastSeen = '2024-01-02T00:00:00.000Z'
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: lastSeen,
      registered_at: registeredAt,
      last_processed_event_id: 42,
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({
      ok: true,
      agent_id: 'K',
      name: 'xats-kimi',
      team: 'default',
      session_id: 'session_kimi_1',
      base_url: KIMI_BASE_URL,
      last_seen_at: lastSeen,
    })
    expect(holder.current).toBe('K')

    // The probe hits the kimi session endpoint with the poke dispatcher's
    // bearer resolution (auth_token_ref -> env var value).
    expect(seen[0].method).toBe('GET')
    expect(seen[0].url).toBe(`${KIMI_BASE_URL}/api/v1/sessions/session_kimi_1`)
    expect(seen[0].headers['Authorization']).toBe('Bearer kimi-secret')

    const row = db.prepare(
      `SELECT agent_id, registered_at, last_seen_at, last_processed_event_id
       FROM agents WHERE agent_id='K'`
    ).get() as {
      agent_id: string
      registered_at: string
      last_seen_at: string
      last_processed_event_id: number
    }
    expect(row.registered_at).toBe(registeredAt)
    expect(row.last_processed_event_id).toBe(42)
    expect(row.last_seen_at).not.toBe(lastSeen)
    const count = db.prepare(
      `SELECT COUNT(*) AS c FROM agents WHERE name='xats-kimi'`
    ).get() as { c: number }
    expect(count.c).toBe(1)

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns session_not_found without mutation when the kimi server reports the session gone', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({
      seen,
      session: {
        body: JSON.stringify({ code: 40401, msg: 'session does not exist', data: null }),
      },
    }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_gone',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_gone' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({
      error: 'session_not_found',
      detail: { base_url: KIMI_BASE_URL, session_id: 'session_gone' },
    })
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns session_not_found without mutation when the probe itself fails', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    vi.stubGlobal('fetch', makeKimiFetch({ seen: [], reject: true }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ error: 'session_not_found' })
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns session_not_found when a 200 envelope does not identify the session', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    vi.stubGlobal('fetch', makeKimiFetch({ seen: [], session: { body: '{}' } }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ error: 'session_not_found' })
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns session_not_found when a 200 body carries a root-level id without the envelope', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    // Would have passed the lenient parser via its root fallback: the id
    // matches, but the body is not a kimi success envelope.
    vi.stubGlobal('fetch', makeKimiFetch({
      seen: [],
      session: { body: JSON.stringify({ id: 'session_kimi_1' }) },
    }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ error: 'session_not_found' })
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns session_not_found when the session is archived', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    vi.stubGlobal('fetch', makeKimiFetch({
      seen: [],
      session: { body: envelope({ id: 'session_kimi_1', archived: true }) },
    }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ error: 'session_not_found' })
    expect(holder.current).toBeUndefined()

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('recovers a row persisted before URL canonicalization (raw legacy spelling)', async () => {
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    vi.stubGlobal('fetch', makeKimiFetch({
      seen: [],
      session: { body: envelope({ id: 'session_kimi_1' }) },
    }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    // A pre-canonicalization row: uppercase scheme + trailing slash stored raw.
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      base_url: 'HTTP://127.0.0.1:58627/',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_RECONNECT_TOK',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: KIMI_BASE_URL,
        session_id: 'session_kimi_1',
      },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ ok: true, agent_id: 'K', name: 'xats-kimi' })
    expect(holder.current).toBe('K')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('register and reconnect agree on the trimmed session_id', async () => {
    // Explicit ref like its siblings: without one the bearer falls back to
    // ~/.kimi-code/server.token, which only exists on a kimi developer's box.
    process.env.KIMI_RECONNECT_TOK = 'kimi-secret'
    envKeys.push('KIMI_RECONNECT_TOK')
    vi.stubGlobal('fetch', makeKimiFetch({
      seen: [],
      session: { body: envelope({ id: 'session_pad' }) },
    }))
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)

    const reg = await parseTool(await client.callTool({
      name: 'register_agent',
      arguments: {
        agent_type: 'kimi-code',
        name: 'kimi-pad',
        base_url: KIMI_BASE_URL,
        session_id: '  session_pad  ',
        auth_token_ref: 'KIMI_RECONNECT_TOK',
      },
    }))
    // The schema trims, so response, stored row, and lookups all agree.
    expect(reg.session_id).toBe('session_pad')

    const rec = await parseTool(await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: KIMI_BASE_URL,
        session_id: '  session_pad  ',
      },
    }))
    expect(rec).toMatchObject({
      ok: true,
      agent_id: reg.agent_id,
      session_id: 'session_pad',
    })

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns need_register on an empty registry when agent_type="kimi-code" is explicit', async () => {
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({ seen }))
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: {
        agent_type: 'kimi-code',
        base_url: KIMI_BASE_URL,
        session_id: 'session_kimi_1',
      },
    })
    const obj = await parseTool(resp)

    // Deterministic dispatch: the kimi need_register answer, and no probe of
    // any server (opencode or kimi) was attempted.
    expect(obj).toMatchObject({ need_register: true })
    expect(seen).toEqual([])

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns need_register when no local kimi row matches the session_id', async () => {
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({ seen }))
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_other',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_nomatch' },
    })
    const obj = await parseTool(resp)

    expect(obj.need_register).toBe(true)
    expect(obj.reason).toContain('session_nomatch')
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('never auto-resolves a kimi row when session_id is omitted', async () => {
    // Only kimi rows exist on this base_url; without an explicit session_id
    // the kimi arm must not run, so no kimi identity is recovered.
    vi.stubGlobal('fetch', makeKimiFetch({ seen: [], reject: true }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL },
    })
    const obj = await parseTool(resp)

    expect(obj.ok).toBeUndefined()
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns ambiguous kimi candidates ordered by last_seen_at without mutation', async () => {
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({ seen }))
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K1',
      session_id: 'session_dup',
      name: 'older-kimi',
      last_seen_at: '2024-01-01T00:00:00.000Z',
    })
    seedKimiAgent(db, {
      agent_id: 'K2',
      session_id: 'session_dup',
      name: 'newer-kimi',
      last_seen_at: '2024-06-01T00:00:00.000Z',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_dup' },
    })
    const obj = await parseTool(resp)

    expect(obj.ambiguous).toBe(true)
    const candidates = obj.candidates as Array<{ name: string }>
    expect(candidates.map(c => c.name)).toEqual(['newer-kimi', 'older-kimi'])
    const rows = db.prepare(
      `SELECT agent_id, last_seen_at FROM agents ORDER BY agent_id`
    ).all()
    expect(rows).toEqual([
      { agent_id: 'K1', last_seen_at: '2024-01-01T00:00:00.000Z' },
      { agent_id: 'K2', last_seen_at: '2024-06-01T00:00:00.000Z' },
    ])

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('returns missing_auth_token without mutation when the stored ref is unset', async () => {
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({ seen }))
    const { dir, db, server, client, transport, holder } = await setup()
    cleanups.push(dir)
    seedKimiAgent(db, {
      agent_id: 'K',
      session_id: 'session_kimi_1',
      name: 'xats-kimi',
      last_seen_at: '2024-01-02T00:00:00.000Z',
      auth_token_ref: 'KIMI_UNSET_TOKEN_REF',
    })

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
    })
    const obj = await parseTool(resp)

    expect(obj).toEqual({
      error: 'missing_auth_token',
      detail: { ref: 'KIMI_UNSET_TOKEN_REF' },
    })
    expect(seen).toHaveLength(0)
    expect(holder.current).toBeUndefined()
    const row = db.prepare(
      `SELECT last_seen_at FROM agents WHERE agent_id='K'`
    ).get() as { last_seen_at: string }
    expect(row.last_seen_at).toBe('2024-01-02T00:00:00.000Z')

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })

  it('rebinds under the kimi runtime key: recovery shares with a live engine connection', async () => {
    process.env.KIMI_SHARE_TOK = 'kimi-secret'
    envKeys.push('KIMI_SHARE_TOK')
    const seen: SeenCall[] = []
    vi.stubGlobal('fetch', makeKimiFetch({
      seen,
      session: { body: envelope({ id: 'session_share' }) },
    }))
    const dir = tmp()
    const db = openDb(join(dir, 'data.db'))
    applySchema(db, { localDevice: 'local' })
    const closes: string[] = []
    let lines: string[] = []
    const registerSvc = new RegisterAgentService(db, {
      closeSessionByConnectionId: (connectionId) => {
        closes.push(connectionId)
        return true
      },
      log: line => { lines = [...lines, line] },
      localDevice: 'local',
    })
    // A live engine connection already holds the identity for this session.
    const live = registerSvc.register({
      connection_id: 'live-conn',
      agent_type: 'kimi-code',
      name: 'xats-kimi',
      team: 'default',
      delivery: {
        kind: 'kimi-server',
        session_id: 'session_share',
        base_url: KIMI_BASE_URL,
        auth_token_ref: 'KIMI_SHARE_TOK',
      },
    })
    if ('error' in live) throw new Error('unexpected live register error')

    const { server, client, transport } = await setup({ registerSvc, db, dir })
    cleanups.push(dir)

    const resp = await client.callTool({
      name: 'reconnect',
      arguments: { base_url: KIMI_BASE_URL, session_id: 'session_share' },
    })
    const obj = await parseTool(resp)

    expect(obj).toMatchObject({ ok: true, agent_id: live.agent_id })
    expect(closes).toEqual([])
    expect(lines.some(line => line.includes('register_agent takeover')))
      .toBe(false)

    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

describe('reconnect tool description kimi arm', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('documents base_url + REQUIRED session_id and the launcher re-export', async () => {
    const { dir, db, server, client, transport } = await setup()
    cleanups.push(dir)
    const resp = await client.listTools()
    const tool = resp.tools.find(t => t.name === 'reconnect')
    expect(tool).toBeDefined()
    expect(tool!.description).toContain('base_url=$KIMI_XATS_BASE_URL')
    expect(tool!.description).toContain('session_id=$KIMI_XATS_SESSION_ID')
    expect(tool!.description).toContain('`session_id` is REQUIRED for the kimi path')
    expect(tool!.description).toContain('KIMI_XATS_BASE_URL / KIMI_XATS_SESSION_ID')
    expect(tool!.description).toContain('kimi-server')
    await transport.close()
    await client.close()
    db.close()
    await server.close()
  })
})

describe('validateKimiSession token resolution', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  function makeTokenFile(content: string): string {
    const dir = tmp()
    cleanups.push(dir)
    const path = join(dir, 'server.token')
    writeFileSync(path, content)
    return path
  }

  it('falls back to the kimi token file when no auth_token_ref is stored', async () => {
    const seen: SeenCall[] = []
    const fetchMock = makeKimiFetch({
      seen,
      session: { body: envelope({ id: 'session_kimi_1' }) },
    })
    const tokenFilePath = makeTokenFile('file-token\n')

    const result = await validateKimiSession(
      { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
      { env: {}, fetch: fetchMock, tokenFilePath }
    )

    expect(result).toEqual({ ok: true })
    expect(seen[0].headers['Authorization']).toBe('Bearer file-token')
  })

  it('returns missing_auth_token (no probe) when the token file is absent', async () => {
    const dir = tmp()
    cleanups.push(dir)
    const tokenFilePath = join(dir, 'server.token')
    const seen: SeenCall[] = []
    const fetchMock = makeKimiFetch({ seen })

    const result = await validateKimiSession(
      { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
      { env: {}, fetch: fetchMock, tokenFilePath }
    )

    expect(result).toEqual({
      error: 'missing_auth_token',
      detail: { token_file: tokenFilePath },
    })
    expect(seen).toHaveLength(0)
  })

  it('treats a non-2xx probe response as session_not_found', async () => {
    const seen: SeenCall[] = []
    const fetchMock = makeKimiFetch({
      seen,
      session: { status: 404, body: 'not found' },
    })
    const tokenFilePath = makeTokenFile('file-token')

    const result = await validateKimiSession(
      { base_url: KIMI_BASE_URL, session_id: 'session_ghost' },
      { env: {}, fetch: fetchMock, tokenFilePath }
    )

    expect(result).toMatchObject({
      error: 'session_not_found',
      detail: { base_url: KIMI_BASE_URL, session_id: 'session_ghost' },
    })
  })

  it('rejects a 2xx body that is not a strict kimi success envelope', async () => {
    const tokenFilePath = makeTokenFile('file-token')
    const bodies = [
      JSON.stringify({ id: 'session_kimi_1' }),
      JSON.stringify({ code: 0, msg: 'ok', data: null }),
      JSON.stringify({ code: 0, msg: 'ok', data: [] }),
    ]
    for (const body of bodies) {
      const fetchMock = makeKimiFetch({ seen: [], session: { body } })
      const result = await validateKimiSession(
        { base_url: KIMI_BASE_URL, session_id: 'session_kimi_1' },
        { env: {}, fetch: fetchMock, tokenFilePath }
      )
      expect(result, `body=${body}`).toMatchObject({
        error: 'session_not_found',
        detail: { cause: 'error_envelope' },
      })
    }
  })
})

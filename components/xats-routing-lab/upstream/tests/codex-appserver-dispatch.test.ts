import { describe, it, expect } from 'vitest'
import {
  dispatchCodexAppserverPoke,
  type WebSocketLike,
} from '../src/mcp/codex-appserver-dispatch.js'

const THREAD_ID = '11111111-1111-4111-8111-111111111111'
const DELIVERY = {
  kind: 'codex-appserver' as const,
  thread_id: THREAD_ID,
  ws_url: 'ws://127.0.0.1:8799',
}

type EventName = 'open' | 'message' | 'error' | 'close'
type JsonRpcMessage = { id?: number; method?: string; params?: unknown }

class MockWebSocket implements WebSocketLike {
  readonly sent: JsonRpcMessage[] = []
  readonly listeners: Record<EventName, Set<(event: unknown) => void>> = {
    open: new Set(),
    message: new Set(),
    error: new Set(),
    close: new Set(),
  }
  closeCalls = 0

  constructor(private readonly onSend: (message: JsonRpcMessage, ws: MockWebSocket) => void) {}

  addEventListener(type: EventName, listener: (event: unknown) => void): void {
    this.listeners[type].add(listener)
  }

  removeEventListener(type: EventName, listener: (event: unknown) => void): void {
    this.listeners[type].delete(listener)
  }

  send(data: string): void {
    const message = JSON.parse(data) as JsonRpcMessage
    this.sent.push(message)
    this.onSend(message, this)
  }

  close(): void {
    this.closeCalls += 1
  }

  emit(type: EventName, event: unknown): void {
    for (const listener of this.listeners[type]) {
      listener(event)
    }
  }

  reply(id: number, payload: { result?: unknown; error?: unknown }): void {
    this.emit('message', {
      data: JSON.stringify({ jsonrpc: '2.0', id, ...payload }),
    })
  }
}

function createHarness(args: {
  onCreate?: (ws: MockWebSocket) => void
  onSend: (message: JsonRpcMessage, ws: MockWebSocket) => void
}) {
  const calls: Array<{ url: string; headers?: Record<string, string> }> = []
  const sockets: MockWebSocket[] = []
  return {
    calls,
    sockets,
    factory: ({ url, headers }: { url: string; headers?: Record<string, string> }) => {
      const ws = new MockWebSocket(args.onSend)
      calls.push({ url, headers })
      sockets.push(ws)
      args.onCreate?.(ws)
      return ws
    },
  }
}

describe('dispatchCodexAppserverPoke', () => {
  it('returns codex-appserver success after initialize, initialized, resume, and turn/start', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, { result: { turn: { id: 'turn-1' } } })
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'turn/started',
              params: {
                threadId: THREAD_ID,
                turn: { id: 'turn-1' },
              },
            }),
          }))
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'item/started',
              params: {
                threadId: THREAD_ID,
                turnId: 'turn-1',
                item: {
                  type: 'mcpToolCall',
                  tool: 'get_inbox',
                },
              },
            }),
          }))
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello from daemon' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      ok: true,
      transport_used: 'codex-appserver',
      thread_id: THREAD_ID,
    })
    expect(harness.calls).toEqual([
      { url: 'ws://127.0.0.1:8799', headers: undefined },
    ])
    expect(harness.sockets[0]?.sent.map((message) => message.method)).toEqual([
      'initialize',
      'initialized',
      'thread/resume',
      'turn/start',
    ])
    expect(harness.sockets[0]?.sent[3]).toMatchObject({
      method: 'turn/start',
      params: {
        threadId: THREAD_ID,
        input: [{ type: 'text', text: 'hello from daemon', text_elements: [] }],
      },
    })
    expect(harness.sockets[0]?.closeCalls).toBe(0)
    harness.sockets[0]?.emit('message', {
      data: JSON.stringify({
        jsonrpc: '2.0',
        method: 'turn/completed',
        params: {
          threadId: THREAD_ID,
          turn: { id: 'turn-1' },
        },
      }),
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('does not report success when turn/start is accepted but turn/started never arrives', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, { result: { turn: { id: 'turn-1' } } })
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello from daemon' },
      {
        webSocketFactory: harness.factory,
        env: {},
        turnStartConfirmTimeoutMs: 5,
      }
    )

    expect(result).toEqual({
      error: 'codex_turn_start_unconfirmed',
      detail: 'turn/started notification timed out for turn-1',
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('does not report success when the turn starts but never calls get_inbox', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, { result: { turn: { id: 'turn-no-inbox' } } })
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'turn/started',
              params: {
                threadId: THREAD_ID,
                turn: { id: 'turn-no-inbox' },
              },
            }),
          }))
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello from daemon' },
      {
        webSocketFactory: harness.factory,
        env: {},
        wakeConfirmTimeoutMs: 5,
      }
    )

    expect(result).toEqual({
      error: 'codex_wake_unconfirmed',
      detail: 'get_inbox tool call not observed for turn-no-inbox',
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('reports a socket close during wake confirmation as a connection failure', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, { result: { turn: { id: 'turn-close' } } })
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'turn/started',
              params: { threadId: THREAD_ID, turn: { id: 'turn-close' } },
            }),
          }))
          queueMicrotask(() => ws.emit('close', {
            code: 1006,
            reason: 'connection lost',
          }))
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello from daemon' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'codex_connect_failed',
      detail: 'close 1006: connection lost',
      transport_used: 'codex-appserver',
    })
  })

  it('accepts a buffered wake confirmation that arrived before the socket closed', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, { result: { turn: { id: 'turn-buffered' } } })
          for (const payload of [
            {
              method: 'turn/started',
              params: { threadId: THREAD_ID, turn: { id: 'turn-buffered' } },
            },
            {
              method: 'item/started',
              params: {
                threadId: THREAD_ID,
                turnId: 'turn-buffered',
                item: { type: 'mcpToolCall', tool: 'get_inbox' },
              },
            },
          ]) {
            queueMicrotask(() => ws.emit('message', {
              data: JSON.stringify({ jsonrpc: '2.0', ...payload }),
            }))
          }
          queueMicrotask(() => ws.emit('close', {
            code: 1006,
            reason: 'closed after delivery',
          }))
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello from daemon' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toMatchObject({
      ok: true,
      transport_used: 'codex-appserver',
    })
  })

  it('steers the active turn when the resumed thread is busy', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, {
            result: {
              thread: {
                turns: [{ id: 'turn-active', status: 'inProgress' }],
              },
            },
          })
          ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'item/started',
              params: {
                threadId: THREAD_ID,
                turnId: 'turn-active',
                item: { type: 'mcpToolCall', tool: 'get_inbox' },
              },
            }),
          })
        }
        if (message.method === 'turn/steer' && message.id) {
          ws.reply(message.id, { result: { turnId: 'turn-active' } })
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'item/started',
              params: {
                threadId: THREAD_ID,
                turnId: 'turn-active',
                item: { type: 'mcpToolCall', tool: 'get_inbox' },
              },
            }),
          }))
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'new mail arrived' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toMatchObject({ ok: true })
    expect(harness.sockets[0]?.sent.map((message) => message.method)).toEqual([
      'initialize',
      'initialized',
      'thread/resume',
      'turn/steer',
    ])
    expect(harness.sockets[0]?.sent[3]).toMatchObject({
      method: 'turn/steer',
      params: {
        threadId: THREAD_ID,
        expectedTurnId: 'turn-active',
        input: [{
          type: 'text',
          text: 'new mail arrived',
          text_elements: [],
        }],
      },
    })
  })

  it('resolves auth token from env and sends bearer Authorization header', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'turn/start' && typeof message.id === 'number') {
          ws.reply(message.id, { result: { turn: { id: 'turn-auth' } } })
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'turn/started',
              params: { threadId: THREAD_ID, turn: { id: 'turn-auth' } },
            }),
          }))
          queueMicrotask(() => ws.emit('message', {
            data: JSON.stringify({
              jsonrpc: '2.0',
              method: 'item/started',
              params: {
                threadId: THREAD_ID,
                turnId: 'turn-auth',
                item: { type: 'mcpToolCall', tool: 'get_inbox' },
              },
            }),
          }))
        } else if (typeof message.id === 'number') {
          ws.reply(message.id, { result: { ok: true } })
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      {
        delivery: {
          ...DELIVERY,
          auth_token_ref: 'CODEX_REMOTE_TOKEN',
        },
        content: 'hello',
      },
      {
        webSocketFactory: harness.factory,
        env: { CODEX_REMOTE_TOKEN: 'secret-token' },
      }
    )

    expect(result).toMatchObject({ ok: true, transport_used: 'codex-appserver' })
    expect(harness.calls).toEqual([
      {
        url: 'ws://127.0.0.1:8799',
        headers: { Authorization: 'Bearer secret-token' },
      },
    ])
  })

  it('returns missing_auth_token before websocket connect when auth_token_ref is unset', async () => {
    const harness = createHarness({
      onSend: () => undefined,
    })

    const result = await dispatchCodexAppserverPoke(
      {
        delivery: {
          ...DELIVERY,
          auth_token_ref: 'CODEX_REMOTE_TOKEN',
        },
        content: 'hello',
      },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'missing_auth_token',
      detail: { ref: 'CODEX_REMOTE_TOKEN' },
    })
    expect(harness.calls).toHaveLength(0)
  })

  it('maps websocket dial failure to codex_connect_failed', async () => {
    const harness = createHarness({
      onCreate: (ws) =>
        queueMicrotask(() => ws.emit('error', { message: 'ECONNREFUSED' })),
      onSend: () => undefined,
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'codex_connect_failed',
      detail: 'ECONNREFUSED',
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('maps initialize RPC failure to codex_initialize_failed', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, {
            error: { code: -32000, message: 'initialize failed' },
          })
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'codex_initialize_failed',
      detail: { code: -32000, message: 'initialize failed' },
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('maps thread/resume RPC failure to codex_resume_failed', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, {
            error: { code: -32001, message: 'resume failed' },
          })
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'codex_resume_failed',
      detail: { code: -32001, message: 'resume failed' },
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })

  it('maps turn/start RPC failure to codex_turn_start_failed', async () => {
    const harness = createHarness({
      onCreate: (ws) => queueMicrotask(() => ws.emit('open', {})),
      onSend: (message, ws) => {
        if (message.method === 'initialize' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'thread/resume' && message.id) {
          ws.reply(message.id, { result: { ok: true } })
        }
        if (message.method === 'turn/start' && message.id) {
          ws.reply(message.id, {
            error: { code: -32002, message: 'turn start failed' },
          })
        }
      },
    })

    const result = await dispatchCodexAppserverPoke(
      { delivery: DELIVERY, content: 'hello' },
      { webSocketFactory: harness.factory, env: {} }
    )

    expect(result).toEqual({
      error: 'codex_turn_start_failed',
      detail: { code: -32002, message: 'turn start failed' },
      transport_used: 'codex-appserver',
    })
    expect(harness.sockets[0]?.closeCalls).toBe(1)
  })
})

import type { DeliveryCodexAppserver } from '../lib/delivery-spec.js'
import {
  JsonRpcSocketClient,
  defaultWebSocketFactory,
  describeError,
  resolveAuthToken,
  safeClose,
  type CodexWebSocketFactory,
  type JsonRpcResponse,
  type WebSocketLike,
} from './codex-appserver-rpc.js'
export type { WebSocketLike } from './codex-appserver-rpc.js'

type Json = unknown

export interface CodexAppserverDispatchDeps {
  env?: NodeJS.ProcessEnv
  webSocketFactory?: CodexWebSocketFactory
  turnStartConfirmTimeoutMs?: number
  wakeConfirmTimeoutMs?: number
  turnCompletionHoldTimeoutMs?: number
}

export type CodexAppserverDispatchResult =
  | {
      ok: true
      transport_used: 'codex-appserver'
      thread_id: string
    }
  | {
      error:
        | 'missing_auth_token'
        | 'codex_connect_failed'
        | 'codex_initialize_failed'
        | 'codex_resume_failed'
        | 'codex_turn_start_failed'
        | 'codex_turn_start_unconfirmed'
        | 'codex_wake_unconfirmed'
      detail?: unknown
      transport_used?: 'codex-appserver'
    }

async function requestStep(
  client: JsonRpcSocketClient,
  method: string,
  params: Json,
  options: { discardNotificationsOnResponse?: boolean } = {}
): Promise<
  | { ok: JsonRpcResponse }
  | {
      error:
        | 'codex_initialize_failed'
        | 'codex_resume_failed'
        | 'codex_turn_start_failed'
      detail: unknown
    }
> {
  try {
    const response = await client.request(method, params, options)
    if (response.error) {
      const mappedError =
        method === 'initialize'
          ? 'codex_initialize_failed'
          : method === 'thread/resume'
            ? 'codex_resume_failed'
            : 'codex_turn_start_failed'
      return { error: mappedError, detail: response.error }
    }
    return { ok: response }
  } catch (error) {
    const mappedError =
      method === 'initialize'
        ? 'codex_initialize_failed'
        : method === 'thread/resume'
          ? 'codex_resume_failed'
          : 'codex_turn_start_failed'
    return { error: mappedError, detail: describeError(error) }
  }
}

export async function dispatchCodexAppserverPoke(
  input: {
    delivery: DeliveryCodexAppserver
    content: string
  },
  deps: CodexAppserverDispatchDeps = {}
): Promise<CodexAppserverDispatchResult> {
  const authToken = resolveAuthToken(
    input.delivery.auth_token_ref,
    deps.env ?? process.env
  )
  if ('error' in authToken) return authToken

  const headers = authToken.ok === undefined
    ? undefined
    : { Authorization: `Bearer ${authToken.ok}` }

  let ws: WebSocketLike
  try {
    ws = (deps.webSocketFactory ?? defaultWebSocketFactory)({
      url: input.delivery.ws_url,
      headers,
    })
  } catch (error) {
    return {
      error: 'codex_connect_failed',
      detail: describeError(error),
      transport_used: 'codex-appserver',
    }
  }

  const client = new JsonRpcSocketClient(ws)
  let closeInFinally = true
  try {
    await client.waitForOpen()

    const init = await requestStep(client, 'initialize', {
      clientInfo: {
        name: 'cross-agent-teams-mcp',
        title: null,
        version: '0.1.0',
      },
      capabilities: {
        experimentalApi: true,
        optOutNotificationMethods: null,
      },
    })
    if ('error' in init) {
      return {
        error: init.error,
        detail: init.detail,
        transport_used: 'codex-appserver',
      }
    }

    client.notify('initialized')

    const resume = await requestStep(client, 'thread/resume', {
      threadId: input.delivery.thread_id,
      persistExtendedHistory: false,
    })
    if ('error' in resume) {
      return {
        error: resume.error,
        detail: resume.detail,
        transport_used: 'codex-appserver',
      }
    }

    const activeTurnId = extractActiveTurnId(resume.ok.result)
    client.discardNotifications()
    const turnMethod = activeTurnId === undefined ? 'turn/start' : 'turn/steer'
    const turnStart = await requestStep(
      client,
      turnMethod,
      {
        threadId: input.delivery.thread_id,
        input: [{ type: 'text', text: input.content, text_elements: [] }],
        ...(activeTurnId === undefined ? {} : { expectedTurnId: activeTurnId }),
      },
      { discardNotificationsOnResponse: activeTurnId !== undefined }
    )
    if ('error' in turnStart) {
      return {
        error: turnStart.error,
        detail: turnStart.detail,
        transport_used: 'codex-appserver',
      }
    }

    const turnId = activeTurnId ?? extractTurnId(turnStart.ok.result)
    if (turnId === undefined) {
      return {
        error: 'codex_turn_start_unconfirmed',
        detail: 'turn/start response missing turn id',
        transport_used: 'codex-appserver',
      }
    }

    if (activeTurnId === undefined) {
      try {
        await client.waitForNotification(
          (notification) => isMatchingTurnStarted(
            notification,
            input.delivery.thread_id,
            turnId
          ),
          deps.turnStartConfirmTimeoutMs ?? 5_000
        )
      } catch (error) {
        if (describeError(error) !== 'notification_timeout') {
          return {
            error: 'codex_connect_failed',
            detail: describeError(error),
            transport_used: 'codex-appserver',
          }
        }
        return {
          error: 'codex_turn_start_unconfirmed',
          detail: `turn/started notification timed out for ${turnId}`,
          transport_used: 'codex-appserver',
        }
      }
    }

    try {
      await client.waitForNotification(
        (notification) => isMatchingGetInboxCall(
          notification,
          input.delivery.thread_id,
          turnId
        ),
        deps.wakeConfirmTimeoutMs ?? 30_000
      )
    } catch (error) {
      if (describeError(error) !== 'notification_timeout') {
        return {
          error: 'codex_connect_failed',
          detail: describeError(error),
          transport_used: 'codex-appserver',
        }
      }
      return {
        error: 'codex_wake_unconfirmed',
        detail: `get_inbox tool call not observed for ${turnId}`,
        transport_used: 'codex-appserver',
      }
    }

    closeInFinally = false
    void client.waitForNotification(
      (notification) => isMatchingTurnEvent(
        notification,
        'turn/completed',
        input.delivery.thread_id,
        turnId
      ),
      deps.turnCompletionHoldTimeoutMs ?? 10 * 60_000
    ).catch(() => undefined).finally(() => safeClose(ws))

    return {
      ok: true,
      transport_used: 'codex-appserver',
      thread_id: input.delivery.thread_id,
    }
  } catch (error) {
    return {
      error: 'codex_connect_failed',
      detail: describeError(error),
      transport_used: 'codex-appserver',
    }
  } finally {
    if (closeInFinally) safeClose(ws)
  }
}

function extractTurnId(result: unknown): string | undefined {
  if (result === null || typeof result !== 'object') return undefined
  const turn = (result as Record<string, unknown>).turn
  if (turn === null || typeof turn !== 'object') return undefined
  const id = (turn as Record<string, unknown>).id
  return typeof id === 'string' && id.length > 0 ? id : undefined
}

function extractActiveTurnId(result: unknown): string | undefined {
  if (result === null || typeof result !== 'object') return undefined
  const thread = (result as Record<string, unknown>).thread
  if (thread === null || typeof thread !== 'object') return undefined
  const turns = (thread as Record<string, unknown>).turns
  if (!Array.isArray(turns)) return undefined
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    if (turn === null || typeof turn !== 'object') continue
    const row = turn as Record<string, unknown>
    if (row.status === 'inProgress' && typeof row.id === 'string') return row.id
  }
  return undefined
}

function isMatchingTurnStarted(
  notification: { method: string; params?: unknown },
  threadId: string,
  turnId: string
): boolean {
  return isMatchingTurnEvent(notification, 'turn/started', threadId, turnId)
}

function isMatchingTurnEvent(
  notification: { method: string; params?: unknown },
  method: string,
  threadId: string,
  turnId: string
): boolean {
  if (notification.method !== method) return false
  if (notification.params === null || typeof notification.params !== 'object') return false
  const params = notification.params as Record<string, unknown>
  const turn = params.turn
  if (turn === null || typeof turn !== 'object') return false
  return params.threadId === threadId && (turn as Record<string, unknown>).id === turnId
}

function isMatchingGetInboxCall(
  notification: { method: string; params?: unknown },
  threadId: string,
  turnId: string
): boolean {
  if (notification.method !== 'item/started') return false
  if (notification.params === null || typeof notification.params !== 'object') return false
  const params = notification.params as Record<string, unknown>
  const item = params.item
  if (item === null || typeof item !== 'object') return false
  const row = item as Record<string, unknown>
  return params.threadId === threadId
    && params.turnId === turnId
    && row.type === 'mcpToolCall'
    && row.tool === 'get_inbox'
}

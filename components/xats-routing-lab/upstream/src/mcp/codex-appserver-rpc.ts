type Json = unknown

interface JsonRpcRequest {
  jsonrpc: '2.0'
  id: number
  method: string
  params: Json
}

export interface JsonRpcNotification {
  jsonrpc: '2.0'
  method: string
  params?: Json
}

export interface JsonRpcResponse {
  jsonrpc: '2.0'
  id: number
  result?: Json
  error?: { code: number; message: string; data?: Json }
}

type MessageEventLike = { data: unknown }
type ErrorEventLike = { error?: unknown; message?: string }
type CloseEventLike = { code?: number; reason?: string }

export interface WebSocketLike {
  addEventListener(
    type: 'open' | 'message' | 'error' | 'close',
    listener: (event: unknown) => void
  ): void
  removeEventListener?(
    type: 'open' | 'message' | 'error' | 'close',
    listener: (event: unknown) => void
  ): void
  send(data: string): void
  close(): void
}

export interface CodexWebSocketFactoryArgs {
  url: string
  headers?: Record<string, string>
}

export type CodexWebSocketFactory = (
  args: CodexWebSocketFactoryArgs
) => WebSocketLike

type PendingResolver = {
  resolve: (value: JsonRpcResponse) => void
  reject: (reason?: unknown) => void
  discardNotificationsOnResponse: boolean
}

export function defaultWebSocketFactory(
  args: CodexWebSocketFactoryArgs
): WebSocketLike {
  const ctor = globalThis.WebSocket as unknown as new (
    url: string,
    options?: { headers?: Record<string, string> }
  ) => WebSocketLike
  return new ctor(
    args.url,
    args.headers === undefined ? undefined : { headers: args.headers }
  )
}

export function describeError(error: unknown): string {
  if (error instanceof Error && error.message.length > 0) return error.message
  if (typeof error === 'string' && error.length > 0) return error
  if (error && typeof error === 'object') {
    const record = error as Record<string, unknown>
    const message = record.message
    if (typeof message === 'string' && message.length > 0) return message
    const reason = record.reason
    if (typeof reason === 'string' && reason.length > 0) return reason
  }
  return String(error)
}

function closeDetail(event: CloseEventLike): string {
  const code = typeof event.code === 'number' ? event.code : 'unknown'
  const reason = typeof event.reason === 'string' && event.reason.length > 0
    ? event.reason
    : 'socket_closed'
  return `close ${code}: ${reason}`
}

function decodeMessageData(data: unknown): string {
  if (typeof data === 'string') return data
  if (data instanceof ArrayBuffer) {
    return new TextDecoder().decode(new Uint8Array(data))
  }
  if (ArrayBuffer.isView(data)) {
    return new TextDecoder().decode(data)
  }
  return String(data)
}

export function safeClose(ws: WebSocketLike): void {
  try {
    ws.close()
  } catch {
    return
  }
}

export function resolveAuthToken(
  authTokenRef: string | undefined,
  env: NodeJS.ProcessEnv
): { ok: string | undefined } | { error: 'missing_auth_token'; detail: { ref: string } } {
  if (authTokenRef === undefined) return { ok: undefined }
  const token = env[authTokenRef]?.trim()
  if (!token) {
    return {
      error: 'missing_auth_token',
      detail: { ref: authTokenRef },
    }
  }
  return { ok: token }
}

export class JsonRpcSocketClient {
  private nextId = 1
  private socketFailure: unknown
  private readonly pending = new Map<number, PendingResolver>()
  private readonly notifications: JsonRpcNotification[] = []
  private readonly notificationWaiters = new Set<{
    predicate: (notification: JsonRpcNotification) => boolean
    resolve: (notification: JsonRpcNotification) => void
    reject: (reason?: unknown) => void
  }>()
  private openState:
    | { kind: 'pending'; promise: Promise<void> }
    | { kind: 'open' }
    | { kind: 'failed'; error: unknown }

  constructor(private readonly ws: WebSocketLike) {
    this.openState = {
      kind: 'pending',
      promise: new Promise<void>((resolve, reject) => {
        const onOpen = () => {
          cleanup()
          this.openState = { kind: 'open' }
          resolve()
        }
        const onError = (event: unknown) => {
          cleanup()
          const detail = event as ErrorEventLike
          const error = detail.error ?? detail.message ?? 'websocket_error'
          this.openState = { kind: 'failed', error }
          reject(error)
        }
        const onClose = (event: unknown) => {
          cleanup()
          const closeEvent = event as CloseEventLike
          const error = closeDetail(closeEvent)
          this.openState = { kind: 'failed', error }
          reject(error)
        }
        const cleanup = () => {
          this.ws.removeEventListener?.('open', onOpen)
          this.ws.removeEventListener?.('error', onError)
          this.ws.removeEventListener?.('close', onClose)
        }
        this.ws.addEventListener('open', onOpen)
        this.ws.addEventListener('error', onError)
        this.ws.addEventListener('close', onClose)
      }),
    }

    this.ws.addEventListener('message', (event) => {
      let message: JsonRpcResponse | JsonRpcNotification
      try {
        message = JSON.parse(decodeMessageData((event as MessageEventLike).data))
      } catch {
        return
      }
      if ('id' in message && typeof message.id === 'number') {
        const pending = this.pending.get(message.id)
        if (!pending) return
        this.pending.delete(message.id)
        if (pending.discardNotificationsOnResponse) this.notifications.length = 0
        pending.resolve(message)
        return
      }
      if (!('method' in message) || typeof message.method !== 'string') return
      this.notifications.push(message)
      if (this.notifications.length > 100) this.notifications.shift()
      for (const waiter of this.notificationWaiters) {
        let matches = false
        try {
          matches = waiter.predicate(message)
        } catch {
          continue
        }
        if (!matches) continue
        this.notificationWaiters.delete(waiter)
        waiter.resolve(message)
      }
    })

    this.ws.addEventListener('error', (event) => {
      if (this.openState.kind !== 'open') return
      const detail = event as ErrorEventLike
      const error = detail.error ?? detail.message ?? 'websocket_error'
      this.socketFailure = error
      this.rejectAll(error)
    })

    this.ws.addEventListener('close', (event) => {
      if (this.openState.kind !== 'open') return
      const error = closeDetail(event as CloseEventLike)
      this.socketFailure = error
      this.rejectAll(error)
    })
  }

  async waitForOpen(): Promise<void> {
    if (this.openState.kind === 'open') return
    if (this.openState.kind === 'failed') throw this.openState.error
    await this.openState.promise
  }

  request(
    method: string,
    params: Json,
    options: { discardNotificationsOnResponse?: boolean } = {}
  ): Promise<JsonRpcResponse> {
    const id = this.nextId++
    const request: JsonRpcRequest = { jsonrpc: '2.0', id, method, params }
    return new Promise<JsonRpcResponse>((resolve, reject) => {
      this.pending.set(id, {
        resolve,
        reject,
        discardNotificationsOnResponse:
          options.discardNotificationsOnResponse ?? false,
      })
      try {
        this.ws.send(JSON.stringify(request))
      } catch (error) {
        this.pending.delete(id)
        reject(error)
      }
    })
  }

  notify(method: string, params?: Json): void {
    const notification: JsonRpcNotification = {
      jsonrpc: '2.0',
      method,
      ...(params === undefined ? {} : { params }),
    }
    this.ws.send(JSON.stringify(notification))
  }

  discardNotifications(): void {
    this.notifications.length = 0
  }

  async waitForNotification(
    predicate: (notification: JsonRpcNotification) => boolean,
    timeoutMs: number
  ): Promise<JsonRpcNotification> {
    const bufferedIndex = this.notifications.findIndex(predicate)
    if (bufferedIndex >= 0) return this.notifications.splice(bufferedIndex, 1)[0]!
    if (this.socketFailure !== undefined) throw this.socketFailure

    let waiter:
      | {
          predicate: (notification: JsonRpcNotification) => boolean
          resolve: (notification: JsonRpcNotification) => void
          reject: (reason?: unknown) => void
        }
      | undefined
    const notification = new Promise<JsonRpcNotification>((resolve, reject) => {
      waiter = { predicate, resolve, reject }
      this.notificationWaiters.add(waiter)
    })
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('notification_timeout')), timeoutMs)
    })
    try {
      return await Promise.race([notification, timeout])
    } finally {
      if (timer !== undefined) clearTimeout(timer)
      if (waiter !== undefined) this.notificationWaiters.delete(waiter)
    }
  }

  private rejectAll(error: unknown): void {
    const pending = [...this.pending.values()]
    this.pending.clear()
    for (const entry of pending) {
      entry.reject(error)
    }
    const waiters = [...this.notificationWaiters]
    this.notificationWaiters.clear()
    for (const waiter of waiters) {
      waiter.reject(error)
    }
  }
}

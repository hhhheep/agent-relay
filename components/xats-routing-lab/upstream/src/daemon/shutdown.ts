import type { FastifyInstance } from 'fastify'
import { releasePidFile } from './pid.js'

export interface WireShutdownOpts {
  graceMs?: number
  exit?: (code: number) => void
  // Called when the drain deadline expires (or graceMs <= 0), alongside the
  // main `app.server.closeAllConnections()`. Used to force-close auxiliary
  // listeners such as the loopback companion HTTP server.
  extraForceClose?: () => void
}

const DEFAULT_GRACE_MS = 5000

function resolveGraceMs(explicit: number | undefined): number {
  if (typeof explicit === 'number' && Number.isFinite(explicit)) {
    return explicit < 0 ? 0 : Math.floor(explicit)
  }
  const raw = process.env.XATS_SHUTDOWN_GRACE_MS
  if (raw === undefined || raw === '') return DEFAULT_GRACE_MS
  const n = Number(raw)
  if (!Number.isFinite(n)) return DEFAULT_GRACE_MS
  return n < 0 ? 0 : Math.floor(n)
}

export function wireShutdown(
  app: FastifyInstance,
  pidPath: string,
  opts: WireShutdownOpts = {}
): void {
  const graceMs = resolveGraceMs(opts.graceMs)
  const exit = opts.exit ?? ((code: number) => { process.exit(code) })
  const extraForceClose = opts.extraForceClose
  let shuttingDown = false

  const handler = (_signal: NodeJS.Signals): void => {
    if (shuttingDown) {
      releasePidFile(pidPath)
      exit(0)
      return
    }
    shuttingDown = true
    void runDrain(app, pidPath, graceMs, exit, extraForceClose)
  }
  process.on('SIGTERM', handler)
  process.on('SIGINT', handler)
}

function forceCloseAll(app: FastifyInstance, extra: (() => void) | undefined): void {
  try { app.server.closeAllConnections() } catch { /* best-effort */ }
  if (extra) {
    try { extra() } catch { /* best-effort */ }
  }
}

async function runDrain(
  app: FastifyInstance,
  pidPath: string,
  graceMs: number,
  exit: (code: number) => void,
  extraForceClose: (() => void) | undefined
): Promise<void> {
  if (graceMs <= 0) {
    forceCloseAll(app, extraForceClose)
    try { await app.close() } catch { /* ignore */ }
    releasePidFile(pidPath)
    exit(0)
    return
  }

  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<'timeout'>((resolve) => {
    timer = setTimeout(() => resolve('timeout'), graceMs)
    if (typeof timer.unref === 'function') timer.unref()
  })
  const closed = app.close().then(() => 'closed' as const).catch(() => 'closed' as const)

  const winner = await Promise.race([closed, deadline])
  if (winner === 'timeout') {
    forceCloseAll(app, extraForceClose)
    try { await app.close() } catch { /* ignore */ }
  }
  if (timer) clearTimeout(timer)
  releasePidFile(pidPath)
  exit(0)
}

import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { mkdtempSync, rmSync, existsSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { request as httpRequest } from 'node:http'
import { buildServer } from '../src/daemon/server.js'
import { wireShutdown } from '../src/daemon/shutdown.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-shutdown-'))

interface Harness {
  app: Awaited<ReturnType<typeof buildServer>>
  port: number
  pidPath: string
  exitCalls: number[]
  exitedAt: number | null
  whenExited: Promise<number>
}

async function boot(dir: string, graceMs?: number): Promise<Harness> {
  const dbPath = join(dir, 'data.db')
  const pidPath = join(dir, 'daemon.pid')
  writeFileSync(pidPath, JSON.stringify({ pid: process.pid, port: 0 }))
  const app = await buildServer({ dbPath })
  // Register a hanging route that simulates a long-lived SSE / MCP stream:
  // the handler never resolves, so Fastify's app.close() will wait for it
  // forever unless the shutdown handler force-closes the socket.
  app.get('/__longlived__', async (_req, reply) => {
    reply.raw.writeHead(200, { 'content-type': 'text/event-stream' })
    reply.raw.write(': hi\n\n')
    // Never end — emulate a long-lived stream.
    await new Promise(() => { /* hang forever */ })
  })
  await app.listen({ port: 0, host: '127.0.0.1' })
  const addr = app.server.address()
  const port = addr && typeof addr === 'object' ? addr.port : 0
  const exitCalls: number[] = []
  let resolveExit!: (code: number) => void
  const whenExited = new Promise<number>((r) => { resolveExit = r })
  let exitedAt: number | null = null
  const exit = (code: number): void => {
    exitCalls.push(code)
    if (exitedAt === null) {
      exitedAt = Date.now()
      resolveExit(code)
    }
  }
  wireShutdown(app, pidPath, { graceMs, exit })
  return {
    app,
    port,
    pidPath,
    exitCalls,
    get exitedAt() { return exitedAt },
    whenExited,
  } as Harness
}

// Open a long-lived GET against the hanging route registered in boot().
// The server-side handler never resolves, so Fastify's app.close() will
// wait indefinitely unless the shutdown handler force-closes the socket.
function openLongLivedStream(host: string, port: number): { destroy: () => void; firstByte: Promise<void> } {
  let req: ReturnType<typeof httpRequest> | undefined
  const firstByte = new Promise<void>((resolve, reject) => {
    req = httpRequest({
      host,
      port,
      method: 'GET',
      path: '/__longlived__',
    }, (res) => {
      res.on('data', () => resolve())
      res.on('error', () => { /* expected on force-close */ })
    })
    req.on('error', () => { /* expected on force-close */ })
    req.end()
    setTimeout(() => reject(new Error('long-lived stream never produced data')), 2000).unref?.()
  })
  return {
    destroy: () => { try { req?.destroy() } catch { /* best-effort */ } },
    firstByte,
  }
}

function removeShutdownListeners(): void {
  process.removeAllListeners('SIGTERM')
  process.removeAllListeners('SIGINT')
}

describe('graceful shutdown with drain deadline', () => {
  const dirs: string[] = []
  beforeEach(() => { removeShutdownListeners() })
  afterEach(() => {
    removeShutdownListeners()
    for (const d of dirs) rmSync(d, { recursive: true, force: true })
    dirs.length = 0
  })

  it('no long-lived clients: exits within 1 second and removes pid file', async () => {
    const dir = tmp(); dirs.push(dir)
    const h = await boot(dir, 5000)
    const start = Date.now()
    process.emit('SIGTERM' as NodeJS.Signals)
    await h.whenExited
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(1000)
    expect(h.exitCalls).toEqual([0])
    expect(existsSync(h.pidPath)).toBe(false)
  })

  it('long-lived in-flight client: exits within graceMs + 500ms via closeAllConnections', async () => {
    const dir = tmp(); dirs.push(dir)
    const graceMs = 500
    const h = await boot(dir, graceMs)
    const pending = openLongLivedStream('127.0.0.1', h.port)
    await pending.firstByte
    const start = Date.now()
    process.emit('SIGTERM' as NodeJS.Signals)
    await h.whenExited
    const elapsed = Date.now() - start
    expect(elapsed).toBeGreaterThanOrEqual(graceMs - 50)
    expect(elapsed).toBeLessThan(graceMs + 500)
    expect(h.exitCalls).toEqual([0])
    expect(existsSync(h.pidPath)).toBe(false)
    pending.destroy()
  })

  it('second SIGTERM during drain triggers fast exit (<200ms after 2nd signal) and removes pid', async () => {
    const dir = tmp(); dirs.push(dir)
    const graceMs = 5000
    const h = await boot(dir, graceMs)
    const pending = openLongLivedStream('127.0.0.1', h.port)
    await pending.firstByte
    process.emit('SIGTERM' as NodeJS.Signals)
    // Brief delay to ensure first handler is mid-drain
    await new Promise((r) => setTimeout(r, 50))
    const secondAt = Date.now()
    process.emit('SIGTERM' as NodeJS.Signals)
    await h.whenExited
    const elapsedAfterSecond = Date.now() - secondAt
    expect(elapsedAfterSecond).toBeLessThan(200)
    expect(h.exitCalls.length).toBeGreaterThanOrEqual(1)
    expect(h.exitCalls[0]).toBe(0)
    expect(existsSync(h.pidPath)).toBe(false)
    pending.destroy()
    // Let the first-signal drain finish in the background to avoid leaks.
    await new Promise((r) => setTimeout(r, 50))
  })

  it('XATS_SHUTDOWN_GRACE_MS=0 (graceMs=0) skips drain and force-closes immediately', async () => {
    const dir = tmp(); dirs.push(dir)
    const h = await boot(dir, 0)
    const pending = openLongLivedStream('127.0.0.1', h.port)
    await pending.firstByte
    const start = Date.now()
    process.emit('SIGTERM' as NodeJS.Signals)
    await h.whenExited
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(500)
    expect(h.exitCalls).toEqual([0])
    expect(existsSync(h.pidPath)).toBe(false)
    pending.destroy()
  })

  it('reads XATS_SHUTDOWN_GRACE_MS env var when graceMs not passed (negative clamps to 0)', async () => {
    const dir = tmp(); dirs.push(dir)
    const dbPath = join(dir, 'data.db')
    const pidPath = join(dir, 'daemon.pid')
    writeFileSync(pidPath, JSON.stringify({ pid: process.pid, port: 0 }))
    const app = await buildServer({ dbPath })
    app.get('/__longlived__', async (_req, reply) => {
      reply.raw.writeHead(200, { 'content-type': 'text/event-stream' })
      reply.raw.write(': hi\n\n')
      await new Promise(() => { /* hang */ })
    })
    await app.listen({ port: 0, host: '127.0.0.1' })
    const addr = app.server.address()
    const port = addr && typeof addr === 'object' ? addr.port : 0

    const original = process.env.XATS_SHUTDOWN_GRACE_MS
    process.env.XATS_SHUTDOWN_GRACE_MS = '-100'
    let exited = 0
    const whenExited = new Promise<void>((resolve) => {
      wireShutdown(app, pidPath, {
        exit: () => { exited++; resolve() },
      })
    })
    process.env.XATS_SHUTDOWN_GRACE_MS = original

    const pending = openLongLivedStream('127.0.0.1', port)
    await pending.firstByte
    const start = Date.now()
    process.emit('SIGTERM' as NodeJS.Signals)
    await whenExited
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(500)
    expect(exited).toBe(1)
    expect(existsSync(pidPath)).toBe(false)
    pending.destroy()
  })
})

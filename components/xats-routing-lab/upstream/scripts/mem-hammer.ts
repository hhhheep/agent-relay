#!/usr/bin/env tsx
// Memory leak hammer for the cross-agent-teams-mcp daemon.
//
// Spawns N MCP clients against a running daemon and calls `echo` on a loop
// to reproduce the OOM pattern observed when channel-proxy stdio shims keep
// hitting the daemon at 5 RPS each (200 ms healthcheck loop). Prints the
// hammer-side heap every 5 s. Daemon-side heap should be observed via
// Chrome DevTools attached to a `node --inspect=9229 dist/cli.js daemon` run.
//
// Usage:
//   tsx scripts/mem-hammer.ts \
//     [--url http://127.0.0.1:9100/mcp] \
//     [--clients 5] \
//     [--interval-ms 100] \
//     [--duration-s 0]   # 0 = run until ctrl-c
//
// Suggested workflow:
//   Terminal A (daemon, observable heap):
//     pnpm build
//     node --inspect=9229 --max-old-space-size=2048 \
//       ./dist/cli.js daemon --port 9100
//   Terminal B (Chrome): chrome://inspect → Inspect → Memory tab → take
//     snapshots at t=0, t=5min, t=10min and diff them.
//   Terminal C (this hammer):
//     tsx scripts/mem-hammer.ts --clients 5 --interval-ms 100

import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'

interface Args {
  url: string
  clients: number
  intervalMs: number
  durationS: number
}

function parseArgs(argv: string[]): Args {
  const out: Args = {
    url: 'http://127.0.0.1:9100/mcp',
    clients: 5,
    intervalMs: 100,
    durationS: 0,
  }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    const v = argv[i + 1]
    if (k === '--url' && v) { out.url = v; i++ }
    else if (k === '--clients' && v) { out.clients = Number(v); i++ }
    else if (k === '--interval-ms' && v) { out.intervalMs = Number(v); i++ }
    else if (k === '--duration-s' && v) { out.durationS = Number(v); i++ }
  }
  return out
}

interface ClientCtx {
  id: number
  client: Client
  transport: StreamableHTTPClientTransport
  callCount: number
  errorCount: number
}

async function spawnClient(id: number, url: string): Promise<ClientCtx> {
  const transport = new StreamableHTTPClientTransport(new URL(url))
  const client = new Client({ name: `mem-hammer-${id}`, version: '0.0.0' })
  await client.connect(transport)
  return { id, client, transport, callCount: 0, errorCount: 0 }
}

async function runClientLoop(
  ctx: ClientCtx,
  intervalMs: number,
  stopSignal: { stopped: boolean }
): Promise<void> {
  while (!stopSignal.stopped) {
    try {
      await ctx.client.callTool({ name: 'echo', arguments: { msg: 'hb' } })
      ctx.callCount += 1
    } catch {
      ctx.errorCount += 1
    }
    if (stopSignal.stopped) break
    await new Promise(r => setTimeout(r, intervalMs))
  }
}

function fmtMb(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2))
  // eslint-disable-next-line no-console
  console.log(
    `[hammer] url=${args.url} clients=${args.clients} interval=${args.intervalMs}ms duration=${args.durationS}s`
  )

  const ctxs: ClientCtx[] = []
  for (let i = 0; i < args.clients; i++) {
    try {
      const ctx = await spawnClient(i, args.url)
      ctxs.push(ctx)
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(`[hammer] client ${i} connect failed:`, err instanceof Error ? err.message : err)
    }
  }
  if (ctxs.length === 0) {
    // eslint-disable-next-line no-console
    console.error('[hammer] no clients connected; is daemon up?')
    process.exit(1)
  }
  // eslint-disable-next-line no-console
  console.log(`[hammer] connected clients: ${ctxs.length}`)

  const stopSignal = { stopped: false }
  const startMs = Date.now()
  const startMem = process.memoryUsage()
  // eslint-disable-next-line no-console
  console.log(
    `[hammer] t=0s  heapUsed=${fmtMb(startMem.heapUsed)} rss=${fmtMb(startMem.rss)}`
  )

  const reporter = setInterval(() => {
    const mem = process.memoryUsage()
    const calls = ctxs.reduce((acc, c) => acc + c.callCount, 0)
    const errors = ctxs.reduce((acc, c) => acc + c.errorCount, 0)
    const elapsedS = Math.round((Date.now() - startMs) / 1000)
    const rps = elapsedS > 0 ? (calls / elapsedS).toFixed(1) : '0'
    // eslint-disable-next-line no-console
    console.log(
      `[hammer] t=${elapsedS}s heapUsed=${fmtMb(mem.heapUsed)} rss=${fmtMb(mem.rss)} ` +
      `calls=${calls} errors=${errors} rps=${rps}`
    )
  }, 5000)
  reporter.unref?.()

  const stop = async (): Promise<void> => {
    if (stopSignal.stopped) return
    stopSignal.stopped = true
    clearInterval(reporter)
    const mem = process.memoryUsage()
    const calls = ctxs.reduce((acc, c) => acc + c.callCount, 0)
    const errors = ctxs.reduce((acc, c) => acc + c.errorCount, 0)
    const elapsedS = Math.round((Date.now() - startMs) / 1000)
    // eslint-disable-next-line no-console
    console.log(
      `[hammer] FINAL t=${elapsedS}s heapUsed=${fmtMb(mem.heapUsed)} rss=${fmtMb(mem.rss)} ` +
      `calls=${calls} errors=${errors}`
    )
    await Promise.all(ctxs.map(async (c) => {
      try { await c.client.close() } catch { /* ignore */ }
      try { await c.transport.close() } catch { /* ignore */ }
    }))
    process.exit(0)
  }

  process.on('SIGINT', () => { void stop() })
  process.on('SIGTERM', () => { void stop() })

  if (args.durationS > 0) {
    setTimeout(() => { void stop() }, args.durationS * 1000)
  }

  await Promise.all(ctxs.map(c => runClientLoop(c, args.intervalMs, stopSignal)))
}

void main()

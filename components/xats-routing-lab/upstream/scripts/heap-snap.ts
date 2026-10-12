#!/usr/bin/env tsx
// Take a heap snapshot from a running Node process via the V8 inspector
// protocol. Connect by attaching to ws://127.0.0.1:<port>/<id>.
//
// Usage:
//   tsx scripts/heap-snap.ts [--inspector-port 9229] [--out heap-<ts>.heapsnapshot]
//
// Output is a `.heapsnapshot` JSON file. Open it in Chrome DevTools (Memory
// tab → Load) to inspect, OR feed multiple snapshots into the bundled
// analyzer (TODO) for diff-based leak attribution.

import * as http from 'node:http'
import { createWriteStream, statSync } from 'node:fs'

interface Args {
  inspectorPort: number
  out: string
}

function parseArgs(argv: string[]): Args {
  const ts = new Date().toISOString().replace(/[:.]/g, '-')
  const out: Args = {
    inspectorPort: 9229,
    out: `heap-${ts}.heapsnapshot`,
  }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    const v = argv[i + 1]
    if (k === '--inspector-port' && v) { out.inspectorPort = Number(v); i++ }
    else if (k === '--out' && v) { out.out = v; i++ }
  }
  return out
}

interface InspectorTarget {
  webSocketDebuggerUrl: string
}

function fetchJson(port: number): Promise<InspectorTarget[]> {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: '/json' }, (res) => {
      let body = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { body += chunk })
      res.on('end', () => {
        try { resolve(JSON.parse(body) as InspectorTarget[]) }
        catch (err) { reject(err) }
      })
    })
    req.on('error', reject)
    req.end()
  })
}

interface CdpMessage {
  id?: number
  method?: string
  params?: Record<string, unknown>
  result?: unknown
  error?: { code: number; message: string }
}

async function takeSnapshot(wsUrl: string, outFile: string): Promise<void> {
  // eslint-disable-next-line no-console
  console.log(`[heap-snap] connecting ${wsUrl}`)
  const ws = new WebSocket(wsUrl)
  await new Promise<void>((resolve, reject) => {
    ws.addEventListener('open', () => resolve(), { once: true })
    ws.addEventListener('error', (ev) => reject(new Error(`ws error: ${String(ev)}`)), { once: true })
  })
  // eslint-disable-next-line no-console
  console.log('[heap-snap] connected, requesting takeHeapSnapshot...')

  const fileStream = createWriteStream(outFile, { encoding: 'utf8' })
  let chunkCount = 0
  let totalBytes = 0
  let nextId = 1
  let progressLast = 0
  const pending = new Map<number, { method: string; resolve: () => void; reject: (err: Error) => void }>()

  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ev.data.toString()) as CdpMessage
    if (msg.method === 'HeapProfiler.addHeapSnapshotChunk') {
      const chunk = (msg.params as { chunk?: string } | undefined)?.chunk ?? ''
      fileStream.write(chunk)
      chunkCount += 1
      totalBytes += chunk.length
      return
    }
    if (msg.method === 'HeapProfiler.reportHeapSnapshotProgress') {
      const p = msg.params as { done?: number; total?: number; finished?: boolean }
      if (typeof p.done === 'number' && typeof p.total === 'number' && p.total > 0) {
        const pct = Math.floor((p.done / p.total) * 100)
        if (pct - progressLast >= 10) {
          progressLast = pct
          // eslint-disable-next-line no-console
          console.log(`[heap-snap] progress ${pct}%${p.finished ? ' (finalizing)' : ''}`)
        }
      }
      return
    }
    if (msg.id !== undefined) {
      const entry = pending.get(msg.id)
      if (!entry) return
      pending.delete(msg.id)
      if (msg.error) entry.reject(new Error(`CDP ${entry.method} error ${msg.error.code}: ${msg.error.message}`))
      else entry.resolve()
    }
  })

  const call = (method: string, params: Record<string, unknown> = {}): Promise<void> => {
    const id = nextId++
    return new Promise<void>((resolve, reject) => {
      pending.set(id, { method, resolve, reject })
      ws.send(JSON.stringify({ id, method, params }))
    })
  }

  await call('HeapProfiler.enable')
  await call('HeapProfiler.takeHeapSnapshot', { reportProgress: true, captureNumericValue: false })

  ws.close()
  await new Promise<void>((resolve, reject) => {
    fileStream.end((err?: Error | null) => {
      if (err) reject(err); else resolve()
    })
  })
  const stat = statSync(outFile)
  // eslint-disable-next-line no-console
  console.log(
    `[heap-snap] received ${chunkCount} chunks, ${totalBytes} bytes streamed; ` +
    `wrote ${outFile} (${(stat.size / 1024 / 1024).toFixed(1)} MB)`
  )
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2))
  const targets = await fetchJson(args.inspectorPort)
  if (targets.length === 0) {
    // eslint-disable-next-line no-console
    console.error(`[heap-snap] no inspector targets at port ${args.inspectorPort}`)
    process.exit(1)
  }
  await takeSnapshot(targets[0].webSocketDebuggerUrl, args.out)
}

void main()

#!/usr/bin/env tsx
// Diff two .heapsnapshot files and print the constructors with the biggest
// growth in count and self_size between baseline and after. Used to identify
// what's leaking under hammer load.
//
// Usage:
//   tsx scripts/heap-diff.ts <baseline.heapsnapshot> <after.heapsnapshot>

import { readFileSync } from 'node:fs'

interface SnapshotMeta {
  node_fields: string[]
  node_types: [string[], ...unknown[]]
  edge_fields: string[]
  edge_types: [string[], ...unknown[]]
}

interface RawSnapshot {
  snapshot: { meta: SnapshotMeta; node_count: number; edge_count: number }
  nodes: number[]
  edges: number[]
  strings: string[]
}

interface Aggregate {
  count: number
  selfSize: number
}

function load(path: string): RawSnapshot {
  // eslint-disable-next-line no-console
  console.log(`[heap-diff] reading ${path}`)
  const raw = readFileSync(path, 'utf8')
  // eslint-disable-next-line no-console
  console.log(`[heap-diff] parsing ${(raw.length / 1024 / 1024).toFixed(1)} MB JSON`)
  return JSON.parse(raw) as RawSnapshot
}

function aggregate(snap: RawSnapshot): Map<string, Aggregate> {
  const fields = snap.snapshot.meta.node_fields
  const typeIdx = fields.indexOf('type')
  const nameIdx = fields.indexOf('name')
  const sizeIdx = fields.indexOf('self_size')
  const stride = fields.length
  const nodeTypes = snap.snapshot.meta.node_types[0]
  const objectTypeId = nodeTypes.indexOf('object')
  const closureTypeId = nodeTypes.indexOf('closure')
  const arrayTypeId = nodeTypes.indexOf('array')
  const stringTypeId = nodeTypes.indexOf('string')
  const concatStringTypeId = nodeTypes.indexOf('concatenated string')
  const slicedStringTypeId = nodeTypes.indexOf('sliced string')

  const map = new Map<string, Aggregate>()
  const nodes = snap.nodes
  const total = nodes.length
  for (let i = 0; i < total; i += stride) {
    const t = nodes[i + typeIdx]
    const nameId = nodes[i + nameIdx]
    const size = nodes[i + sizeIdx]
    let bucket: string
    if (t === objectTypeId) {
      bucket = `object: ${snap.strings[nameId] ?? ''}`
    } else if (t === closureTypeId) {
      bucket = `closure: ${snap.strings[nameId] ?? ''}`
    } else if (t === arrayTypeId) {
      bucket = '(array)'
    } else if (t === stringTypeId || t === concatStringTypeId || t === slicedStringTypeId) {
      bucket = '(string)'
    } else {
      bucket = `<${nodeTypes[t] ?? 'unknown'}>`
    }
    const cur = map.get(bucket) ?? { count: 0, selfSize: 0 }
    cur.count += 1
    cur.selfSize += size
    map.set(bucket, cur)
  }
  return map
}

function fmtKb(n: number): string {
  return (n / 1024).toFixed(1) + ' KB'
}

function main(): void {
  const args = process.argv.slice(2)
  if (args.length !== 2) {
    // eslint-disable-next-line no-console
    console.error('usage: heap-diff <baseline> <after>')
    process.exit(1)
  }
  const a = load(args[0])
  const b = load(args[1])
  const agA = aggregate(a)
  const agB = aggregate(b)
  // eslint-disable-next-line no-console
  console.log(`[heap-diff] baseline buckets=${agA.size} after buckets=${agB.size}`)

  interface DiffRow { bucket: string; count: number; selfSize: number; baseCount: number; baseSize: number }
  const rows: DiffRow[] = []
  const allKeys = new Set([...agA.keys(), ...agB.keys()])
  for (const k of allKeys) {
    const av = agA.get(k) ?? { count: 0, selfSize: 0 }
    const bv = agB.get(k) ?? { count: 0, selfSize: 0 }
    rows.push({
      bucket: k,
      count: bv.count - av.count,
      selfSize: bv.selfSize - av.selfSize,
      baseCount: av.count,
      baseSize: av.selfSize,
    })
  }

  rows.sort((x, y) => y.selfSize - x.selfSize)
  // eslint-disable-next-line no-console
  console.log('\n=== top 30 buckets by self_size DELTA (after - baseline) ===')
  // eslint-disable-next-line no-console
  console.log('Δsize        Δcount   base#  base_size   bucket')
  for (let i = 0; i < Math.min(30, rows.length); i++) {
    const r = rows[i]
    if (r.selfSize <= 0) break
    // eslint-disable-next-line no-console
    console.log(
      `${fmtKb(r.selfSize).padStart(11)} ${String(r.count).padStart(8)} ` +
      `${String(r.baseCount).padStart(7)} ${fmtKb(r.baseSize).padStart(10)}   ${r.bucket}`
    )
  }

  rows.sort((x, y) => y.count - x.count)
  // eslint-disable-next-line no-console
  console.log('\n=== top 30 buckets by count DELTA (after - baseline) ===')
  // eslint-disable-next-line no-console
  console.log('Δcount       Δsize       base#  base_size   bucket')
  for (let i = 0; i < Math.min(30, rows.length); i++) {
    const r = rows[i]
    if (r.count <= 0) break
    // eslint-disable-next-line no-console
    console.log(
      `${String(r.count).padStart(8)} ${fmtKb(r.selfSize).padStart(11)} ` +
      `${String(r.baseCount).padStart(7)} ${fmtKb(r.baseSize).padStart(10)}   ${r.bucket}`
    )
  }
}

main()

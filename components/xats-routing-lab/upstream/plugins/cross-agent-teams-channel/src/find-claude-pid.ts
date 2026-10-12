import { execFileSync } from 'node:child_process'

const MAX_HOPS = 8

interface PsRow {
  ppid: number
  cmd: string
}

export function readPsRow(pid: number): PsRow | null {
  try {
    const out = execFileSync('ps', ['-o', 'ppid=,args=', '-p', String(pid)], {
      encoding: 'utf8',
      timeout: 1000,
      stdio: ['ignore', 'pipe', 'ignore']
    })
    const trimmed = out.trim()
    if (!trimmed) return null
    const m = /^\s*(\d+)\s+(.*)$/.exec(trimmed)
    if (!m) return null
    return { ppid: parseInt(m[1], 10), cmd: m[2] }
  } catch {
    return null
  }
}

export function isClaudeCmd(cmd: string): boolean {
  const first = cmd.trim().split(/\s+/)[0]
  if (!first) return false
  const base = first.replace(/^.*\//, '')
  return base === 'claude'
}

export function findClaudeUiPid(
  startPpid: number = process.ppid,
  reader: (pid: number) => PsRow | null = readPsRow
): number {
  let pid = startPpid
  for (let i = 0; i < MAX_HOPS; i++) {
    const row = reader(pid)
    if (!row) break
    if (isClaudeCmd(row.cmd)) return pid
    if (row.ppid <= 1 || row.ppid === pid) break
    pid = row.ppid
  }
  return startPpid
}

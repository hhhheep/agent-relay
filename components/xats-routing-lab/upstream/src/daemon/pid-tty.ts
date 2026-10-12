import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const PS_LIST_TIMEOUT_MS = 3_000

export function normalizeTty(raw: string | undefined): string | undefined {
  const value = raw?.trim()
  if (!value) return undefined
  const normalized = value.replace(/^\/dev\//, '')
  if (!normalized || normalized === '?') return undefined
  return normalized
}

export async function readPidInfo(
  execLike: typeof execFile,
  pid: number
): Promise<{ found: boolean; tty?: string; command?: string }> {
  const exec = promisify(execLike)
  try {
    const { stdout } = await exec(
      'ps',
      ['-p', String(pid), '-o', 'tty=,command='],
      { timeout: PS_LIST_TIMEOUT_MS }
    )
    const line = stdout
      .split('\n')
      .map(value => value.trim())
      .find(Boolean)
    if (!line) return { found: false }
    const match = line.match(/^(\S+)\s+(.*)$/)
    if (!match) return { found: false }
    return {
      found: true,
      tty: normalizeTty(match[1]),
      command: match[2]?.trim(),
    }
  } catch {
    return { found: false }
  }
}

/** Controlling tty of a live pid, normalized like the tmux pane tty column. */
export async function readPidTty(
  pid: number,
  execLike: typeof execFile = execFile
): Promise<string | null> {
  const info = await readPidInfo(execLike, pid)
  return info.tty ?? null
}

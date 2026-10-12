import { capturePaneTail as _capture } from '../daemon/tmux-cli.js'

const DEFAULT_QUIET_MS = 2000
const GUARD_TAIL_LINES = 8

type CaptureFn = (paneId: string, lines?: number) => Promise<string>

let _captureImpl: CaptureFn = _capture

export function __setCapturePaneTail(fn: CaptureFn): void {
  _captureImpl = fn
}

export function __resetCapturePaneTail(): void {
  _captureImpl = _capture
}

export function resolveQuietMs(opt?: number): number {
  if (typeof opt === 'number' && Number.isInteger(opt) && opt > 0) return opt
  const raw = process.env.POKE_QUIET_MS
  if (raw === undefined) return DEFAULT_QUIET_MS
  const n = Number(raw)
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_QUIET_MS
}

export async function runQuietGuard(paneId: string, quietMs?: number): Promise<'pass' | 'fail'> {
  const ms = resolveQuietMs(quietMs)
  const before = await _captureImpl(paneId, GUARD_TAIL_LINES)
  await new Promise(r => setTimeout(r, ms))
  const after = await _captureImpl(paneId, GUARD_TAIL_LINES)
  return before === after ? 'pass' : 'fail'
}

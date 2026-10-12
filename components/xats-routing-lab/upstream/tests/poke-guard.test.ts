import { describe, it, expect, afterEach } from 'vitest'
import {
  runQuietGuard,
  resolveQuietMs,
  __setCapturePaneTail,
  __resetCapturePaneTail
} from '../src/mcp/poke-guard.js'

describe('runQuietGuard', () => {
  const origEnv = process.env.POKE_QUIET_MS
  afterEach(() => {
    __resetCapturePaneTail()
    if (origEnv === undefined) delete process.env.POKE_QUIET_MS
    else process.env.POKE_QUIET_MS = origEnv
  })

  it('returns pass when captures match (idle pane)', async () => {
    __setCapturePaneTail(async () => 'stable tail content')
    expect(await runQuietGuard('%42', 50)).toBe('pass')
  })

  it('returns fail when captures differ (active pane)', async () => {
    let n = 0
    __setCapturePaneTail(async () => `tail-${n++}`)
    expect(await runQuietGuard('%42', 50)).toBe('fail')
  })

  it('resolveQuietMs returns ENV override when valid positive int', () => {
    process.env.POKE_QUIET_MS = '100'
    expect(resolveQuietMs()).toBe(100)
  })

  it('resolveQuietMs returns default on invalid env', () => {
    process.env.POKE_QUIET_MS = 'not-a-number'
    expect(resolveQuietMs()).toBe(2000)
  })

  it('resolveQuietMs returns default when env unset', () => {
    delete process.env.POKE_QUIET_MS
    expect(resolveQuietMs()).toBe(2000)
  })

  it('resolveQuietMs honors explicit positive arg over env', () => {
    process.env.POKE_QUIET_MS = '5000'
    expect(resolveQuietMs(123)).toBe(123)
  })
})

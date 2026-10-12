import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SseFanout, type SseSink } from '../src/daemon/sse-fanout.js'

describe('SseFanout heartbeat', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('ticks on every attached sink at the configured interval', () => {
    const fanout = new SseFanout({ heartbeatIntervalMs: 100 })
    const sink: SseSink = {
      sendHeartbeat: vi.fn(),
      close: vi.fn()
    }
    fanout.attach('sess-A', 'default', sink)
    vi.advanceTimersByTime(250)
    expect((sink.sendHeartbeat as any).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('stops ticker when last sink detaches', () => {
    const fanout = new SseFanout({ heartbeatIntervalMs: 100 })
    const sink: SseSink = { sendHeartbeat: vi.fn(), close: vi.fn() }
    fanout.attach('sess-A', 'default', sink)
    vi.advanceTimersByTime(150)
    const before = (sink.sendHeartbeat as any).mock.calls.length
    fanout.detach('sess-A')
    vi.advanceTimersByTime(500)
    const after = (sink.sendHeartbeat as any).mock.calls.length
    expect(after).toBe(before)
  })
})

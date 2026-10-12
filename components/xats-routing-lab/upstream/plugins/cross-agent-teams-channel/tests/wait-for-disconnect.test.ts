import { describe, it, expect, vi, afterEach } from 'vitest'
import { waitForDisconnect } from '../src/daemon-client.js'

describe('waitForDisconnect heartbeat default', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('default heartbeat interval is 30 seconds (consecutive echo calls spaced 30 s apart)', async () => {
    vi.useFakeTimers()
    const ts: number[] = []
    const seq = {
      client: {
        callTool: async () => {
          ts.push(Date.now())
          return { content: [{ type: 'text' as const, text: 'hb' }] }
        }
      },
      transport: { onclose: undefined as (() => void) | undefined }
    }
    let stop = false
    const promise = waitForDisconnect(seq, { shouldStop: () => stop })

    // Advance 30 s twice — should produce exactly 2 echo calls under default.
    await vi.advanceTimersByTimeAsync(30_000)
    await vi.advanceTimersByTimeAsync(30_000)

    stop = true
    // Push the loop past one more setTimeout boundary so it observes shouldStop.
    await vi.advanceTimersByTimeAsync(30_000)
    await promise

    expect(ts.length).toBe(2)
    expect(ts[1] - ts[0]).toBeGreaterThanOrEqual(29_000)
    expect(ts[1] - ts[0]).toBeLessThanOrEqual(31_000)
  })

  it('test override of heartbeat interval is honoured', async () => {
    vi.useFakeTimers()
    const ts: number[] = []
    const seq = {
      client: {
        callTool: async () => {
          ts.push(Date.now())
          return { content: [{ type: 'text' as const, text: 'hb' }] }
        }
      },
      transport: { onclose: undefined as (() => void) | undefined }
    }
    let stop = false
    const promise = waitForDisconnect(seq, { healthCheckIntervalMs: 100, shouldStop: () => stop })

    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(100)

    stop = true
    await vi.advanceTimersByTimeAsync(100)
    await promise

    expect(ts.length).toBe(3)
  })

  it('echo failure during heartbeat triggers reconnect (returns immediately on rejection)', async () => {
    let n = 0
    const seq = {
      client: {
        callTool: async () => {
          n++
          if (n === 1) return { content: [{ type: 'text' as const, text: 'hb' }] }
          throw new Error('echo failed')
        }
      },
      transport: { onclose: undefined as (() => void) | undefined }
    }
    const start = Date.now()
    await waitForDisconnect(seq, { healthCheckIntervalMs: 50 })
    const elapsed = Date.now() - start
    expect(n).toBe(2)
    expect(elapsed).toBeLessThan(500)
  }, 5000)

  it('transport.onclose during the wait wakes the loop immediately (does not block until interval)', async () => {
    const seq = {
      client: {
        callTool: async () => ({ content: [{ type: 'text' as const, text: 'hb' }] })
      },
      transport: { onclose: undefined as (() => void) | undefined }
    }
    const promise = waitForDisconnect(seq, { healthCheckIntervalMs: 30_000 })
    // Wait one tick so waitForDisconnect installs its onclose handler.
    await new Promise(r => setImmediate(r))
    const start = Date.now()
    seq.transport.onclose?.()
    await promise
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(500)
  }, 5000)
})

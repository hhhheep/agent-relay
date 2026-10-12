import { describe, it, expect } from 'vitest'
import { SseFanout, type SseSink } from '../src/daemon/sse-fanout.js'

interface TrackedSink extends SseSink {
  closed: boolean
  heartbeats: number
}

function makeSink(): TrackedSink {
  const sink: TrackedSink = {
    closed: false,
    heartbeats: 0,
    sendHeartbeat() { sink.heartbeats += 1 },
    close() { sink.closed = true }
  }
  return sink
}

describe('SseFanout attach-replace semantics', () => {
  it('re-attach under same agent_id detaches previous sink and replaces it', () => {
    const f = new SseFanout({ heartbeatIntervalMs: 60_000 })
    const a = makeSink()
    const b = makeSink()
    f.attach('agent-X', 'default', a)
    f.attach('agent-X', 'default', b)
    expect(a.closed).toBe(true)
    expect(b.closed).toBe(false)
    const peek = f.peek()
    expect(peek).toEqual([{ agent_id: 'agent-X', team: 'default' }])
    f.stopAll()
  })
})

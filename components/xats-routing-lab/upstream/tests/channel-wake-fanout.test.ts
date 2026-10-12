import { describe, it, expect } from 'vitest'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'

describe('ChannelWakeFanout', () => {
  it('attach + send fans out only to the matched sink', () => {
    const fanout = new ChannelWakeFanout()
    const calls1: unknown[] = []
    const calls2: unknown[] = []
    fanout.attach('csid-1', (p) => calls1.push(p), 'sess-A')
    fanout.attach('csid-2', (p) => calls2.push(p), 'sess-B')
    fanout.send('csid-1', { foo: 1 })
    expect(calls1).toEqual([{ foo: 1 }])
    expect(calls2).toEqual([])
  })

  it('detach removes the sink', () => {
    const fanout = new ChannelWakeFanout()
    const calls: unknown[] = []
    fanout.attach('csid-1', (p) => calls.push(p), 'sess-A')
    fanout.detach('csid-1')
    fanout.send('csid-1', { x: 1 })
    expect(calls).toEqual([])
  })

  it('re-subscription replaces prior sink', () => {
    const fanout = new ChannelWakeFanout()
    const callsA: unknown[] = []
    const callsB: unknown[] = []
    fanout.attach('csid-1', (p) => callsA.push(p), 'sess-A')
    fanout.attach('csid-1', (p) => callsB.push(p), 'sess-B')
    fanout.send('csid-1', { z: 1 })
    expect(callsA).toEqual([])
    expect(callsB).toEqual([{ z: 1 }])
  })

  it('detachBySession removes all sinks owned by the session', () => {
    const fanout = new ChannelWakeFanout()
    const calls1: unknown[] = []
    const calls2: unknown[] = []
    const callsOther: unknown[] = []
    fanout.attach('csid-1', (p) => calls1.push(p), 'sess-P')
    fanout.attach('csid-2', (p) => calls2.push(p), 'sess-P')
    fanout.attach('csid-3', (p) => callsOther.push(p), 'sess-Q')
    fanout.detachBySession('sess-P')
    fanout.send('csid-1', { a: 1 })
    fanout.send('csid-2', { b: 1 })
    fanout.send('csid-3', { c: 1 })
    expect(calls1).toEqual([])
    expect(calls2).toEqual([])
    expect(callsOther).toEqual([{ c: 1 }])
  })

  it('has() reports presence', () => {
    const fanout = new ChannelWakeFanout()
    expect(fanout.has('csid-1')).toBe(false)
    fanout.attach('csid-1', () => {}, 'sess-A')
    expect(fanout.has('csid-1')).toBe(true)
    fanout.detach('csid-1')
    expect(fanout.has('csid-1')).toBe(false)
  })
})

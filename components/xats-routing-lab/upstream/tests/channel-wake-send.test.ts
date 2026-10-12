import { describe, it, expect } from 'vitest'
import { ChannelWakeFanout } from '../src/daemon/channel-wake-fanout.js'
import { sendChannelWake } from '../src/daemon/channel-wake-send.js'

describe('sendChannelWake', () => {
  it('emits notifications/channel_wake with params { content, meta }', () => {
    const fanout = new ChannelWakeFanout()
    const payloads: unknown[] = []
    fanout.attach('csid-abc', (p) => payloads.push(p), 'sess-A')
    const res = sendChannelWake(fanout, 'csid-abc', {
      content: 'you have 3 unread',
      meta: { message_count: '3', latest_sender: 'alice' }
    })
    expect(res).toEqual({ ok: true })
    expect(payloads).toEqual([
      {
        jsonrpc: '2.0',
        method: 'notifications/channel_wake',
        params: {
          content: 'you have 3 unread',
          meta: { message_count: '3', latest_sender: 'alice' }
        }
      }
    ])
  })

  it('drops meta keys not matching /^[A-Za-z0-9_]+$/', () => {
    const fanout = new ChannelWakeFanout()
    const payloads: Array<{ params: { meta: Record<string, string> } }> = []
    fanout.attach('csid-abc', (p) => payloads.push(p as { params: { meta: Record<string, string> } }), 'sess-A')
    sendChannelWake(fanout, 'csid-abc', {
      content: 'hi',
      meta: { message_count: '3', 'bad-key': 'oops', 'another.bad': 'x' }
    })
    expect(payloads).toHaveLength(1)
    expect(payloads[0].params.meta).toEqual({ message_count: '3' })
  })

  it('returns { ok: false, reason: "no_subscriber" } with no emit when no sink attached', () => {
    const fanout = new ChannelWakeFanout()
    const payloads: unknown[] = []
    fanout.attach('csid-other', (p) => payloads.push(p), 'sess-A')
    const res = sendChannelWake(fanout, 'csid-none', { content: 'x', meta: {} })
    expect(res).toEqual({ ok: false, reason: 'no_subscriber' })
    expect(payloads).toEqual([])
  })
})

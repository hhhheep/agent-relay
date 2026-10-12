import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { createProxyServer, relayChannelWake } from '../src/proxy.js'

describe('channel proxy relay', () => {
  it('relays channel_wake params as notifications/claude/channel to the host', async () => {
    const server = createProxyServer()
    const [clientT, serverT] = InMemoryTransport.createLinkedPair()
    await server.connect(serverT)
    const host = new Client({ name: 'fake-claude-code', version: '0.0.0' })
    const received: Array<{ method: string; params: unknown }> = []
    host.fallbackNotificationHandler = async (n) => {
      received.push({ method: n.method, params: n.params })
    }
    await host.connect(clientT)

    relayChannelWake(server, {
      content: 'hi',
      meta: { message_count: '3' }
    })
    // Allow the notification to propagate.
    await new Promise(r => setTimeout(r, 50))

    expect(received).toHaveLength(1)
    expect(received[0].method).toBe('notifications/claude/channel')
    expect(received[0].params).toEqual({ content: 'hi', meta: { message_count: '3' } })

    await host.close()
    await server.close()
  })

  it('relay does not throw when host transport is already closed', async () => {
    const server = createProxyServer()
    const [clientT, serverT] = InMemoryTransport.createLinkedPair()
    await server.connect(serverT)
    const host = new Client({ name: 'fake', version: '0.0.0' })
    await host.connect(clientT)
    await host.close()
    await new Promise(r => setTimeout(r, 20))

    expect(() => relayChannelWake(server, { content: 'x', meta: {} })).not.toThrow()
    await server.close()
  })
})

import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { createProxyServer } from '../src/proxy.js'

describe('channel proxy capability', () => {
  it('declares capabilities.experimental["claude/channel"] on initialize', async () => {
    const server = createProxyServer()
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await server.connect(serverTransport)
    const client = new Client({ name: 'fake-claude-code', version: '0.0.0' })
    await client.connect(clientTransport)
    const caps = client.getServerCapabilities() as {
      experimental?: Record<string, unknown>
    } | undefined
    expect(caps).toBeDefined()
    expect(caps?.experimental).toBeDefined()
    expect(caps?.experimental?.['claude/channel']).toEqual({})
    await client.close()
    await server.close()
  })
})

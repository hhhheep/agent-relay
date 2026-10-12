import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { createProxyServer } from '../src/proxy.js'

describe('channel proxy server identity', () => {
  it('serverInfo.name is cross-agent-teams-channel', async () => {
    const server = createProxyServer()
    const client = new Client({ name: 'fake-host', version: '0.0.0' })
    const [clientT, serverT] = InMemoryTransport.createLinkedPair()
    await server.connect(serverT)
    await client.connect(clientT)
    expect(client.getServerVersion()?.name).toBe('cross-agent-teams-channel')
    await client.close()
    await server.close()
  })
})

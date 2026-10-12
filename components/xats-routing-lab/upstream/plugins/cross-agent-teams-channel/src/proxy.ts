import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'

export function createProxyServer(): McpServer {
  return new McpServer(
    { name: 'cross-agent-teams-channel', version: '0.1.0' },
    { capabilities: { experimental: { 'claude/channel': {} } } }
  )
}

export interface ChannelWakeParams {
  content: string
  meta: Record<string, string>
}

export function relayChannelWake(server: McpServer, params: ChannelWakeParams): void {
  try {
    const notif = {
      method: 'notifications/claude/channel',
      params: params as unknown as Record<string, unknown>
    }
    const p = (server.server.notification as (n: typeof notif) => Promise<void>)(notif)
    if (p && typeof p.catch === 'function') {
      p.catch(() => { /* host closed — drop silently */ })
    }
  } catch {
    // host transport closed or not yet connected — drop silently
  }
}

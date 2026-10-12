import { describe, it, expect } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { createProxyServer, relayChannelWake } from '../src/proxy.js'
import { buildStartupHint } from '../src/cli.js'

describe('proxy startup channel notification', () => {
  it('emits a claude/channel notification containing csid and Claude self-registration guidance', async () => {
    const server = createProxyServer()
    const client = new Client({ name: 'fake-claude', version: '0.0.0' })
    const [clientT, serverT] = InMemoryTransport.createLinkedPair()

    const received: Array<{ method: string; params?: unknown }> = []
    client.fallbackNotificationHandler = async (n) => {
      received.push({ method: n.method, params: n.params })
    }

    await server.connect(serverT)
    await client.connect(clientT)

    const csid = 'csid-xyz-1234'
    // Exercise production: use the real hint builder from cli.ts
    const hint = buildStartupHint(csid)
    relayChannelWake(server, hint)

    await new Promise(r => setTimeout(r, 50))

    const hit = received.find(r => r.method === 'notifications/claude/channel')
    expect(hit, `got ${JSON.stringify(received)}`).toBeDefined()
    const params = hit!.params as { content: string; meta: Record<string, string> }
    expect(params.content).toContain(csid)
    expect(params.content).not.toContain('register_claude_self')
    expect(params.content).not.toContain('register_codex_self')
    expect(params.content).toContain('ui_pid')
    expect(params.content).toContain('bind_channel')
    // Resume / channel re-attach (fresh session, same $PPID) should route to reconnect,
    // not the bind_channel→register fallback that returns unknown_agent first.
    expect(params.content).toContain('reconnect({ui_pid: $PPID})')
    expect(params.content).toContain('register_agent')
    expect(params.content).toContain('agent_type: "claude-code"')
    expect(params.content).toContain('curl')
    // Must instruct Claude Code to ask the user (in English) before registering,
    // and use placeholder names that survive markdown rendering (no <angle-brackets>
    // in the user-facing wording — Claude Code's renderer strips them).
    expect(params.content).toMatch(/ask the user/i)
    expect(params.content).toContain('Register to xats')
    expect(params.content).toContain('your-agent-name')
    expect(params.content).toContain('your-team-name')
    expect(params.content).toMatch(/Do NOT register automatically/)
    // Lead-in must explain why (talk to other agents), not just bark "Register".
    expect(params.content).toMatch(/(message|talk to|communicate with) other agents/i)
    // No trailing '?' on the user-facing wording — it ends in a period.
    expect(params.content).not.toMatch(/basename\)\?/)
    expect(params.meta.kind).toBe('startup_bind_hint')
    // Brand-contract assertions
    expect(params.content).toContain('cross-agent-teams-mcp')
    expect(params.content).not.toContain('ts-agent-teams')
    expect(params.meta.source).toBe('cross_agent_teams_mcp')

    await client.close()
    await server.close()
  })

  it('includes the device value in both the user-facing ask and the register_agent call when the proxy runs with --device', async () => {
    const csid = 'csid-with-device'
    const hint = buildStartupHint(csid, 'mb-neo')
    expect(hint.content).toContain('device: "mb-neo"')
    // The user-facing wording must surface the device so the human reply
    // includes it verbatim — otherwise daemon returns device_required_from_remote.
    expect(hint.content).toContain('device: mb-neo')
    expect(hint.content).toContain('device_required_from_remote')
  })

  it('omits ALL device wording when the proxy was started without --device (pure-local zero-noise contract)', async () => {
    // csid intentionally free of the substring "device" so the regex below
    // only matches the hint's own wording, not the csid itself.
    const csid = '11111111-2222-3333-4444-555555555555'
    const hint = buildStartupHint(csid)
    // Local-loopback path must contain ZERO mention of "device" anywhere
    // (case-insensitive) so a single-host user never has to think about
    // device labels.  Cross-host wording is gated on --device being passed.
    expect(hint.content).not.toMatch(/device/i)
    expect(hint.content).not.toMatch(/device_required_from_remote/)
  })
})

import { describe, it, expect } from 'vitest'
import { isAgentLive, REACHABLE_MS, type AgentRow } from '../src/storage/agents-repo.js'

function agent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 'A',
    agent_type: null,
    agent_type_name: null,
    device: 'local',
    team: 'default',
    role: 'default',
    name: 'A',
    model: null,
    tmux_pane_id: null,
    runtime_ui_pid: null,
    delivery: { kind: 'none' },
    channel_session_id: null,
    identity_key: null,
    last_seen_at: new Date().toISOString(),
    ...overrides,
  }
}

describe('process-based agent liveness', () => {
  it('exports a day-level fallback window', () => {
    expect(REACHABLE_MS).toBe(4 * 24 * 60 * 60 * 1000)
  })

  it('marks a local agent with a live pid online despite long idleness', () => {
    const row = agent({
      runtime_ui_pid: process.pid,
      last_seen_at: new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString(),
    })

    expect(isAgentLive(row, { localDevice: 'local', livePanes: null })).toBe(true)
  })

  it('marks a local agent with a dead pid offline', () => {
    const row = agent({ runtime_ui_pid: 999_999_999 })

    expect(isAgentLive(row, { localDevice: 'local', livePanes: null })).toBe(false)
  })

  it('uses the batched pane set for local agents without a pid', () => {
    const present = agent({ agent_id: 'present', tmux_pane_id: '%1' })
    const absent = agent({ agent_id: 'absent', tmux_pane_id: '%2' })
    const livePanes = new Set(['%1'])

    expect(isAgentLive(present, { localDevice: 'local', livePanes })).toBe(true)
    expect(isAgentLive(absent, { localDevice: 'local', livePanes })).toBe(false)
  })

  it('falls back to REACHABLE_MS when the daemon cannot probe the agent', () => {
    const twoDaysAgo = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString()
    const fiveDaysAgo = new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString()

    expect(isAgentLive(
      agent({ device: 'remote', last_seen_at: twoDaysAgo }),
      { localDevice: 'local', livePanes: null }
    )).toBe(true)
    expect(isAgentLive(
      agent({ device: 'remote', last_seen_at: fiveDaysAgo }),
      { localDevice: 'local', livePanes: null }
    )).toBe(false)
  })
})

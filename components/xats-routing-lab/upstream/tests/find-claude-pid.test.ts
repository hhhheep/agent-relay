import { describe, expect, it } from 'vitest'
import {
  findClaudeUiPid,
  isClaudeCmd
} from '../plugins/cross-agent-teams-channel/src/find-claude-pid.js'

describe('isClaudeCmd', () => {
  it('matches plain claude bin path', () => {
    expect(isClaudeCmd('/usr/local/bin/claude --foo')).toBe(true)
    expect(isClaudeCmd('/Users/me/.nvm/.../bin/claude server:cross-agent-teams-channel')).toBe(true)
    expect(isClaudeCmd('claude')).toBe(true)
  })

  it('rejects npm/npx wrapper command lines', () => {
    expect(isClaudeCmd('npm exec cross-agent-teams-channel')).toBe(false)
    expect(isClaudeCmd('/usr/local/bin/npm exec --some-flag x')).toBe(false)
    expect(isClaudeCmd('/usr/local/bin/npx -y -p cross-agent-teams-mcp@latest cross-agent-teams-channel')).toBe(false)
  })

  it('rejects node-launched scripts', () => {
    expect(isClaudeCmd('/usr/local/bin/node /path/to/cli.js')).toBe(false)
    expect(isClaudeCmd('node /path/to/cli.js --daemon-url x')).toBe(false)
  })

  it('rejects shells and unrelated programs', () => {
    expect(isClaudeCmd('/bin/zsh -lc "exec claude"')).toBe(false)
    expect(isClaudeCmd('-bash')).toBe(false)
    expect(isClaudeCmd('')).toBe(false)
  })

  it('rejects basenames that contain but do not equal "claude"', () => {
    expect(isClaudeCmd('/usr/local/bin/claude-launcher')).toBe(false)
    expect(isClaudeCmd('/Applications/Claude.app/Contents/MacOS/Claude')).toBe(false) // capital C
  })
})

describe('findClaudeUiPid', () => {
  it('returns the first ancestor whose cmd is claude (npx wrapper case)', () => {
    // Process tree: claude(100) -> npm exec(200) -> node channel-cli(300)
    // proxy starts with process.ppid = 200 (the npm wrapper), should walk up to 100.
    const tree = new Map<number, { ppid: number; cmd: string }>([
      [200, { ppid: 100, cmd: 'npm exec cross-agent-teams-channel' }],
      [100, { ppid: 1, cmd: '/usr/local/bin/claude --dangerously-load-development-channels server:cross-agent-teams-channel' }]
    ])
    expect(findClaudeUiPid(200, (pid) => tree.get(pid) ?? null)).toBe(100)
  })

  it('returns ppid directly when ppid is already claude (dev path)', () => {
    const tree = new Map<number, { ppid: number; cmd: string }>([
      [100, { ppid: 1, cmd: '/usr/local/bin/claude --dangerously-load-development-channels server:cross-agent-teams-channel' }]
    ])
    expect(findClaudeUiPid(100, (pid) => tree.get(pid) ?? null)).toBe(100)
  })

  it('falls back to startPpid when no ancestor is claude', () => {
    const tree = new Map<number, { ppid: number; cmd: string }>([
      [200, { ppid: 100, cmd: 'npm exec foo' }],
      [100, { ppid: 1, cmd: '/bin/zsh -lc "exec foo"' }]
    ])
    expect(findClaudeUiPid(200, (pid) => tree.get(pid) ?? null)).toBe(200)
  })

  it('falls back to startPpid when ps reader returns null on first hop', () => {
    expect(findClaudeUiPid(999, () => null)).toBe(999)
  })

  it('stops walking at ppid 1 (init)', () => {
    let calls = 0
    const reader = (pid: number) => {
      calls += 1
      if (pid === 200) return { ppid: 1, cmd: 'npm exec foo' }
      return null
    }
    expect(findClaudeUiPid(200, reader)).toBe(200)
    expect(calls).toBe(1) // only the first hop, no walk past init
  })

  it('stops at MAX_HOPS to avoid infinite loops on cycles', () => {
    // Self-cycle: pid 200 reports its own ppid is 200 (pathological)
    let calls = 0
    const reader = (pid: number) => {
      calls += 1
      // Make the cycle break trigger before hop limit by reporting same pid as ppid
      if (pid === 200) return { ppid: 200, cmd: 'wrapper' }
      return null
    }
    expect(findClaudeUiPid(200, reader)).toBe(200)
    expect(calls).toBeLessThanOrEqual(2)
  })
})

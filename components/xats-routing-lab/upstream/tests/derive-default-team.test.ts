import { describe, expect, it } from 'vitest'
import { deriveDefaultTeam } from '../src/mcp/register-agent.js'

describe('deriveDefaultTeam', () => {
  it('uses explicit team before project_dir', () => {
    expect(deriveDefaultTeam({
      team: 'alpha',
      project_dir: '/x/y/cross-agent-teams-mcp',
    })).toBe('alpha')
  })

  it('derives team from project_dir basename', () => {
    expect(deriveDefaultTeam({
      project_dir: '/x/y/cross-agent-teams-mcp',
    })).toBe('cross-agent-teams-mcp')
  })

  it('strips trailing slash from project_dir', () => {
    expect(deriveDefaultTeam({
      project_dir: '/x/y/cross-agent-teams-mcp/',
    })).toBe('cross-agent-teams-mcp')
  })

  it('normalizes project_dir basename to lowercase', () => {
    expect(deriveDefaultTeam({
      project_dir: '/x/y/Cross-Agent-Teams-MCP',
    })).toBe('cross-agent-teams-mcp')
  })

  it('falls back to default for root project_dir', () => {
    expect(deriveDefaultTeam({ project_dir: '/' })).toBe('default')
  })

  it('falls back to default when team and project_dir are omitted', () => {
    expect(deriveDefaultTeam({})).toBe('default')
  })
})

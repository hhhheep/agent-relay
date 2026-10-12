import { describe, it, expect } from 'vitest'
import {
  parseCliArgs,
  CliArgError,
  isNonLoopbackDaemonUrl,
  deriveHostnameDeviceLabel,
} from '../src/cli.js'

describe('channel proxy parseCliArgs env var', () => {
  it('reads CROSS_AGENT_TEAMS_MCP_DAEMON_URL when flag is absent', () => {
    const parsed = parseCliArgs([], {
      CROSS_AGENT_TEAMS_MCP_DAEMON_URL: 'http://example:8787',
      TS_AGENT_TEAMS_DAEMON_URL: 'http://legacy:8787'
    } as NodeJS.ProcessEnv)
    expect(parsed.daemonUrl).toBe('http://example:8787')
  })

  it('ignores legacy TS_AGENT_TEAMS_DAEMON_URL when new var is missing', () => {
    expect(() => parseCliArgs([], {
      TS_AGENT_TEAMS_DAEMON_URL: 'http://legacy:8787'
    } as NodeJS.ProcessEnv)).toThrow(CliArgError)
  })

  it('diagnostic mentions CROSS_AGENT_TEAMS_MCP_DAEMON_URL', () => {
    try {
      parseCliArgs([], {} as NodeJS.ProcessEnv)
      expect.fail('expected throw')
    } catch (e) {
      expect((e as Error).message).toMatch(/CROSS_AGENT_TEAMS_MCP_DAEMON_URL/)
    }
  })

  it('parses --token and --device flags', () => {
    const parsed = parseCliArgs([
      '--daemon-url',
      'http://example:8787',
      '--token',
      'T',
      '--device',
      'GX@Desktop',
    ], {} as NodeJS.ProcessEnv)
    expect(parsed.token).toBe('T')
    expect(parsed.device).toBe('gx-desktop')
  })

  it('reads CROSS_AGENT_TEAMS_MCP_TOKEN when --token is absent', () => {
    const parsed = parseCliArgs(['--daemon-url', 'http://example:8787'], {
      CROSS_AGENT_TEAMS_MCP_TOKEN: 'ENV-T',
    } as NodeJS.ProcessEnv)
    expect(parsed.token).toBe('ENV-T')
  })

  it('rejects invalid explicit device labels', () => {
    expect(() => parseCliArgs([
      '--daemon-url',
      'http://example:8787',
      '--device',
      'has:colon',
    ], {} as NodeJS.ProcessEnv)).toThrow(CliArgError)
    expect(() => parseCliArgs([
      '--daemon-url',
      'http://example:8787',
      '--device',
      'a'.repeat(65),
    ], {} as NodeJS.ProcessEnv)).toThrow(CliArgError)
  })

  it('leaves device undefined when --device is not supplied on loopback (lets daemon auto-fill)', () => {
    const parsed = parseCliArgs([
      '--daemon-url',
      'http://127.0.0.1:8787',
    ], {} as NodeJS.ProcessEnv)
    expect(parsed.device).toBeUndefined()
    expect(parsed.deviceAutoDerivedNotice).toBeUndefined()
  })

  it('auto-derives device from hostname when --device is missing on non-loopback daemon', () => {
    const parsed = parseCliArgs(
      ['--daemon-url', 'http://192.168.1.10:8787'],
      {} as NodeJS.ProcessEnv,
      { hostname: () => 'My.Laptop' }
    )
    expect(parsed.device).toBe('my-laptop')
    expect(parsed.deviceAutoDerivedNotice).toMatch(/auto-derived "my-laptop"/)
    expect(parsed.deviceAutoDerivedNotice).toMatch(/192\.168\.1\.10/)
  })

  it('fail-fasts when --device is missing on non-loopback and hostname yields nothing usable', () => {
    expect(() =>
      parseCliArgs(
        ['--daemon-url', 'http://192.168.1.10:8787'],
        {} as NodeJS.ProcessEnv,
        { hostname: () => '' }
      )
    ).toThrow(CliArgError)
    expect(() =>
      parseCliArgs(
        ['--daemon-url', 'http://192.168.1.10:8787'],
        {} as NodeJS.ProcessEnv,
        { hostname: () => '---' }
      )
    ).toThrow(CliArgError)
  })
})

describe('isNonLoopbackDaemonUrl', () => {
  it.each([
    ['http://127.0.0.1:9100/mcp', false],
    ['http://127.5.5.5:9100/mcp', false],
    ['http://localhost:9100/mcp', false],
    ['http://[::1]:9100/mcp', false],
    ['http://0.0.0.0:9100/mcp', false],
    ['http://192.168.1.10:9100/mcp', true],
    ['http://example.com:9100/mcp', true],
    ['not a url', true],
  ])('classifies %s -> non-loopback=%s', (url, expected) => {
    expect(isNonLoopbackDaemonUrl(url)).toBe(expected)
  })
})

describe('deriveHostnameDeviceLabel', () => {
  it('lowercases and normalizes non-allowed chars', () => {
    expect(deriveHostnameDeviceLabel('My.Laptop')).toBe('my-laptop')
    expect(deriveHostnameDeviceLabel('  GX-PC  ')).toBe('gx-pc')
  })
  it('trims leading/trailing dashes left over from normalization', () => {
    expect(deriveHostnameDeviceLabel('.foo.')).toBe('foo')
  })
  it('returns null for empty or all-dash hostnames', () => {
    expect(deriveHostnameDeviceLabel('')).toBeNull()
    expect(deriveHostnameDeviceLabel('...')).toBeNull()
  })
  it('returns null when label exceeds 64 chars', () => {
    expect(deriveHostnameDeviceLabel('a'.repeat(65))).toBeNull()
  })
})

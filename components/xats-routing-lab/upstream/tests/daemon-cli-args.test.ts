import { describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { parseDaemonCliArgs } from '../src/cli.js'

describe('daemon CLI args', () => {
  it('parses host, device, token, and port', () => {
    const parsed = parseDaemonCliArgs([
      'node',
      'cli.js',
      'daemon',
      '--host',
      '0.0.0.0',
      '--device',
      'JT@Laptop',
      '--token',
      'T',
      '--port',
      '9200',
    ])
    expect(parsed.host).toBe('0.0.0.0')
    expect(parsed.localDevice).toBe('jt-laptop')
    expect(parsed.token).toBe('T')
    expect(parsed.requestedPort).toBe(9200)
  })

  it('uses CROSS_AGENT_TEAMS_MCP_TOKEN as token fallback', () => {
    const parsed = parseDaemonCliArgs([
      'node',
      'cli.js',
      'daemon',
      '--device',
      'jt',
    ], {
      CROSS_AGENT_TEAMS_MCP_TOKEN: 'ENV-T',
    } as NodeJS.ProcessEnv)
    expect(parsed.token).toBe('ENV-T')
  })

  it('defaults loopbackCompanion to true and honors --no-loopback-companion', () => {
    const on = parseDaemonCliArgs(['node', 'cli.js', 'daemon', '--device', 'jt'])
    expect(on.loopbackCompanion).toBe(true)

    const off = parseDaemonCliArgs([
      'node', 'cli.js', 'daemon', '--device', 'jt', '--no-loopback-companion',
    ])
    expect(off.loopbackCompanion).toBe(false)
  })

  it('rejects invalid device labels', () => {
    expect(() => parseDaemonCliArgs([
      'node',
      'cli.js',
      'daemon',
      '--device',
      'has:colon',
    ])).toThrow('invalid_device_label')
  })

  it('exits before binding a non-loopback host without a token', () => {
    const dir = mkdtempSync(join(tmpdir(), 'atm-daemon-cli-'))
    try {
      const result = spawnSync(process.execPath, [
        '--import',
        'tsx',
        'src/cli.ts',
        'daemon',
        '--host',
        '0.0.0.0',
        '--db',
        join(dir, 'data.db'),
        '--pid-file',
        join(dir, 'daemon.pid'),
        '--port',
        '0',
      ], {
        cwd: process.cwd(),
        encoding: 'utf8',
        env: {
          ...process.env,
          CROSS_AGENT_TEAMS_MCP_TOKEN: '',
        },
      })
      expect(result.status).toBe(1)
      expect(result.stderr).toContain('token_required_for_non_loopback_bind')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

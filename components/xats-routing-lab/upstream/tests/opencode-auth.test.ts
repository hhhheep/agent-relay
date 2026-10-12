import { describe, it, expect } from 'vitest'
import { opencodeAuthHeaders } from '../src/mcp/opencode-auth.js'

function basic(username: string, password: string): string {
  return `Basic ${Buffer.from(`${username}:${password}`).toString('base64')}`
}

describe('opencodeAuthHeaders', () => {
  it('returns no header when auth_token_ref is absent/blank', () => {
    expect(opencodeAuthHeaders(undefined, {})).toEqual({ headers: {} })
    expect(opencodeAuthHeaders('  ', {})).toEqual({ headers: {} })
  })

  it('builds Basic header with default username "opencode"', () => {
    expect(opencodeAuthHeaders('OPENCODE_SERVER_PASSWORD', {
      OPENCODE_SERVER_PASSWORD: 'pw',
    })).toEqual({ headers: { Authorization: basic('opencode', 'pw') } })
  })

  it('honors OPENCODE_SERVER_USERNAME verbatim (no trim)', () => {
    expect(opencodeAuthHeaders('OPENCODE_SERVER_PASSWORD', {
      OPENCODE_SERVER_PASSWORD: 'pw',
      OPENCODE_SERVER_USERNAME: '  alice  ',
    })).toEqual({ headers: { Authorization: basic('  alice  ', 'pw') } })
  })

  it('encodes the password verbatim, preserving surrounding spaces', () => {
    expect(opencodeAuthHeaders('OPENCODE_SERVER_PASSWORD', {
      OPENCODE_SERVER_PASSWORD: ' secret ',
    })).toEqual({ headers: { Authorization: basic('opencode', ' secret ') } })
  })

  it('returns missing_auth_token when the env var is unset', () => {
    expect(opencodeAuthHeaders('OPENCODE_SERVER_PASSWORD', {})).toEqual({
      error: 'missing_auth_token',
      detail: { ref: 'OPENCODE_SERVER_PASSWORD' },
    })
  })

  it('treats an empty-string password as missing', () => {
    expect(opencodeAuthHeaders('OPENCODE_SERVER_PASSWORD', {
      OPENCODE_SERVER_PASSWORD: '',
    })).toEqual({
      error: 'missing_auth_token',
      detail: { ref: 'OPENCODE_SERVER_PASSWORD' },
    })
  })

  it('does not throw on inherited keys like toString/constructor (returns missing_auth_token)', () => {
    for (const ref of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(opencodeAuthHeaders(ref, {})).toEqual({
        error: 'missing_auth_token',
        detail: { ref },
      })
    }
  })

  it('reads an own property that shadows an inherited name', () => {
    const env: NodeJS.ProcessEnv = { toString: 'literal-pw' }
    expect(opencodeAuthHeaders('toString', env)).toEqual({
      headers: { Authorization: basic('opencode', 'literal-pw') },
    })
  })
})

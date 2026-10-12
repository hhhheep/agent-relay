import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Build brand token at runtime so this file itself does not match any sweep.
const LEGACY_BRAND = ['ts', 'agent', 'teams'].join('-')
const SPEC_PATH = resolve(
  process.cwd(),
  'openspec/specs/daemon-core/spec.md'
)

const SWEEP_TEST_PATH = resolve(
  process.cwd(),
  'tests/brand-sweep.test.ts'
)

describe('brand-sweep spec self-check', () => {
  it('daemon-core main spec file is literal-free', () => {
    const content = readFileSync(SPEC_PATH, 'utf8')
    expect(
      content.includes(LEGACY_BRAND),
      `daemon-core spec must not embed the legacy brand literal as a contiguous substring`
    ).toBe(false)
  })

  it('sweep allowlist covers daemon-core spec dir as defense in depth', () => {
    const sweep = readFileSync(SWEEP_TEST_PATH, 'utf8')
    const hasDirExclude = sweep.includes('--exclude-dir=daemon-core')
    const hasPathAllowlist =
      sweep.includes('daemon-core') && /exclude/i.test(sweep)
    expect(
      hasDirExclude || hasPathAllowlist,
      `brand-sweep.test.ts must categorically exempt the daemon-core spec directory via --exclude-dir=daemon-core or an equivalent path-aware allowlist entry`
    ).toBe(true)
  })
})

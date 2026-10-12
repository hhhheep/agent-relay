import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'

const ACTIVE_PATHS = [
  'src',
  'plugins/cross-agent-teams-channel/src',
  'plugins/cross-agent-teams-channel/tests',
  'plugins/cross-agent-teams-channel/package.json',
  'plugins/cross-agent-teams-channel/plugin.json',
  'plugins/cross-agent-teams-channel/README.md',
  'tests',
  'docs/configs',
  'openspec/specs',
  'package.json',
  'tsconfig.json',
  'opencode.json',
  '.gitignore'
]

// Build brand token at runtime so this file itself does not match the sweep.
const LEGACY_BRAND = ['ts', 'agent', 'teams'].join('-')

// Files that legitimately reference the legacy brand as negative-assertion
// test data (they exist to prove the brand is absent elsewhere).
const ANTI_BRAND_ASSERTION_EXCLUDES = [
  'brand-sweep.test.ts',
  'daemon-brand-in-tool-text.test.ts',
  'proxy-cli.test.ts',
  'proxy-startup-notification.test.ts'
]

describe('brand sweep', () => {
  it('no active source file contains the legacy ts-agent-teams brand', () => {
    const excludeArgs = [
      ...ANTI_BRAND_ASSERTION_EXCLUDES.flatMap((name) => [
        `--exclude=${name}`
      ]),
      // daemon-core dir holds the spec that describes the invariant by
      // negative assertion; see change fix-brand-sweep-self-match.
      '--exclude-dir=daemon-core'
    ]
    let hits = ''
    try {
      hits = execFileSync(
        'grep',
        [
          '-rHn',
          '--binary-files=without-match',
          ...excludeArgs,
          LEGACY_BRAND,
          ...ACTIVE_PATHS
        ],
        { encoding: 'utf8' }
      )
    } catch (e: unknown) {
      const err = e as { status?: number; stdout?: string }
      if (err.status === 1) { hits = '' } else { throw e }
    }
    expect(hits, `unexpected legacy brand hits:\n${hits}`).toBe('')
  })
})

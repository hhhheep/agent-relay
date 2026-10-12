import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const PROXY_SRC_DIR = resolve(__dirname, '..', 'plugins', 'cross-agent-teams-channel', 'src')

// The point of this guard is to catch a re-introduction of the 0.2.x
// auto-bootstrap path (the channel proxy spawning a daemon child).  The 0.3.x
// proxy is allowed to make synchronous, short-lived system queries (e.g.
// `execFileSync('ps', ...)` to walk ancestor pids when figuring out which
// claude CLI launched it through an npx wrapper).  We therefore ban the
// long-running primitives `spawn` / `fork` (and their *Sync siblings) but
// permit `execFile` / `exec` style synchronous invocations.
const FORBIDDEN_PATTERN = /\b(spawn|fork)(Sync)?\s*\(/

function listSourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    const st = statSync(p)
    if (st.isDirectory()) {
      out.push(...listSourceFiles(p))
    } else if (entry.endsWith('.ts') || entry.endsWith('.js')) {
      out.push(p)
    }
  }
  return out
}

describe('channel proxy source has no daemon-spawning primitives', () => {
  it('no file under plugins/cross-agent-teams-channel/src references child_process / spawn / fork / execFile', () => {
    const files = listSourceFiles(PROXY_SRC_DIR)
    expect(files.length).toBeGreaterThan(0)
    const offenders: string[] = []
    for (const f of files) {
      const contents = readFileSync(f, 'utf8')
      if (FORBIDDEN_PATTERN.test(contents)) offenders.push(f)
    }
    expect(offenders, `Forbidden spawn primitives found in: ${offenders.join(', ')}`).toEqual([])
  })
})

import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

function listTsFiles(dir: string): string[] {
  const entries = readdirSync(dir, { withFileTypes: true })
  return entries.flatMap((entry) => {
    const fullPath = join(dir, entry.name)
    if (entry.isDirectory()) return listTsFiles(fullPath)
    return entry.name.endsWith('.ts') ? [fullPath] : []
  })
}

describe('daemon source has no direct channel_session_id writes', () => {
  it('does not UPDATE or INSERT agents.channel_session_id in src/', () => {
    const srcDir = join(process.cwd(), 'src')
    const files = listTsFiles(srcDir)
    const updatePattern = /UPDATE\s+agents\s+SET\s+channel_session_id\b/is
    const insertPattern = /INSERT\s+INTO\s+agents\s*\([^)]*\bchannel_session_id\b/is

    const offenders = files.filter((file) => {
      const content = readFileSync(file, 'utf8')
      return updatePattern.test(content) || insertPattern.test(content)
    })

    expect(offenders).toEqual([])
  })
})

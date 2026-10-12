import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('openDb', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('applies WAL, busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    expect(String(db.pragma('journal_mode', { simple: true })).toLowerCase()).toBe('wal')
    expect(Number(db.pragma('busy_timeout', { simple: true }))).toBe(5000)
    expect(Number(db.pragma('synchronous', { simple: true }))).toBe(1)
    expect(Number(db.pragma('foreign_keys', { simple: true }))).toBe(1)
    db.close()
  })
})

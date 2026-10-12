import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import Database from 'better-sqlite3'
import { startServer } from '../src/daemon/server.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

function seedAgedEvents(dbPath: string, count: number, daysOld: number): void {
  const db = new Database(dbPath)
  const ts = new Date(Date.now() - daysOld * 86400 * 1000).toISOString()
  const stmt = db.prepare(
    'INSERT INTO events (from_team, to_team, event_type, actor_agent_id, payload, created_at) VALUES (?,?,?,?,?,?)'
  )
  const tx = db.transaction(() => {
    for (let i = 0; i < count; i++) stmt.run('default', 'default', 'message_sent', null, '{}', ts)
  })
  tx()
  db.close()
}

function countEvents(dbPath: string): number {
  const db = new Database(dbPath, { readonly: true })
  const row = db.prepare('SELECT COUNT(*) AS c FROM events').get() as { c: number }
  db.close()
  return row.c
}

describe('cleanup interval', () => {
  const cleanups: string[] = []
  afterEach(() => { cleanups.forEach(d => rmSync(d, { recursive: true, force: true })); cleanups.length = 0 })

  it('runs runCleanup on the provided cadence and stops on close', async () => {
    const dir = tmp(); cleanups.push(dir)
    const dbPath = join(dir, 'data.db')
    const { app } = await startServer({ dbPath, port: 0, cleanupIntervalMs: 300 })

    // Seed 10 events dated 31 days old — beyond the 30-day uniform TTL
    seedAgedEvents(dbPath, 10, 31)
    expect(countEvents(dbPath)).toBe(10)

    // Wait past one cleanup tick
    await new Promise(r => setTimeout(r, 500))

    // Aged events should be deleted by the 30-day hard TTL
    expect(countEvents(dbPath)).toBe(0)

    // Close — onClose hook must clearInterval so vitest doesn't report a leaked handle
    await app.close()

    // After close, even if we re-insert aged events, they are NOT cleaned up (proof the interval stopped)
    seedAgedEvents(dbPath, 5, 31)
    await new Promise(r => setTimeout(r, 500))
    expect(countEvents(dbPath)).toBe(5)
  }, 10000)
})

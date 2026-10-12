import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = (): string => mkdtempSync(join(tmpdir(), 'atm-identity-idx-'))

interface IndexListRow { name: string; unique: number }
interface IndexInfoRow { seqno: number; cid: number; name: string }

describe('agents_identity_idx UNIQUE on (device, team, name)', () => {
  const dirs: string[] = []
  afterEach(() => { dirs.forEach(d => rmSync(d, { recursive: true, force: true })); dirs.length = 0 })

  function freshDb(): ReturnType<typeof openDb> {
    const dir = tmp(); dirs.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    return db
  }

  it('index is marked UNIQUE', () => {
    const db = freshDb()
    const idx = db.prepare(`PRAGMA index_list('agents')`).all() as IndexListRow[]
    const row = idx.find(r => r.name === 'agents_identity_idx')
    expect(row).toBeDefined()
    expect(row!.unique).toBe(1)
  })

  it('index covers exactly device, team, and name in order', () => {
    const db = freshDb()
    const info = db.prepare(`PRAGMA index_info('agents_identity_idx')`).all() as IndexInfoRow[]
    const names = info.sort((a, b) => a.seqno - b.seqno).map(r => r.name)
    expect(names).toEqual(['device', 'team', 'name'])
  })

  it('inserting two rows with same (device, team, name) raises UNIQUE constraint failed', () => {
    const db = freshDb()
    const now = new Date().toISOString()
    const insert = db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    insert.run('X', 'local', 'default', 'backend', 'alice', null, now, now, null)
    expect(() => {
      insert.run('Y', 'local', 'default', 'frontend', 'alice', null, now, now, null)
    }).toThrow(/UNIQUE constraint failed/)
  })
})

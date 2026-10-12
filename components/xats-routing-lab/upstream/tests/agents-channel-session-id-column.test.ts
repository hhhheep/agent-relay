import { describe, it, expect, afterEach } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { openDb } from '../src/storage/db.js'
import { applySchema } from '../src/storage/schema.js'

const tmp = () => mkdtempSync(join(tmpdir(), 'atm-'))

describe('agents.channel_session_id column', () => {
  const cleanups: string[] = []
  afterEach(() => {
    cleanups.forEach(d => rmSync(d, { recursive: true, force: true }))
    cleanups.length = 0
  })

  it('fresh bootstrap creates nullable TEXT channel_session_id column', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const cols = db.pragma('table_info(agents)') as Array<{
      name: string; type: string; notnull: number; pk: number
    }>
    const csid = cols.find(c => c.name === 'channel_session_id')
    expect(csid).toBeDefined()
    expect(csid?.type).toBe('TEXT')
    expect(csid?.notnull).toBe(0)
    db.close()
  })

  it('rows inserted without channel_session_id default to NULL', () => {
    const dir = tmp(); cleanups.push(dir)
    const db = openDb(join(dir, 'data.db'))
    applySchema(db)
    const now = new Date().toISOString()
    db.prepare(
      `INSERT INTO agents (agent_id, device, team, role, name, model, registered_at, last_seen_at, tmux_pane_id)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run('a1', 'local', 'default', 'r', 'n1', 'm', now, now, null)
    const row = db.prepare(`SELECT channel_session_id FROM agents WHERE agent_id='a1'`).get() as
      { channel_session_id: string | null }
    expect(row.channel_session_id).toBeNull()
    db.close()
  })
})

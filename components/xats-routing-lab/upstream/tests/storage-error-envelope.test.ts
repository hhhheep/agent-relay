import { describe, it, expect } from 'vitest'
import { wrapStorage, isStorageError } from '../src/daemon/errors.js'

class FakeSqliteError extends Error { constructor(public code: string, msg: string) { super(msg); this.name = 'SqliteError' } }

describe('storage error envelope', () => {
  it('maps SqliteError SQLITE_FULL to storage_unavailable', async () => {
    const res = await wrapStorage(async () => { throw new FakeSqliteError('SQLITE_FULL', 'disk full') })
    expect(res).toEqual({ error: 'storage_unavailable' })
  })

  it('maps SqliteError SQLITE_BUSY to storage_unavailable', async () => {
    const res = await wrapStorage(async () => { throw new FakeSqliteError('SQLITE_BUSY', 'busy') })
    expect(res).toEqual({ error: 'storage_unavailable' })
  })

  it('re-throws non-storage errors', async () => {
    await expect(wrapStorage(async () => { throw new Error('other') })).rejects.toThrow('other')
  })

  it('returns the handler result on success', async () => {
    const res = await wrapStorage(async () => ({ ok: true }))
    expect(res).toEqual({ ok: true })
  })

  it('isStorageError detects by name or code', () => {
    expect(isStorageError(new FakeSqliteError('SQLITE_FULL', 'x'))).toBe(true)
    expect(isStorageError(new Error('other'))).toBe(false)
  })
})

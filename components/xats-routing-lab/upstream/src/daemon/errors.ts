const STORAGE_CODES = new Set(['SQLITE_FULL','SQLITE_BUSY','SQLITE_IOERR','SQLITE_LOCKED','SQLITE_READONLY'])

export function isStorageError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const anyErr = err as { name?: string; code?: string }
  if (anyErr.name === 'SqliteError') return true
  if (anyErr.code && STORAGE_CODES.has(anyErr.code)) return true
  return false
}

export async function wrapStorage<T>(fn: () => Promise<T> | T): Promise<T | { error: 'storage_unavailable' }> {
  try {
    return await fn()
  } catch (err) {
    if (isStorageError(err)) return { error: 'storage_unavailable' }
    throw err
  }
}

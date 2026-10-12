import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export type KimiAuthResult =
  | { headers: Record<string, string> }
  | { error: 'missing_auth_token'; detail: { ref: string } | { token_file: string } }

export const DEFAULT_KIMI_TOKEN_FILE = join(homedir(), '.kimi-code', 'server.token')

function readEnvString(env: NodeJS.ProcessEnv, key: string): string | undefined {
  if (Object.prototype.hasOwnProperty.call(env, key)) {
    const v = env[key]
    return typeof v === 'string' ? v : undefined
  }
  return undefined
}

/**
 * Kimi Code server bearer auth header. Resolution order:
 * 1. auth_token_ref present → env var name lookup (missing/empty →
 *    missing_auth_token before any I/O). Never treated as an inline secret.
 * 2. auth_token_ref absent → read the token file `kimi server` persists at
 *    ~/.kimi-code/server.token (missing/unreadable/empty → missing_auth_token).
 * hasOwnProperty avoids inherited keys like `toString`.
 */
export function kimiAuthHeaders(
  auth_token_ref: string | undefined,
  env: NodeJS.ProcessEnv,
  tokenFilePath: string = DEFAULT_KIMI_TOKEN_FILE
): KimiAuthResult {
  const ref = auth_token_ref?.trim()
  if (ref) {
    const token = readEnvString(env, ref)
    if (token === undefined || token.trim() === '') {
      return { error: 'missing_auth_token', detail: { ref } }
    }
    return { headers: { Authorization: `Bearer ${token}` } }
  }
  let token: string
  try {
    token = readFileSync(tokenFilePath, 'utf8').trim()
  } catch {
    return { error: 'missing_auth_token', detail: { token_file: tokenFilePath } }
  }
  if (token === '') {
    return { error: 'missing_auth_token', detail: { token_file: tokenFilePath } }
  }
  return { headers: { Authorization: `Bearer ${token}` } }
}

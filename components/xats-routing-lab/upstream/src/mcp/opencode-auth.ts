export type OpencodeAuthResult =
  | { headers: Record<string, string> }
  | { error: 'missing_auth_token'; detail: { ref: string } }

function readEnvString(env: NodeJS.ProcessEnv, key: string): string | undefined {
  if (Object.prototype.hasOwnProperty.call(env, key)) {
    const v = env[key]
    return typeof v === 'string' ? v : undefined
  }
  return undefined
}

/**
 * OpenCode server HTTP Basic auth header. Password is read verbatim from the
 * env var named by auth_token_ref (the server compares verbatim, no trim);
 * hasOwnProperty avoids inherited keys like `toString`.
 */
export function opencodeAuthHeaders(
  auth_token_ref: string | undefined,
  env: NodeJS.ProcessEnv
): OpencodeAuthResult {
  const ref = auth_token_ref?.trim()
  if (!ref) return { headers: {} }
  const password = readEnvString(env, ref)
  if (password === undefined || password === '') {
    return { error: 'missing_auth_token', detail: { ref } }
  }
  const username = readEnvString(env, 'OPENCODE_SERVER_USERNAME') ?? 'opencode'
  const credentials = Buffer.from(`${username}:${password}`).toString('base64')
  return { headers: { Authorization: `Basic ${credentials}` } }
}

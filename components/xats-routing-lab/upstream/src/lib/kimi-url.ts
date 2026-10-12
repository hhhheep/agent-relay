/**
 * Single source of truth for kimi server base URL handling. Kimi endpoint
 * URLs are built by appending `/api/v1/...` to base_url, so a query,
 * fragment, or userinfo component corrupts every request the daemon would
 * ever make. Every intake point (register schema, reconnect schema, delivery
 * write validation) rejects via `kimiBaseUrlIssue`; the share key, the
 * persisted delivery, and the reconnect lookup all compare via
 * `canonicalKimiBaseUrl`.
 */

export type KimiBaseUrlIssue =
  | 'unparseable'
  | 'not_http'
  | 'query_or_fragment'
  | 'userinfo'

/**
 * The raw string is checked for the `?` / `#` separators because WHATWG URL
 * reports a bare trailing `?` or `#` as an EMPTY search/hash while keeping
 * the separator in `href` — checking only `.search`/`.hash` lets
 * `http://host/?` through and the appended API path then lands in the query.
 */
export function kimiBaseUrlIssue(base_url: string): KimiBaseUrlIssue | undefined {
  let parsed: URL
  try {
    parsed = new URL(base_url)
  } catch {
    return 'unparseable'
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return 'not_http'
  }
  if (base_url.includes('?') || base_url.includes('#')) {
    return 'query_or_fragment'
  }
  if (parsed.username !== '' || parsed.password !== '') {
    return 'userinfo'
  }
  return undefined
}

/**
 * Canonical form: scheme/host lowercased and default port dropped (both via
 * the URL parser), hash and any dangling `?` stripped, trailing slashes
 * removed. Query-carrying URLs are rejected at every intake point; if one
 * still reaches this function (service-direct callers), it is kept verbatim
 * past the hash strip — trimming slashes inside a query would corrupt it.
 * Unparseable input falls back to bare trailing-slash trimming.
 */
export function canonicalKimiBaseUrl(base_url: string): string {
  try {
    const parsed = new URL(base_url)
    parsed.hash = ''
    if (parsed.search !== '') return parsed.href
    // Re-assigning an empty search drops a dangling '?' from the
    // serialization ('http://h/?' -> 'http://h/').
    parsed.search = ''
    return parsed.href.replace(/\/+$/, '')
  } catch {
    return base_url.replace(/\/+$/, '')
  }
}

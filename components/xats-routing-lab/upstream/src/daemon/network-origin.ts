export type SessionOrigin = 'local' | 'remote'

export interface SessionOriginInfo {
  origin: SessionOrigin
  remote_addr: string | null
}

export function isLoopbackAddress(address: string | null | undefined): boolean {
  if (!address) return true
  return (
    address === '::1' ||
    address.startsWith('127.') ||
    address.startsWith('::ffff:127.')
  )
}

export function classifyPeerAddress(address: string | null | undefined): SessionOriginInfo {
  if (isLoopbackAddress(address)) {
    return { origin: 'local', remote_addr: null }
  }
  return { origin: 'remote', remote_addr: address ?? null }
}

export function isLoopbackHost(host: string): boolean {
  return host === 'localhost' || isLoopbackAddress(host)
}

// Bind-host predicate: does listening on `host` already accept connections
// arriving on 127.0.0.1? Used to decide whether the loopback companion
// listener should be skipped (it would collide on the same port if so).
//
// Specifically: 127.0.0.1, localhost (resolves to 127.0.0.1), and 0.0.0.0
// (catch-all IPv4) all cover loopback connections. 127.0.0.2 and similar
// alternate loopback addresses do NOT — they bind a different socket and
// 127.0.0.1 traffic still needs a separate listener.
export function bindHostCoversIpv4Loopback(host: string): boolean {
  return host === '127.0.0.1' || host === 'localhost' || host === '0.0.0.0'
}

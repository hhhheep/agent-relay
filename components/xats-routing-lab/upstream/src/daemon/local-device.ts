import { hostname } from 'node:os'

export function resolveLocalDeviceLabel(explicit?: string): string {
  const raw = explicit ?? hostname()
  if (raw.includes(':')) {
    throw new Error('invalid_device_label')
  }

  const normalized = raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, '-')

  if (normalized.length === 0) {
    // Empty result only auto-falls back when the user did not supply --device.
    // Explicit empty/whitespace/all-replaced labels are a misconfiguration —
    // surface it instead of silently aliasing distinct hosts to "local".
    if (explicit !== undefined) {
      throw new Error('invalid_device_label')
    }
    return 'local'
  }
  if (normalized.length > 64) {
    throw new Error('invalid_device_label')
  }
  return normalized
}

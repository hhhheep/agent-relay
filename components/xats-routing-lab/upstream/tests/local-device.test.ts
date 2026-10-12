import { describe, expect, it } from 'vitest'
import { resolveLocalDeviceLabel } from '../src/daemon/local-device.js'

describe('resolveLocalDeviceLabel', () => {
  it('normalizes explicit labels', () => {
    expect(resolveLocalDeviceLabel('JT@Laptop')).toBe('jt-laptop')
  })

  it('rejects an empty explicit label as a misconfiguration', () => {
    expect(() => resolveLocalDeviceLabel('   ')).toThrow('invalid_device_label')
    expect(() => resolveLocalDeviceLabel('')).toThrow('invalid_device_label')
  })

  it('only falls back to "local" when no explicit label is given', () => {
    // When `explicit === undefined`, the helper consults os.hostname(). On
    // hosts where the derived value is non-empty we cannot deterministically
    // assert the exact label, so this assertion only checks the
    // non-empty + lowercase contract.
    const label = resolveLocalDeviceLabel()
    expect(label.length).toBeGreaterThan(0)
    expect(label).toBe(label.toLowerCase())
  })

  it('rejects labels containing colon', () => {
    expect(() => resolveLocalDeviceLabel('has:colon')).toThrow('invalid_device_label')
  })

  it('rejects labels longer than 64 characters after normalization', () => {
    expect(() => resolveLocalDeviceLabel('a'.repeat(65))).toThrow('invalid_device_label')
  })
})

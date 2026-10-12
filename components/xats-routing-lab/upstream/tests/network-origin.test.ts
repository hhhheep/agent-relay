import { describe, expect, it } from 'vitest'
import {
  classifyPeerAddress,
  isLoopbackHost,
} from '../src/daemon/network-origin.js'

describe('network origin classification', () => {
  it('tags IPv4 loopback as local', () => {
    expect(classifyPeerAddress('127.0.0.1')).toEqual({
      origin: 'local',
      remote_addr: null,
    })
  })

  it('tags IPv6 loopback as local', () => {
    expect(classifyPeerAddress('::1')).toEqual({
      origin: 'local',
      remote_addr: null,
    })
  })

  it('tags IPv4-mapped IPv6 loopback as local', () => {
    expect(classifyPeerAddress('::ffff:127.0.0.1')).toEqual({
      origin: 'local',
      remote_addr: null,
    })
  })

  it('tags LAN peers as remote and preserves the peer address', () => {
    expect(classifyPeerAddress('192.168.1.42')).toEqual({
      origin: 'remote',
      remote_addr: '192.168.1.42',
    })
  })

  it('classifies bind hosts for the daemon startup guard', () => {
    expect(isLoopbackHost('127.0.0.1')).toBe(true)
    expect(isLoopbackHost('localhost')).toBe(true)
    expect(isLoopbackHost('0.0.0.0')).toBe(false)
  })
})

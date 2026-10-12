import { describe, it, expect, afterEach } from 'vitest'
import { createServer, type Server } from 'node:net'
import { selectPort } from '../src/daemon/port.js'

function hold(port: number): Promise<Server> {
  return new Promise((resolve, reject) => {
    const s = createServer()
    s.once('error', reject)
    s.listen(port, '127.0.0.1', () => resolve(s))
  })
}

describe('selectPort', () => {
  const held: Server[] = []
  afterEach(async () => { for (const s of held) await new Promise(r => s.close(() => r(null))); held.length = 0 })

  it('returns the first candidate when free', async () => {
    const port = await selectPort([19099, 19100, 19101], '127.0.0.1')
    expect(port).toBe(19099)
  })

  it('falls back when first two are busy', async () => {
    held.push(await hold(19200))
    held.push(await hold(19201))
    const port = await selectPort([19200, 19201, 19202], '127.0.0.1')
    expect(port).toBe(19202)
  })

  it('throws when all three are busy', async () => {
    held.push(await hold(19300))
    held.push(await hold(19301))
    held.push(await hold(19302))
    await expect(selectPort([19300, 19301, 19302], '127.0.0.1')).rejects.toThrow(/unavailable/i)
  })
})

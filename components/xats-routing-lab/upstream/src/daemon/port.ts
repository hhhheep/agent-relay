import { createServer } from 'node:net'

function tryBind(port: number, host: string): Promise<boolean> {
  return new Promise(resolve => {
    const s = createServer()
    s.once('error', () => resolve(false))
    s.listen(port, host, () => s.close(() => resolve(true)))
  })
}

export async function selectPort(candidates: number[], host = '127.0.0.1'): Promise<number> {
  for (const p of candidates) {
    if (await tryBind(p, host)) return p
  }
  throw new Error(`ports ${candidates[0]}-${candidates[candidates.length - 1]} unavailable`)
}

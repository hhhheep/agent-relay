import type { FastifyRequest, FastifyReply } from 'fastify'
import { sendControlPlaneReject } from '../mcp/control-plane-reject.js'

export function extractToken(req: FastifyRequest): string | undefined {
  const h = req.headers['authorization']
  if (typeof h === 'string' && h.startsWith('Bearer ')) return h.slice(7)
  const q = (req.query as Record<string, unknown> | undefined)?.token
  return typeof q === 'string' ? q : undefined
}

export function makeAuthHook(expected: string | undefined) {
  return async (req: FastifyRequest, reply: FastifyReply) => {
    if (req.url.startsWith('/health')) return
    if (!expected) return
    const got = extractToken(req)
    if (got !== expected) return sendControlPlaneReject(reply, 401)
  }
}

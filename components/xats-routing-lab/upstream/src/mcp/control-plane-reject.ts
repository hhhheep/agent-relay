import type { FastifyReply } from 'fastify'

/**
 * Emit a transport-level control-plane rejection with an EMPTY body.
 *
 * Strict MCP clients (e.g. codex's `rmcp`) deserialize ANY `/mcp` response body
 * as an untagged JSON-RPC message. A bare `{ "error": <string> }` object matches
 * no JSON-RPC 2.0 variant and poisons the client transport worker. An empty body
 * is the guaranteed-safe form: there is nothing to mis-parse. The HTTP status
 * (404/401/409/403) is the sole source of truth for the failure kind.
 */
export function sendControlPlaneReject(reply: FastifyReply, status: number): FastifyReply {
  return reply.code(status).send()
}

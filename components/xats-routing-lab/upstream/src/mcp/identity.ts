export class IdentityMismatchError extends Error {
  readonly code = 'identity_mismatch'
  constructor() { super('identity_mismatch') }
}

export function ensureCallerMatches(sessionId: string, claimedAgentId: string | undefined): void {
  if (claimedAgentId && claimedAgentId !== sessionId) throw new IdentityMismatchError()
}

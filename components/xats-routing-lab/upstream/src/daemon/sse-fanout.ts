export interface SseSink {
  sendHeartbeat(): void
  close(): void
}

interface Session { agent_id: string; team: string; sink: SseSink }

const DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000

function resolveHeartbeatIntervalMs(opt?: number): number {
  if (typeof opt === 'number' && opt > 0) return opt
  const n = Number(process.env.HEARTBEAT_INTERVAL_MS)
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_HEARTBEAT_INTERVAL_MS
}

export class SseFanout {
  private sessions = new Map<string, Session>()
  private heartbeatTimer: ReturnType<typeof setInterval> | undefined
  private readonly heartbeatIntervalMs: number

  constructor(opts: { heartbeatIntervalMs?: number } = {}) {
    this.heartbeatIntervalMs = resolveHeartbeatIntervalMs(opts.heartbeatIntervalMs)
  }

  attach(agent_id: string, team: string, sink: SseSink): void {
    const prior = this.sessions.get(agent_id)
    if (prior && prior.sink !== sink) {
      try { prior.sink.close() } catch { /* ignore */ }
    }
    const wasEmpty = this.sessions.size === 0
    this.sessions.set(agent_id, { agent_id, team, sink })
    if (wasEmpty) this.startHeartbeat()
  }

  rebind(agent_id: string, team: string): void {
    const s = this.sessions.get(agent_id)
    if (!s) return
    this.sessions.set(agent_id, { agent_id, team, sink: s.sink })
  }

  detach(agent_id: string): void {
    const s = this.sessions.get(agent_id)
    if (s) { try { s.sink.close() } catch { /* ignore */ } this.sessions.delete(agent_id) }
    if (this.sessions.size === 0) this.stopHeartbeat()
  }

  stopAll(): void {
    this.stopHeartbeat()
    for (const s of this.sessions.values()) { try { s.sink.close() } catch { /* ignore */ } }
    this.sessions.clear()
  }

  peek(): Array<{ agent_id: string; team: string }> {
    return Array.from(this.sessions.values()).map(s => ({ agent_id: s.agent_id, team: s.team }))
  }

  private startHeartbeat(): void {
    if (this.heartbeatTimer) return
    this.heartbeatTimer = setInterval(() => {
      for (const s of this.sessions.values()) {
        try { s.sink.sendHeartbeat() } catch { /* ignore */ }
      }
    }, this.heartbeatIntervalMs)
    if (typeof this.heartbeatTimer.unref === 'function') this.heartbeatTimer.unref()
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = undefined }
  }
}

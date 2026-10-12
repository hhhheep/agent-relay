import type Database from 'better-sqlite3'
import type { ChannelWakeFanout, ChannelWakeSink } from '../daemon/channel-wake-fanout.js'

export const CHANNEL_PROXY_ROLE = '__channel_proxy__'

export interface SubscribeInput {
  callerAgentId: string
  channel_session_id: string
  sessionId: string
  sink: ChannelWakeSink
}

export type SubscribeResult =
  | { ok: true }
  | { error: 'unknown_agent' | 'forbidden_role' | 'invalid_channel_session_id' }

export class SubscribeChannelWakeService {
  constructor(private readonly db: Database.Database, private readonly fanout: ChannelWakeFanout) {}

  subscribe(input: SubscribeInput): SubscribeResult {
    const csid = input.channel_session_id?.trim()
    if (!csid) return { error: 'invalid_channel_session_id' }
    const row = this.db
      .prepare(`SELECT role FROM agents WHERE agent_id=?`)
      .get(input.callerAgentId) as { role: string } | undefined
    if (!row) return { error: 'unknown_agent' }
    if (row.role !== CHANNEL_PROXY_ROLE) return { error: 'forbidden_role' }
    this.fanout.attach(csid, input.sink, input.sessionId)
    return { ok: true }
  }
}

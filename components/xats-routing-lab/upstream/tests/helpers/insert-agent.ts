import type Database from 'better-sqlite3'
import {
  serializeDelivery,
  type DeliverySpec,
} from '../../src/lib/delivery-spec.js'
import type { AgentType } from '../../src/lib/agent-type.js'

export interface InsertAgentArgs {
  agent_id: string
  model?: string
  agent_type?: AgentType
  device?: string
  role?: string
  name?: string
  team?: string
  tmux_pane_id?: string | null
  runtime_ui_pid?: number | null
  delivery?: DeliverySpec
  registered_at?: string
  last_seen_at?: string
}

// Test helper: insert a row directly into `agents` with an explicit agent_id.
// Prefer this over AgentsRepo.register when tests need deterministic IDs.
export function insertAgent(db: Database.Database, args: InsertAgentArgs): string {
  const now = new Date().toISOString()
  const team = args.team ?? 'default'
  const device = args.device ?? 'local'
  const role = args.role ?? 'default'
  const name = args.name ?? args.agent_id
  const model = args.model ?? 'test-model'
  const registered_at = args.registered_at ?? now
  const last_seen_at = args.last_seen_at ?? now
  const tmux_pane_id = args.tmux_pane_id ?? null
  const runtime_ui_pid = args.runtime_ui_pid ?? null
  const delivery = serializeDelivery(args.delivery ?? { kind: 'none' })
  db.prepare(
    `INSERT INTO agents (
       agent_id, agent_type, team, role, name, model, registered_at, last_seen_at,
       device, tmux_pane_id, runtime_ui_pid, delivery_kind, delivery_payload
     )
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
     ON CONFLICT(agent_id) DO UPDATE SET
       agent_type=excluded.agent_type, device=excluded.device, team=excluded.team, role=excluded.role, name=excluded.name, model=excluded.model,
       last_seen_at=excluded.last_seen_at, tmux_pane_id=excluded.tmux_pane_id,
       runtime_ui_pid=excluded.runtime_ui_pid,
       delivery_kind=excluded.delivery_kind, delivery_payload=excluded.delivery_payload`
  ).run(
    args.agent_id,
    args.agent_type ?? null,
    team,
    role,
    name,
    model,
    registered_at,
    last_seen_at,
    device,
    tmux_pane_id,
    runtime_ui_pid,
    delivery.delivery_kind,
    delivery.delivery_payload,
  )
  return args.agent_id
}

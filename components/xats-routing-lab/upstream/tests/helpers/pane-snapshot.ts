import type { TmuxPaneRow } from '../../src/daemon/tmux-pane-list.js'
import type { PaneSnapshot, PaneSnapshotLoader } from '../../src/mcp/pane-host-verify.js'

export interface FakePane {
  pane_id: string
  tty?: string
  pane_pid?: number | null
}

export function paneRow(pane: FakePane): TmuxPaneRow {
  return {
    pane_id: pane.pane_id,
    session_name: 'sess',
    window_index: 0,
    pane_index: 0,
    active: false,
    tty: pane.tty ?? `ttys-${pane.pane_id.replace('%', '')}`,
    current_path: '/tmp',
    current_command: 'zsh',
    title: '',
    pane_pid: pane.pane_pid ?? null,
  }
}

export function paneSnapshotOf(panes: FakePane[]): PaneSnapshot {
  return new Map(panes.map(pane => [pane.pane_id, paneRow(pane)]))
}

// Test-side stand-in for the per-round tmux snapshot loader.
export function fakePaneSnapshot(panes: FakePane[]): PaneSnapshotLoader {
  const snapshot = paneSnapshotOf(panes)
  return async () => snapshot
}

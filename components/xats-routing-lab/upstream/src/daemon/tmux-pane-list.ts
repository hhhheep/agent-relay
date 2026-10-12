import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

export interface TmuxPaneRow {
  pane_id: string
  session_name: string
  window_index: number
  pane_index: number
  active: boolean
  tty: string
  current_path: string
  current_command: string
  title: string
  pane_pid: number | null
}

const TMUX_LIST_TIMEOUT_MS = 3_000

function normalizeTty(raw: string | undefined): string | undefined {
  const value = raw?.trim()
  if (!value) return undefined
  return value.replace(/^\/dev\//, '')
}

function parsePaneRows(stdout: string): TmuxPaneRow[] {
  return stdout
    .split('\n')
    .map(line => line.trimEnd())
    .filter(Boolean)
    .map((line) => {
      const [
        pane_id,
        session_name,
        window_index,
        pane_index,
        pane_active,
        pane_tty,
        pane_current_path,
        pane_current_command,
        pane_title,
        pane_pid,
      ] = line.split('\t')
      const panePid = Number(pane_pid)
      return {
        pane_id,
        session_name,
        window_index: Number(window_index),
        pane_index: Number(pane_index),
        active: pane_active === '1',
        tty: normalizeTty(pane_tty) ?? '',
        current_path: pane_current_path ?? '',
        current_command: pane_current_command ?? '',
        title: pane_title ?? '',
        pane_pid: Number.isInteger(panePid) && panePid > 0 ? panePid : null,
      }
    })
}

export async function listTmuxPaneRows(
  execLike: typeof execFile = execFile
): Promise<TmuxPaneRow[]> {
  const exec = promisify(execLike)
  const { stdout } = await exec(
    'tmux',
    [
      'list-panes',
      '-a',
      '-F',
      '#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_tty}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_title}\t#{pane_pid}',
    ],
    { timeout: TMUX_LIST_TIMEOUT_MS }
  )
  return parsePaneRows(stdout)
}

export async function listTmuxPaneIds(
  execLike: typeof execFile = execFile
): Promise<Set<string> | null> {
  try {
    const panes = await listTmuxPaneRows(execLike)
    return new Set(panes.map(pane => pane.pane_id))
  } catch {
    return null
  }
}

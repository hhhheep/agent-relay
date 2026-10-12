import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import type { DetectAgentKind } from './tmux-pane-detect.js'
import { normalizeTty, readPidInfo } from './pid-tty.js'

const TMUX_LIST_TIMEOUT_MS = 3_000
const PS_LIST_TIMEOUT_MS = 3_000

export interface BindRuntimeIdentityInput {
  agent: DetectAgentKind
  ui_pid?: number
  ui_tty?: string
  tmux_pane_id?: string
  process_pattern?: string
}

export type BindRuntimeIdentityResult =
  | {
      ok: true
      tmux_pane_id: string
      verification_mode: 'verified_pid_tty_pane' | 'verified_tty_pane'
      tty: string
      ui_pid?: number
    }
  | { error: 'invalid_runtime_identity' }
  | { error: 'invalid_process_pattern' }
  | { error: 'invalid_ui_pid' }
  | { error: 'pid_not_found' }
  | { error: 'pid_has_no_tty' }
  | { error: 'agent_process_mismatch' }
  | { error: 'invalid_ui_tty' }
  | { error: 'tmux_unavailable'; detail: string }
  | { error: 'tmux_pane_not_found' }
  | { error: 'pid_pane_tty_mismatch'; detail: { pid_tty: string; pane_tty: string } }
  | { error: 'tty_maps_to_no_agent_process' }
  | { error: 'ambiguous_tty_match'; candidates: Array<{ pane_id: string; tty: string }> }

export interface BindRuntimeIdentityDeps {
  execFile?: typeof execFile
}

interface PaneRow {
  pane_id: string
  tty: string
}

function commandPattern(args: BindRuntimeIdentityInput): RegExp | null {
  if (args.agent === 'custom') {
    const raw = args.process_pattern?.trim()
    if (!raw) return null
    return new RegExp(raw, 'i')
  }
  if (args.agent === 'codex') {
    return /(^|[\s/])(codex|codex-aarch64-a)([\s]|$)/i
  }
  if (args.agent === 'claude-code') {
    return /(^|[\s/])claude([\s]|$)/i
  }
  return /(^|[\s/])opencode([\s]|$)/i
}

async function listPanes(execLike: typeof execFile): Promise<PaneRow[]> {
  const exec = promisify(execLike)
  const { stdout } = await exec(
    'tmux',
    ['list-panes', '-a', '-F', '#{pane_id}\t#{pane_tty}'],
    { timeout: TMUX_LIST_TIMEOUT_MS }
  )
  return stdout
    .split('\n')
    .map(line => line.trimEnd())
    .filter(Boolean)
    .map((line) => {
      const [pane_id, pane_tty] = line.split('\t')
      return {
        pane_id,
        tty: normalizeTty(pane_tty) ?? '',
      }
    })
}

async function ttyProcesses(
  execLike: typeof execFile,
  tty: string
): Promise<string[]> {
  const exec = promisify(execLike)
  const { stdout } = await exec(
    'ps',
    ['-t', tty, '-o', 'pid=,ppid=,stat=,command='],
    { timeout: PS_LIST_TIMEOUT_MS }
  )
  return stdout
    .split('\n')
    .map(line => line.trimEnd())
    .filter(Boolean)
}

function matchAgentProcess(
  agent: DetectAgentKind,
  lines: string[],
  pattern: RegExp
): boolean {
  return lines.some((line) => {
    if (isHelperProcess(agent, line)) return false
    return pattern.test(line)
  })
}

function isHelperProcess(agent: DetectAgentKind, command: string): boolean {
  if (agent !== 'codex') return false
  return /codex\s+app-server/i.test(command) ||
    /Codex Computer Use\.app/i.test(command) ||
    /SkyComputerUseClient/i.test(command)
}

export async function bindRuntimeIdentity(
  input: BindRuntimeIdentityInput,
  deps: BindRuntimeIdentityDeps = {}
): Promise<BindRuntimeIdentityResult> {
  const execLike = deps.execFile ?? execFile
  const pattern = commandPattern(input)
  if (!pattern) return { error: 'invalid_process_pattern' }

  let panes: PaneRow[]
  try {
    panes = await listPanes(execLike)
  } catch (error) {
    return {
      error: 'tmux_unavailable',
      detail: error instanceof Error ? error.message : String(error),
    }
  }

  if (input.ui_pid !== undefined) {
    if (!Number.isInteger(input.ui_pid) || input.ui_pid <= 0) {
      return { error: 'invalid_ui_pid' }
    }
    const pidInfo = await readPidInfo(execLike, input.ui_pid)
    if (!pidInfo.found) return { error: 'pid_not_found' }
    if (
      !pidInfo.command ||
      isHelperProcess(input.agent, pidInfo.command) ||
      !pattern.test(pidInfo.command)
    ) {
      return { error: 'agent_process_mismatch' }
    }
    if (!pidInfo.tty) return { error: 'pid_has_no_tty' }
    const candidates = panes.filter(pane => pane.tty === pidInfo.tty)
    if (candidates.length === 0) return { error: 'tmux_pane_not_found' }
    if (candidates.length > 1) {
      return {
        error: 'ambiguous_tty_match',
        candidates: candidates.map(candidate => ({
          pane_id: candidate.pane_id,
          tty: candidate.tty,
        })),
      }
    }
    const candidate = candidates[0]
    const explicitPane = input.tmux_pane_id?.trim()
    if (explicitPane && explicitPane !== candidate.pane_id) {
      return {
        error: 'pid_pane_tty_mismatch',
        detail: {
          pid_tty: pidInfo.tty,
          pane_tty: candidate.tty,
        },
      }
    }
    return {
      ok: true,
      tmux_pane_id: candidate.pane_id,
      verification_mode: 'verified_pid_tty_pane',
      tty: pidInfo.tty,
      ui_pid: input.ui_pid,
    }
  }

  const tty = normalizeTty(input.ui_tty)
  const paneId = input.tmux_pane_id?.trim()
  if (!tty || !paneId) return { error: 'invalid_runtime_identity' }
  const pane = panes.find(candidate => candidate.pane_id === paneId)
  if (!pane) return { error: 'tmux_pane_not_found' }
  if (pane.tty !== tty) {
    return {
      error: 'pid_pane_tty_mismatch',
      detail: {
        pid_tty: tty,
        pane_tty: pane.tty,
      },
    }
  }
  const processes = await ttyProcesses(execLike, tty)
  if (!matchAgentProcess(input.agent, processes, pattern)) {
    return { error: 'tty_maps_to_no_agent_process' }
  }
  return {
    ok: true,
    tmux_pane_id: paneId,
    verification_mode: 'verified_tty_pane',
    tty,
  }
}

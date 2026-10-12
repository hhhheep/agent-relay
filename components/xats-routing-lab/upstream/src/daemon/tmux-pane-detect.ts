import { execFile } from 'node:child_process'
import { normalize, sep } from 'node:path'
import { promisify } from 'node:util'
import { listTmuxPaneRows, type TmuxPaneRow as PaneRow } from './tmux-pane-list.js'

export type DetectAgentKind = 'codex' | 'claude-code' | 'opencode' | 'custom'

export interface DetectTmuxPaneInput {
  agent: DetectAgentKind
  cwd?: string
  tty?: string
  title_contains?: string
  process_pattern?: string
}

export interface DetectTmuxPaneCandidate {
  pane_id: string
  session_name: string
  window_index: number
  pane_index: number
  active: boolean
  tty: string
  current_path: string
  current_command: string
  title: string
  matched_processes: string[]
  score: number
}

export type DetectTmuxPaneResult =
  | { ok: true; pane: DetectTmuxPaneCandidate; candidates: DetectTmuxPaneCandidate[] }
  | { error: 'not_found'; candidates: [] }
  | { error: 'ambiguous_match'; candidates: DetectTmuxPaneCandidate[] }
  | { error: 'tmux_unavailable'; detail: string }

export interface DetectTmuxPaneDeps {
  execFile?: typeof execFile
}

const PS_LIST_TIMEOUT_MS = 3_000

function normalizeTty(raw: string | undefined): string | undefined {
  const value = raw?.trim()
  if (!value) return undefined
  return value.replace(/^\/dev\//, '')
}

function normalizePath(raw: string | undefined): string | undefined {
  const value = raw?.trim()
  if (!value) return undefined
  return normalize(value)
}

function pathRelated(candidatePath: string, inputPath: string): 'exact' | 'ancestor' | 'descendant' | 'none' {
  const candidate = normalize(candidatePath)
  const input = normalize(inputPath)
  if (candidate === input) return 'exact'
  if (candidate.startsWith(`${input}${sep}`)) return 'descendant'
  if (input.startsWith(`${candidate}${sep}`)) return 'ancestor'
  return 'none'
}

function commandPattern(args: DetectTmuxPaneInput): RegExp {
  if (args.agent === 'custom') {
    const raw = args.process_pattern?.trim()
    if (!raw) throw new Error('process_pattern is required when agent=custom')
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

function commandHintScore(agent: DetectAgentKind, command: string): number {
  if (agent === 'codex' && /codex/i.test(command)) return 6
  if (agent === 'opencode' && /opencode/i.test(command)) return 6
  if (agent === 'claude-code' && /^(\d+\.)+\d+$/.test(command)) return 4
  return 0
}

function isHelperProcess(agent: DetectAgentKind, command: string): boolean {
  if (agent !== 'codex') return false
  return /codex\s+app-server/i.test(command) ||
    /Codex Computer Use\.app/i.test(command) ||
    /SkyComputerUseClient/i.test(command)
}

async function ttyProcesses(execLike: typeof execFile, tty: string): Promise<string[]> {
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

function collectCandidates(
  panes: PaneRow[],
  ttyMap: Map<string, string[]>,
  input: DetectTmuxPaneInput
): DetectTmuxPaneCandidate[] {
  const ttyFilter = normalizeTty(input.tty)
  const cwdFilter = normalizePath(input.cwd)
  const titleFilter = input.title_contains?.trim().toLowerCase()
  const pattern = commandPattern(input)
  const candidates: DetectTmuxPaneCandidate[] = []

  for (const pane of panes) {
    if (ttyFilter && pane.tty !== ttyFilter) continue
    if (cwdFilter) {
      const relation = pathRelated(pane.current_path, cwdFilter)
      if (relation === 'none') continue
    }
    if (titleFilter && !pane.title.toLowerCase().includes(titleFilter)) continue

    const matched_processes = (ttyMap.get(pane.tty) ?? []).filter((line) => {
      if (isHelperProcess(input.agent, line)) return false
      return pattern.test(line)
    })
    if (matched_processes.length === 0) continue

    let score = matched_processes.length * 10
    if (pane.active) score += 3
    score += commandHintScore(input.agent, pane.current_command)
    if (ttyFilter) score += 100
    if (cwdFilter) {
      const relation = pathRelated(pane.current_path, cwdFilter)
      if (relation === 'exact') score += 60
      else if (relation === 'descendant') score += 45
      else if (relation === 'ancestor') score += 30
    }
    if (titleFilter) score += 15

    candidates.push({
      pane_id: pane.pane_id,
      session_name: pane.session_name,
      window_index: pane.window_index,
      pane_index: pane.pane_index,
      active: pane.active,
      tty: pane.tty,
      current_path: pane.current_path,
      current_command: pane.current_command,
      title: pane.title,
      matched_processes,
      score,
    })
  }

  return candidates.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score
    if (a.pane_id < b.pane_id) return -1
    if (a.pane_id > b.pane_id) return 1
    return 0
  })
}

export async function detectTmuxPane(
  input: DetectTmuxPaneInput,
  deps: DetectTmuxPaneDeps = {}
): Promise<DetectTmuxPaneResult> {
  const execLike = deps.execFile ?? execFile
  let panes: PaneRow[]
  try {
    panes = await listTmuxPaneRows(execLike)
  } catch (error) {
    return {
      error: 'tmux_unavailable',
      detail: error instanceof Error ? error.message : String(error),
    }
  }

  const ttyMap = new Map<string, string[]>()
  for (const pane of panes) {
    if (!pane.tty || ttyMap.has(pane.tty)) continue
    try {
      ttyMap.set(pane.tty, await ttyProcesses(execLike, pane.tty))
    } catch {
      ttyMap.set(pane.tty, [])
    }
  }

  let candidates: DetectTmuxPaneCandidate[]
  try {
    candidates = collectCandidates(panes, ttyMap, input)
  } catch (error) {
    return {
      error: 'not_found',
      candidates: [],
    }
  }

  if (candidates.length === 0) return { error: 'not_found', candidates: [] }

  const topScore = candidates[0].score
  const top = candidates.filter(candidate => candidate.score === topScore)
  if (top.length > 1) {
    return {
      error: 'ambiguous_match',
      candidates,
    }
  }

  return {
    ok: true,
    pane: candidates[0],
    candidates,
  }
}

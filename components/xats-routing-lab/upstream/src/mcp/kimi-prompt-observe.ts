type FetchLike = typeof globalThis.fetch

export const DEFAULT_KIMI_PROMPT_OBSERVE_MS = 10 * 60_000

export interface KimiPromptObserveArgs {
  base_url: string
  session_id: string
  prompt_id: string
  headers: Record<string, string>
  fetch: FetchLike
}

export interface KimiPromptStillActiveRecord {
  event: 'kimi_prompt_still_active'
  session_id: string
  prompt_id: string
  elapsed_ms: number
}

export type KimiPromptObserveFn = (args: KimiPromptObserveArgs) => void

const ACTIVE_STATUSES = new Set(['running', 'active', 'in_progress', 'pending', 'queued'])

const timers = new Set<ReturnType<typeof setTimeout>>()

function readThresholdFromEnv(env: NodeJS.ProcessEnv): number | undefined {
  const raw = env.XATS_KIMI_PROMPT_OBSERVE_MS
  if (typeof raw !== 'string' || raw.trim() === '') return undefined
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed <= 0) return undefined
  return parsed
}

function extractPrompts(bodyText: string): Array<Record<string, unknown>> | undefined {
  if (bodyText === '') return undefined
  let parsed: unknown
  try {
    parsed = JSON.parse(bodyText)
  } catch {
    return undefined
  }
  if (typeof parsed !== 'object' || parsed === null) return undefined
  const root = parsed as Record<string, unknown>
  const code = root.code
  if (typeof code === 'number' && code !== 0) return undefined
  const data = typeof root.data === 'object' && root.data !== null ? root.data : root
  const list = Array.isArray(data)
    ? data
    : (data as Record<string, unknown>).prompts
  if (!Array.isArray(list)) return undefined
  return list.filter(
    (x): x is Record<string, unknown> => typeof x === 'object' && x !== null
  )
}

async function isPromptStillActive(args: KimiPromptObserveArgs): Promise<boolean> {
  let response: Response
  try {
    response = await args.fetch(
      `${args.base_url.replace(/\/+$/, '')}/api/v1/sessions/${encodeURIComponent(args.session_id)}/prompts`,
      { method: 'GET', headers: args.headers }
    )
  } catch {
    return false
  }
  if (!response.ok) return false
  let bodyText = ''
  try {
    bodyText = await response.text()
  } catch {
    return false
  }
  const prompts = extractPrompts(bodyText)
  if (!prompts) return false
  const entry = prompts.find(p => p.id === args.prompt_id || p.prompt_id === args.prompt_id)
  if (!entry) return false
  if (entry.active === true) return true
  return typeof entry.status === 'string' && ACTIVE_STATUSES.has(entry.status)
}

/**
 * Observe-only watch on an injected turn. Elapsed time cannot distinguish a
 * stuck turn from a productive one, so this NEVER aborts the prompt and no
 * abort option is exposed; its entire output is a log record. State is
 * in-memory and is forgotten on daemon restart.
 */
export function createKimiPromptObserver(opts: {
  thresholdMs?: number
  log?: (record: KimiPromptStillActiveRecord) => void
  env?: NodeJS.ProcessEnv
} = {}): KimiPromptObserveFn {
  const thresholdMs =
    opts.thresholdMs ??
    readThresholdFromEnv(opts.env ?? process.env) ??
    DEFAULT_KIMI_PROMPT_OBSERVE_MS
  const log = opts.log ?? ((record) => { console.error(JSON.stringify(record)) })

  return (args) => {
    const timer = setTimeout(() => {
      timers.delete(timer)
      void isPromptStillActive(args).then(active => {
        if (!active) return
        log({
          event: 'kimi_prompt_still_active',
          session_id: args.session_id,
          prompt_id: args.prompt_id,
          elapsed_ms: thresholdMs,
        })
      })
    }, thresholdMs)
    timer.unref?.()
    timers.add(timer)
  }
}

export const observeKimiPrompt: KimiPromptObserveFn = createKimiPromptObserver()

export function clearAllKimiPromptObservations(): void {
  for (const timer of timers) clearTimeout(timer)
  timers.clear()
}

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  createKimiPromptObserver,
  clearAllKimiPromptObservations,
  DEFAULT_KIMI_PROMPT_OBSERVE_MS,
} from '../src/mcp/kimi-prompt-observe.js'

const BASE_URL = 'http://127.0.0.1:58627'
const SESSION_ID = 'session_abc'
const PROMPT_ID = 'prompt_1'

type Call = { method: string; url: string }

function makeFetch(body: string | undefined, opts: { status?: number; reject?: boolean } = {}) {
  const calls: Call[] = []
  const fetchMock = (async (url: string, init?: RequestInit) => {
    if (opts.reject) throw new Error('ECONNREFUSED')
    calls.push({ method: init?.method ?? 'GET', url })
    const text = body ?? ''
    return new Response(text.length > 0 ? text : null, { status: opts.status ?? 200 })
  }) as unknown as typeof fetch
  return { fetch: fetchMock, calls }
}

function promptsEnvelope(prompts: unknown[]): string {
  return JSON.stringify({ code: 0, msg: 'ok', data: { prompts } })
}

describe('kimi prompt observation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    clearAllKimiPromptObservations()
  })
  afterEach(() => {
    clearAllKimiPromptObservations()
    vi.useRealTimers()
  })

  it('defaults the threshold to 10 minutes', () => {
    expect(DEFAULT_KIMI_PROMPT_OBSERVE_MS).toBe(10 * 60_000)
  })

  function observe(args: {
    body?: string
    status?: number
    reject?: boolean
    thresholdMs?: number
  }) {
    const logs: unknown[] = []
    const { fetch: fetchMock, calls } = makeFetch(args.body, {
      status: args.status,
      reject: args.reject,
    })
    const observer = createKimiPromptObserver({
      thresholdMs: args.thresholdMs ?? 1_000,
      log: record => { logs.push(record) },
    })
    observer({
      base_url: BASE_URL,
      session_id: SESSION_ID,
      prompt_id: PROMPT_ID,
      headers: { Authorization: 'Bearer t' },
      fetch: fetchMock,
    })
    return { logs, calls }
  }

  it('logs exactly one record when the prompt is still active at the threshold', async () => {
    const { logs, calls } = observe({
      body: promptsEnvelope([{ id: PROMPT_ID, status: 'running' }]),
    })
    expect(logs).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(logs).toHaveLength(1)
    expect(logs[0]).toMatchObject({
      event: 'kimi_prompt_still_active',
      session_id: SESSION_ID,
      prompt_id: PROMPT_ID,
    })
    // No further checks and, above all, no abort.
    await vi.advanceTimersByTimeAsync(3_600_000)
    expect(logs).toHaveLength(1)
    expect(calls).toEqual([
      { method: 'GET', url: `${BASE_URL}/api/v1/sessions/${SESSION_ID}/prompts` },
    ])
  })

  it('logs nothing when the prompt has finished', async () => {
    const { logs, calls } = observe({
      body: promptsEnvelope([{ id: PROMPT_ID, status: 'completed' }]),
    })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(logs).toHaveLength(0)
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe('GET')
  })

  it('logs nothing when the prompt is absent from the list', async () => {
    const { logs } = observe({ body: promptsEnvelope([{ id: 'other', status: 'running' }]) })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(logs).toHaveLength(0)
  })

  it('never issues a non-GET request in either case', async () => {
    for (const body of [
      promptsEnvelope([{ id: PROMPT_ID, status: 'running' }]),
      promptsEnvelope([{ id: PROMPT_ID, status: 'completed' }]),
    ]) {
      clearAllKimiPromptObservations()
      const { calls } = observe({ body })
      await vi.advanceTimersByTimeAsync(1_000)
      expect(calls.every(c => c.method === 'GET')).toBe(true)
      expect(calls.some(c => /abort|cancel|interrupt|stop/i.test(c.url))).toBe(false)
    }
  })

  it('stays silent when the status check itself fails', async () => {
    for (const args of [{ reject: true }, { status: 500, body: 'boom' }, { body: 'not json' }]) {
      clearAllKimiPromptObservations()
      const { logs } = observe(args)
      await vi.advanceTimersByTimeAsync(1_000)
      expect(logs).toHaveLength(0)
    }
  })

  it('does not check before the threshold elapses', async () => {
    const { logs, calls } = observe({
      body: promptsEnvelope([{ id: PROMPT_ID, status: 'running' }]),
      thresholdMs: 60_000,
    })
    await vi.advanceTimersByTimeAsync(59_000)
    expect(calls).toHaveLength(0)
    expect(logs).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(calls).toHaveLength(1)
    expect(logs).toHaveLength(1)
  })
})

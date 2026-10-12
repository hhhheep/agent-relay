import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as cp from 'node:child_process'

vi.mock('node:child_process', () => ({
  execFile: vi.fn(),
  spawn: vi.fn()
}))

describe('tmux-cli wrappers', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const mod = await import('../src/daemon/tmux-cli.js')
    mod._resetTmuxAvailableCache()
  })

  it('isTmuxAvailable returns true when tmux -V exits 0', async () => {
    const { isTmuxAvailable } = await import('../src/daemon/tmux-cli.js')
    ;(cp.execFile as unknown as ReturnType<typeof vi.fn>).mockImplementation((_cmd: string, _args: string[], cb: (e: Error | null, r: { stdout: string; stderr: string }) => void) => cb(null, { stdout: 'tmux 3.4\n', stderr: '' }))
    expect(await isTmuxAvailable()).toBe(true)
    expect(cp.execFile).toHaveBeenCalledWith('tmux', ['-V'], expect.anything())
  })

  it('capturePaneTail invokes tmux capture-pane with -t/-p/-S args and a timeout', async () => {
    const { capturePaneTail } = await import('../src/daemon/tmux-cli.js')
    ;(cp.execFile as unknown as ReturnType<typeof vi.fn>).mockImplementation((..._args: unknown[]) => {
      const cb = _args[_args.length - 1] as (e: Error | null, r: { stdout: string; stderr: string }) => void
      cb(null, { stdout: 'line1\nline2\n', stderr: '' })
    })
    const tail = await capturePaneTail('%42', 8)
    expect(tail).toContain('line1')
    const call = (cp.execFile as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[1]).toEqual(['capture-pane', '-t', '%42', '-p', '-S', '-8'])
    expect(call[2]).toMatchObject({ timeout: expect.any(Number) })
    expect(call[2].timeout).toBeGreaterThan(0)
  })

  it('loadBuffer sends prompt bytes via stdin (not argv)', async () => {
    const { loadBuffer } = await import('../src/daemon/tmux-cli.js')
    const written: Buffer[] = []
    const fakeChild = {
      stdin: { write: (b: Buffer) => { written.push(b); return true }, end: vi.fn() },
      on: vi.fn((evt: string, cb: (code: number) => void) => { if (evt === 'close') setImmediate(() => cb(0)) })
    }
    ;(cp.spawn as unknown as ReturnType<typeof vi.fn>).mockReturnValue(fakeChild)
    await loadBuffer('poke-abc', 'hello 世界')
    expect(cp.spawn).toHaveBeenCalledWith('tmux', ['load-buffer', '-b', 'poke-abc', '-'])
    expect(Buffer.concat(written).toString('utf8')).toBe('hello 世界')
  })

  it('pasteBuffer uses bracketed paste and delete-after', async () => {
    const { pasteBuffer } = await import('../src/daemon/tmux-cli.js')
    ;(cp.execFile as unknown as ReturnType<typeof vi.fn>).mockImplementation((_cmd: string, _args: string[], cb: (e: Error | null, r: { stdout: string; stderr: string }) => void) => cb(null, { stdout: '', stderr: '' }))
    await pasteBuffer('poke-abc', '%42')
    const args = (cp.execFile as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1]
    expect(args).toEqual(['paste-buffer', '-b', 'poke-abc', '-t', '%42', '-p', '-d'])
  })

  it('sendEnter sends only the Enter key', async () => {
    const { sendEnter } = await import('../src/daemon/tmux-cli.js')
    ;(cp.execFile as unknown as ReturnType<typeof vi.fn>).mockImplementation((_cmd: string, _args: string[], cb: (e: Error | null, r: { stdout: string; stderr: string }) => void) => cb(null, { stdout: '', stderr: '' }))
    await sendEnter('%42')
    const args = (cp.execFile as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1]
    expect(args).toEqual(['send-keys', '-t', '%42', 'Enter'])
  })
})

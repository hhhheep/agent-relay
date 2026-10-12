import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as cp from 'node:child_process'

vi.mock('node:child_process', () => ({
  execFile: vi.fn(),
}))

describe('bindRuntimeIdentity', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  function mockExecFile(map: Record<string, string>): void {
    ;(cp.execFile as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (
        cmd: string,
        args: string[],
        options: unknown,
        cb?: (error: Error | null, result: { stdout: string; stderr: string }) => void
      ) => {
        const done = typeof options === 'function' ? options : cb
        const key = [cmd, ...args].join(' ')
        if (!done) throw new Error('missing callback')
        if (!(key in map)) {
          done(new Error(`unexpected execFile call: ${key}`), { stdout: '', stderr: '' })
          return
        }
        done(null, { stdout: map[key], stderr: '' })
      }
    )
  }

  it('binds by ui_pid via pid -> tty -> pane verification', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{pane_tty}":
        '%1902\t/dev/ttys026\n',
      'ps -p 81979 -o tty=,command=':
        'ttys026 codex --dangerously-bypass-approvals-and-sandbox --remote ws://127.0.0.1:8799\n',
    })
    const { bindRuntimeIdentity } = await import('../src/daemon/runtime-identity.js')

    const result = await bindRuntimeIdentity({
      agent: 'codex',
      ui_pid: 81979,
    })

    expect(result).toEqual({
      ok: true,
      tmux_pane_id: '%1902',
      verification_mode: 'verified_pid_tty_pane',
      tty: 'ttys026',
      ui_pid: 81979,
    })
  })

  it('rejects pid whose process command does not match the claimed agent', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{pane_tty}":
        '%1902\t/dev/ttys026\n',
      'ps -p 81979 -o tty=,command=':
        'ttys026 /bin/zsh -l\n',
    })
    const { bindRuntimeIdentity } = await import('../src/daemon/runtime-identity.js')

    const result = await bindRuntimeIdentity({
      agent: 'codex',
      ui_pid: 81979,
    })

    expect(result).toEqual({ error: 'agent_process_mismatch' })
  })

  it('rejects Codex app-server pid as a visible UI pid', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{pane_tty}":
        '%1993\t/dev/ttys065\n',
      'ps -p 23201 -o tty=,command=':
        'ttys065 codex app-server --listen ws://127.0.0.1:8799\n',
    })
    const { bindRuntimeIdentity } = await import('../src/daemon/runtime-identity.js')

    const result = await bindRuntimeIdentity({
      agent: 'codex',
      ui_pid: 23201,
    })

    expect(result).toEqual({ error: 'agent_process_mismatch' })
  })

  it('binds by ui_tty + tmux_pane_id when the tty hosts a matching process', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{pane_tty}":
        '%1916\t/dev/ttys020\n',
      'ps -t ttys020 -o pid=,ppid=,stat=,command=':
        '65887 256 S+ /Users/jtianling/.local/bin/claude --dangerously-skip-permissions\n',
    })
    const { bindRuntimeIdentity } = await import('../src/daemon/runtime-identity.js')

    const result = await bindRuntimeIdentity({
      agent: 'claude-code',
      ui_tty: '/dev/ttys020',
      tmux_pane_id: '%1916',
    })

    expect(result).toEqual({
      ok: true,
      tmux_pane_id: '%1916',
      verification_mode: 'verified_tty_pane',
      tty: 'ttys020',
    })
  })

  it('rejects a tty that only hosts Codex helper processes', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{pane_tty}":
        '%1993\t/dev/ttys065\n',
      'ps -t ttys065 -o pid=,ppid=,stat=,command=':
        [
          '23201     1 S    codex app-server --listen ws://127.0.0.1:8799',
          '26423 23201 S    ./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient mcp',
        ].join('\n'),
    })
    const { bindRuntimeIdentity } = await import('../src/daemon/runtime-identity.js')

    const result = await bindRuntimeIdentity({
      agent: 'codex',
      ui_tty: '/dev/ttys065',
      tmux_pane_id: '%1993',
    })

    expect(result).toEqual({ error: 'tty_maps_to_no_agent_process' })
  })
})

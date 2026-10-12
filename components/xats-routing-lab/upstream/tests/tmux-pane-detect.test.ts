import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as cp from 'node:child_process'

vi.mock('node:child_process', () => ({
  execFile: vi.fn(),
}))

describe('detectTmuxPane', () => {
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

  it('finds the codex pane by tty process plus cwd', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_tty}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_title}\t#{pane_pid}":
        [
          '%1863\ts1\t0\t2\t0\t/dev/ttys015\t/Users/me/project\tzsh\tproject',
          '%1902\ts1\t0\t3\t1\t/dev/ttys026\t/Users/me/project\tcodex-aarch64-a\tproject',
        ].join('\n'),
      'ps -t ttys015 -o pid=,ppid=,stat=,command=':
        '15043     1 S    codex app-server --listen ws://127.0.0.1:8799\n51798 95545 Ss+  /bin/zsh -l\n',
      'ps -t ttys026 -o pid=,ppid=,stat=,command=':
        '32657 99672 S+   codex --dangerously-bypass-approvals-and-sandbox --remote ws://127.0.0.1:8799\n99672 95545 Ss   /bin/zsh -l\n',
    })
    const { detectTmuxPane } = await import('../src/daemon/tmux-pane-detect.js')

    const result = await detectTmuxPane({
      agent: 'codex',
      cwd: '/Users/me/project',
    })

    expect(result).toMatchObject({
      ok: true,
      pane: {
        pane_id: '%1902',
        tty: 'ttys026',
        current_path: '/Users/me/project',
      },
    })
  })

  it('ignores Codex helper panes when detecting the visible Codex pane', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_tty}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_title}\t#{pane_pid}":
        [
          '%1972\ts1\t0\t1\t1\t/dev/ttys026\t/Users/me/project\tcodex-aarch64-a\tproject',
          '%1993\ts1\t0\t2\t0\t/dev/ttys065\t/Users/me/project\tzsh\tproject',
        ].join('\n'),
      'ps -t ttys026 -o pid=,ppid=,stat=,command=':
        '26395 23186 S+   codex --dangerously-bypass-approvals-and-sandbox --remote ws://127.0.0.1:8799\n',
      'ps -t ttys065 -o pid=,ppid=,stat=,command=':
        [
          '23201     1 S    codex app-server --listen ws://127.0.0.1:8799',
          '26423 23201 S    ./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient mcp',
        ].join('\n'),
    })
    const { detectTmuxPane } = await import('../src/daemon/tmux-pane-detect.js')

    const result = await detectTmuxPane({
      agent: 'codex',
      cwd: '/Users/me/project',
    })

    expect(result).toMatchObject({
      ok: true,
      pane: {
        pane_id: '%1972',
        tty: 'ttys026',
      },
      candidates: [
        {
          pane_id: '%1972',
        },
      ],
    })
  })

  it('finds the claude code pane from tty processes even when pane_current_command is version-like', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_tty}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_title}\t#{pane_pid}":
        '%2\ts1\t0\t0\t1\t/dev/ttys002\t/Users/me/agent-of-empires\t2.1.114\t✳ Claude Code\n',
      'ps -t ttys002 -o pid=,ppid=,stat=,command=':
        '45418 95545 S+   claude --resume a00d0fdf-684c-43aa-8549-7b1fc58d12c1 --dangerously-skip-permissions\n',
    })
    const { detectTmuxPane } = await import('../src/daemon/tmux-pane-detect.js')

    const result = await detectTmuxPane({
      agent: 'claude-code',
      cwd: '/Users/me/agent-of-empires',
    })

    expect(result).toMatchObject({
      ok: true,
      pane: {
        pane_id: '%2',
        current_command: '2.1.114',
      },
    })
  })

  it('returns ambiguous_match when two opencode panes tie on score', async () => {
    mockExecFile({
      "tmux list-panes -a -F #{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_tty}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_title}\t#{pane_pid}":
        [
          '%10\ts1\t0\t0\t0\t/dev/ttys010\t/Users/me/a\topencode\ta',
          '%11\ts1\t0\t1\t0\t/dev/ttys011\t/Users/me/b\topencode\tb',
        ].join('\n'),
      'ps -t ttys010 -o pid=,ppid=,stat=,command=':
        '100 1 S+   opencode --session a\n',
      'ps -t ttys011 -o pid=,ppid=,stat=,command=':
        '101 1 S+   opencode --session b\n',
    })
    const { detectTmuxPane } = await import('../src/daemon/tmux-pane-detect.js')

    const result = await detectTmuxPane({
      agent: 'opencode',
    })

    expect(result).toMatchObject({
      error: 'ambiguous_match',
      candidates: [
        { pane_id: '%10' },
        { pane_id: '%11' },
      ],
    })
  })
})

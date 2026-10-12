import { runQuietGuard } from '../../src/mcp/poke-guard.js'
import type { AutoPokeArgs, AutoPokeFn, AutoPokeSkipReason } from '../../src/mcp/auto-poke-fanout.js'

// The fan-out no longer runs the quiet-guard; the poke primitive does (and skips
// it when skipGuard is set). For tests that drive pane state via
// __setCapturePaneTail and a fake poke fn, this wrapper mirrors the real
// primitive: it runs runQuietGuard before recording/forwarding the poke unless
// skipGuard is set, so active panes resolve to guard_failed exactly as the real
// primitive would.
//
// Caveat: this models the tmux-only path, gating on paneId presence. The real
// primitive guards only when dispatch actually reaches the tmux branch — a
// recipient with a live non-tmux transport (e.g. an online claude-channel sink)
// delivers there and never guards even if it also has a pane. Do NOT use this
// helper for a fixture that has BOTH a pane and a reachable non-tmux transport;
// it would run the guard where the real system would not.
export function guardingPoke(
  inner: AutoPokeFn,
  onCall?: (args: AutoPokeArgs) => void
): AutoPokeFn {
  return async (args) => {
    if (!args.skipGuard && args.paneId) {
      const guard = await runQuietGuard(args.paneId)
      if (guard === 'fail') {
        return { ok: false, reason: 'guard_failed' as AutoPokeSkipReason }
      }
    }
    onCall?.(args)
    return inner(args)
  }
}

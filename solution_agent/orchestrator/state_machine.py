from __future__ import annotations

# Valid task states, including intermediate remote-launch states used by run_remote().
ALL_STATES: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "launching",        # SSH launch in progress; transient
        "launch_failed",    # SSH launch definitively failed
        "launch_unknown",   # SSH launch timed out; remote state uncertain
        "completed",
        "partial",
        "failed",
    }
)

# States that have no valid outgoing transitions (hard stops).
TERMINAL_STATES: frozenset[str] = frozenset({"completed", "launch_failed", "launch_unknown"})

# Non-terminal states; any task stuck here indefinitely is a candidate for stale recovery.
NON_TERMINAL_STATES: frozenset[str] = ALL_STATES - TERMINAL_STATES

# Terminal states that can be requeued via an explicit manual action.
RESUMABLE_STATES: frozenset[str] = frozenset({"partial", "failed", "launch_failed", "launch_unknown"})

# Directed adjacency map.  Key = current state, value = set of reachable states.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending":        frozenset({"running"}),
    "running":        frozenset({"launching", "completed", "partial", "failed"}),
    "launching":      frozenset({"running", "launch_failed", "launch_unknown"}),
    "launch_failed":  frozenset({"pending", "running", "launching"}),
    "launch_unknown": frozenset({"pending", "running"}),
    "completed":      frozenset(),
    "partial":        frozenset({"running", "pending"}),
    "failed":         frozenset({"running", "pending"}),
}


class InvalidTransitionError(ValueError):
    def __init__(self, from_state: str, to_state: str, *, source: str = "unknown") -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.source = source
        allowed = sorted(ALLOWED_TRANSITIONS.get(from_state, frozenset()))
        hint = f"allowed from {from_state!r}: {allowed}" if allowed else f"{from_state!r} is a terminal state"
        super().__init__(
            f"invalid transition {from_state!r} → {to_state!r} (source={source!r}); {hint}"
        )


def validate_transition(from_state: str, to_state: str, *, source: str = "unknown") -> None:
    """Raise InvalidTransitionError if from_state → to_state is not in ALLOWED_TRANSITIONS.

    No-op when from_state == to_state (idempotent writes are always safe).
    Unknown states are rejected unless they match a known state.
    """
    if from_state == to_state:
        return
    if from_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(from_state, to_state, source=source)
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise InvalidTransitionError(from_state, to_state, source=source)


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_resumable(state: str) -> bool:
    return state in RESUMABLE_STATES

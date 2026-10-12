# Remote research workflow

Use this workflow for long-running remote research tasks. It complements the command-output guard; it does not replace experiment authorization, scientific review, or the orchestrator's lifecycle rules.

## Stage contract

Each Codex task owns one bounded stage with:

- one objective;
- explicit acceptance checks;
- an allowed working directory;
- a command/output budget;
- a terminal result of `go`, `stop`, or `defer`;
- one `stage_receipt.json` and one short `handoff.md`.

For v0.2 stages, the append-only stage ledger is the execution authority. Generate routine context from its `status` and `change` views, retrieve decision-critical lines through its verified `evidence` view, and produce the terminal handoff only after `close`. A manually written receipt must not override contradictory ledger state.

Do not continue the same Codex conversation into the next stage. Start a fresh task with the validated stage receipt, the short handoff, and only the evidence needed for the next decision.

## Execution

- Route stage commands through `stage_ledger.py run`, which invokes `token_guard.py`, enforces budgets, and reuses an earlier result only when its declared-input fingerprint and raw evidence verify.
- Declare every result-affecting file with `--input`. If dependency coverage is uncertain, do not accept a cache hit.
- A direct command is acceptable when its output is predictably small or exact interactivity is required. Count it as unguarded and record the reason.
- Full output stays under `.token-guard/runs/<run-id>/` on the workplace.
- Long GPU or scheduler jobs run in tmux or the orchestrator. Do not wake the model on a short polling loop. Return only on completion, failure, a material state transition, or required user action.
- A receipt is navigation. Before changing code from a failure, or deciding a scientific gate from a number, verify the relevant Token Guard run and retrieve exact evidence.

## Stage close

Write the stage receipt from [stage-receipt.template.json](stage-receipt.template.json), then validate it:

```text
<python> <skill-dir>/scripts/validate_stage_receipt.py <stage_receipt.json> --root <workspace> --check-artifacts --verify-token-guard
```

The stage is not closed if validation fails. Do not paper over missing evidence with prose.

The handoff should normally remain under 2,000 tokens and contain only:

- the outcome and gate decision;
- evidence paths and hashes;
- unresolved uncertainty;
- the next bounded objective;
- assumptions the next task must not make.

## Reporting

After the final scientific gate, prepare a compact evidence packet for the report writer. The report writer may phrase the approved interpretation but must not invent measurements or authorize a new experiment. Codex performs one evidence audit of the canonical report; avoid creating parallel full reports.

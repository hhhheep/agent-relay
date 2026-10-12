---
name: research-token-guard
description: Capture bounded, hash-verifiable receipts for commands expected to emit large or repeatedly consulted output. Use when omitted output may later support a scientific or code decision; do not use for ordinary small commands, normal file reads, or as a universal shell wrapper.
---

# Research Token Guard

Use the bundled deterministic guard only when command output is predictably
large, repetitive, or likely to be reread. It limits model-facing output while
preserving exact stdout and stderr on disk. It does not make a command safe,
authorize side effects, or replace normal execution approval.

## Minimal workflow

1. On a new host, run `token_guard.py doctor` once.
2. Run the noisy command through `token_guard.py run -- ...`, or use the atomic
   stage-ledger interface when the inputs and evidence queries are already
   known.
3. If `lossy` is false, `inline_output` is complete. If it is true, treat the
   receipt as navigation and retrieve only the exact lines needed with
   `excerpt`.
4. Verify the receipt before a scientific conclusion, numeric comparison, or
   code change that depends on omitted output.
5. Reuse unchanged evidence by its stream hash; do not retrieve the same stream
   repeatedly.

Prefer a producing program's compact schema-checked JSON over mining a human
log. Preserve stream hashes, byte and line counts, exit status, timeout state,
and command identity. A failed or timed-out command requires exact failure
evidence before retry or repair.

## Boundaries and conditional references

- Never route prompts, credentials, or captured output through another model.
- Never claim provider token or cost savings from byte counts or tokenizer
  estimates alone.
- Cache reuse requires the complete declared-input fingerprint and verified raw
  streams; uncertain dependency coverage means rerun.
- Do not enable global command interception.

Read only the reference needed for the current change:

- Compression, budgets, or interception changes:
  [safety-contract.md](references/safety-contract.md)
- SSH installation or validation: [remote-workplace.md](references/remote-workplace.md)
- Multi-stage remote work or Harness integration:
  [remote-workflow.md](references/remote-workflow.md)
- A/B provider-usage claims:
  [token-savings-validation.md](references/token-savings-validation.md)

Ordinary test output that already fits the tool result budget should run
directly; adding a guard there would add ceremony without reducing context.

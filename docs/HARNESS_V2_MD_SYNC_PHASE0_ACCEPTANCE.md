# Harness V2 Markdown Sync Phase 0 acceptance

Date: 2026-09-26

## Scope

Phase 0 implements deterministic read-only snapshots and a three-way sync
planner for local research Markdown and remote ResearchFlow records. It does
not contain an apply path, invoke a reader/writer model, accept a base without
review, or mutate authoritative Markdown.

The two allowlisted stages remain behind the existing three MCP tools:

- `research_md_snapshot`
- `research_md_sync_plan`

No new MCP tool was added.

## Implemented gates

- endpoint paths are relative and must resolve below the declared workspace;
- UTF-8 and input-size limits fail closed;
- frontmatter fields, heading blocks, explicit managed blocks, nearby markers,
  Markdown links, and wikilinks receive deterministic indexes and hashes;
- identical input produces the same snapshot fingerprint and plan;
- an accepted per-section base is required for change classification;
- owner changes create proposals only, non-owner or concurrent changes
  conflict, and all plan operations are empty in Phase 0;
- required reader evidence may not be silently truncated;
- initial base alignment always returns `approve_initial_alignment` and
  requires Codex/user authorization;
- the Codex decision fingerprint covers execution/scientific status,
  conflicts, authorization, evidence IDs, reader tasks, section outcomes, and
  next action;
- source-byte and estimated-token fields are labelled preflight only;
- a savings claim requires provider-reported paired usage, cache balance, and
  exact decision-fingerprint equivalence.

## Real endpoint dry-run

The local source was read from the research vault. The remote source was
read-only copied from the mapped ResearchFlow record on `EXAMPLE_WORKER_A`; the
remote file was not modified.

| Endpoint | Bytes | Blocks | Links | Source SHA-256 |
| --- | ---: | ---: | ---: | --- |
| local | 129,592 | 70 | 143 | `19d49408a0f93998431f40c6be23c83909ef62642b256c15835e4cfca1d2bca4` |
| remote | 52,694 | 24 | 153 | `9f62e17147d7874f7d65a7f8685350a1c95645ccfc3ed4e33d0a0cbec374b672` |

Both source hashes were unchanged before and after snapshot generation.

The initial real two-ended plan reported:

- `write_enabled: false`;
- `conflict_count: 0`;
- `initial_alignment_sections: [execution-status]`;
- `next_action: approve_initial_alignment`;
- `requires_codex: true`;
- full source bytes: 182,286;
- decision-packet bytes: 1,125;
- `provider_usage_measured: false`;
- `token_savings_claim_allowed: false`;
- decision fingerprint:
  `e6780820c3c2808b1660b31e5a47b2efe2adf41e75a782727920a15dd3775818`.

The byte reduction is not a token-savings result.

## Verification

The focused Harness regression set passed `47 passed`. The verified Token
Guard receipt is:

`.token-guard/runs/20260926T150621Z-77382471/receipt.json`

MCP server check passed with `mcp 2.2.0`.

The complete repository suite reported `1532 passed, 26 failed, 1 skipped`.
The failures are in pre-existing continuation/worktree and Windows long-path
tests outside this change. The full-suite receipt is:

`.token-guard/runs/20260926T150657Z-2036a9e3/receipt.json`

This document does not claim that the complete repository suite is green.

## Remaining convergence gate

Before activating reader-manager output for Codex, run at least three paired,
non-private synthetic cases with the same Codex model and reasoning setting.
Provider-reported new-input plus output tokens must decrease, cache imbalance
must remain within the declared bound, and every critical decision field must
remain identical. Until that measured A/B passes, the system may claim reduced
model-facing bytes but not reduced Codex tokens.

---
name: research-report-writer
description: Draft final research Markdown from an approved compact evidence packet with the fixed Bailian DeepSeek V4.1-Flash profile. Use only when external report drafting is requested; do not use for ordinary Markdown edits, planning, scientific decisions, or report auditing.
---

# Research Report Writer

This is a bounded prose generator, not a research agent. Codex owns scientific
reasoning, experiment authorization, evidence selection, and the final audit.

## Draft one canonical report

1. Create or reuse the smallest evidence packet that contains the approved
   claims, measurements, caveats, and source paths.
2. Check the configured provider without exposing credentials. The profile is
   fixed to Bailian-hosted DeepSeek V4.1-Flash.
3. Validate the packet, then draft once. Use the remote route when evidence and
   output already belong on the SSH workplace; otherwise use the local route.
4. Keep the generated Markdown out of Codex context when an in-place output path
   is available.
5. Audit the canonical report once against the packet. Fix the report or packet
   directly; do not ask a second model to audit it and do not create parallel
   full reports.

The provider credential may enter only the writer process environment. Never
put it in chat, tool arguments, specs, logs, tmux state, reports, or bundles.
Preserve the writer sidecar containing model identity, provider usage, and input
and output hashes.

The writer may phrase an approved interpretation. It may not invent a
measurement, change a scientific gate, authorize another experiment, or update
ResearchFlow outside its existing SHA/concurrency workflow.

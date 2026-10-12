# Remote and reporting boundary

- Initialize the remote workplace through the solution-agent profile before any
  long job. Do not guess the remote workdir or run root.
- Run long GPU stages under the orchestrated lifecycle. Do not keep Codex awake
  with a short polling loop.
- Keep API credentials in the environment or configured environment file. Never
  put them in task Markdown, command arguments, receipts, or handoffs.
- A reporting stage receives a compact validated evidence packet. The fixed
  Bailian-hosted DeepSeek writer produces one canonical Markdown report and one
  structured claims/provenance sidecar. It also produces a publication packet;
  it never edits a managed destination directly.
- Preserve the existing manager boundary. On ResearchFlow, the publication
  packet is input to the existing GET/SHA/PUT/409 reconciliation path. On a
  simple local file manager, it is input to a compare-and-swap promotion.
- Declare external readable sources as `workspace.input_roots`, generated output
  locations as `workspace.artifact_roots`, and additional managed destinations
  as `workspace.authority_roots`. `research/records` and `.research` are protected
  automatically.
- Use a new output directory for each report attempt. The writer is create-only
  and refuses to overwrite an earlier draft, sidecar, metrics file, or packet.
- Codex audits the claim sidecar, exact cited measurements, and final diff.
  Recheck affected claims after a correction or new evidence; reuse checks whose
  inputs are unchanged. Keep one canonical report rather than a second competing
  full report.
- A provider token or cost claim requires provider usage. Character counts,
  receipt byte counts, and tokenizer estimates are preflight diagnostics only.

# Managed Markdown

Use this reference only when the active taskbook declares `managed_markdown` or
the user asks to reconcile a report with ResearchFlow.

A declaration identifies the source draft, destination kind and path or record
ID, authority owner, direction, and whether the whole document or named sections
are in scope. Use exact relative paths rather than globs. Markdown without a
matching declaration is an ordinary artifact: collect and hash it, but do not
publish or synchronize it automatically.

A complete report may use `mode: full_document` only when both endpoints are the
same report artifact. Do not replace a ResearchFlow management record wholesale;
normally update only its declared status, summary, and report-link sections.

For comparison without loading whole documents into model context, use the
allowlisted `research_md_snapshot` and `research_md_sync_plan` stages. Snapshot
each endpoint, collect both, and run the sync plan with the explicit entity
mapping and accepted base when one exists. `base_candidate.json` is a proposal,
not an accepted base. Phase 0 is read-only.

Publication is a data-integrity operation. Apply a reviewed packet through
ResearchFlow GET/SHA/PUT/409 or a local compare-and-swap owner. Stop on an
unknown destination, stale SHA, unaccepted initial alignment, or a real content
conflict. Retrieve only exact decision-critical blocks when resolving a
conflict.

For report generation, declare readable sources as `workspace.input_roots`,
generated outputs as `workspace.artifact_roots`, and managed destinations as
`workspace.authority_roots`. Writers create a draft and publication packet; they
do not directly overwrite authoritative research records.

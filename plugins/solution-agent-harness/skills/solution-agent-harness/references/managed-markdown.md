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

## Existing artifact_apply local-file publication path

Use `mode: publication` on the existing `artifact_apply` tool. This consumes the
existing Writer `publication_packet.json` v1 artifact with a nonempty update
reason and explicit expected destination SHA (null only for a new target); it is not worker-input
snapshot synchronization (`task_md_sync`) or a task-publication registry update.
Ordinary artifact operations retain their materialize/compose/patch interface.

1. Store the Writer packet using `artifact_store`. An ordinary short Markdown
   edit may produce the same v1 packet without a model call; draft-only packets
   never authorize publication. Keep the draft separate from the authority.
2. Plan with `request: {packet_artifact_id, declaration}`. The declaration uses
   schema_version `managed-markdown/v1`, task_id, direction `draft_to_authority`,
   destination_kind `local_file`, exact relative draft_path/destination_path,
   draft_owner/review_owner/publish_owner, scope `full_document` or `sections`,
   sections (unique `managed:<name>` SYNC block IDs), and the explicitly accepted
   destination SHA (null only for a new target). All declaration/packet paths,
   hashes and destination protocols must match. This stage returns draft_ready,
   the existing plan/diff IDs and the declaration artifact ID without a writer lease.
3. Review the actual diff and candidate. With the same plan_id, send
   `request: {review, publish_owner}` and commit=false. Review schema
   `managed-markdown-review/v1` contains plan_id, diff_artifact_id,
   candidate_sha256, declaration_sha256, review_owner, publish_owner,
   decision `approved` and nonempty evidence_refs. These are trusted controller
   attestations, not an independent identity authentication or scientific review.
   The existing ArtifactStore saves the review metadata directly; no shell write
   or second review database is needed. The result contains review_artifact_id.
4. Commit using the same plan_id, commit=true and
   `request: {review_artifact_id, publish_owner}`. The existing exclusive writer
   lease, target scope and accepted-SHA checks apply. If a writer owns the task,
   retain this plan/review and retry explicitly after that owner releases it; do
   not create another task, steal the lease or dispatch a duplicate command.
5. Inspect with the same plan_id, commit=false and no request. The existing plan,
   review and receipt determine draft_ready, reviewed, reviewed_waiting_for_writer,
   verified or conflict. Successful publication includes the original ApplyReceipt
   and an independently read-back target SHA. Repeated commit returns the original
   receipt; later target drift is conflict and never triggers automatic overwrite.

Sections preserve all bytes outside declared SYNC marker bodies. An expired plan,
stale target, different review/candidate/owner, missing declaration, scope escape or
ResearchFlow packet is rejected. Draft and immutable artifacts remain available.
ResearchFlow records still require their own existing GET/SHA/PUT/409 owner; a
local_file receipt is never evidence that a ResearchFlow record was synchronized.
See the [engineering contract](../../../../../docs/engineering/hf002-document-handoff-20261015/README.md)
for compatibility and recovery boundaries.

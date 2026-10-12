# Harness V2 Markdown Sync and Writing Router Design

## Decision

Do not mirror the local vault and remote ResearchFlow trees with rsync, Git
overwrite, or last-writer-wins rules. They are different projections of the
same research entities:

- the local Obsidian/Discourse Graph vault owns discussion, evidence relations,
  local reading notes, canvases, and long-form synthesis;
- remote ResearchFlow owns executable task state, dependencies, run-linked
  result records, edit history, and the workplace-facing management view;
- solution-agent owns execution lifecycle, receipts, artifact hashes, and
  bounded handoffs;
- generated drafts are artifacts until an authority-specific compare-and-swap
  commit accepts them.

The safe automation is an entity-aware synchronization protocol with
field/section ownership and three-way merge, plus a reader-manager model that
maintains the research information layer and a writing router that drafts
bounded document types from compact evidence.

The reader-manager is not merely a report writer. It replaces routine Codex
work spent finding, opening, classifying, cross-linking, summarizing, and
maintaining files across the local vault and remote workplace. It must reduce
Codex management load without taking over scientific judgment.

## Goals

1. Keep local and remote Markdown views current without sending complete vaults
   through a model context.
2. Preserve one authority for each field or section and refuse ambiguous writes.
3. Transfer deltas, hashes, receipts, and selected artifacts rather than full
   histories and large result trees.
4. Delegate repetitive writing while keeping measurements, scientific gates,
   and publication authorization with the controlling Codex task.
5. Make every synchronization and writing action replayable and auditable.
6. Give Codex one compact decision queue instead of requiring it to manage each
   file, index, link, and routine record update.

## Non-goals

- automatic semantic conflict resolution;
- full bidirectional directory replication;
- replacing ResearchFlow, Obsidian, Discourse Graphs, or solution-agent;
- allowing a writer model to authorize experiments or promote conclusions;
- storing provider credentials in a sync bundle, Markdown, receipt, or command.
- asking Codex to review every routine file operation merely because a model
  participated in preparing it.

## Three-layer operating model

### Layer 1: deterministic program filter

Code performs operations that do not need language-model judgment:

- inventory local and remote roots under explicit allowlists;
- parse frontmatter, headings, managed blocks, links, and entity IDs;
- compute file and block hashes;
- detect additions, changes, missing targets, stale bases, and broken links;
- select changed/relevant blocks under a byte/token budget;
- generate three-way diffs and synchronization plans;
- apply approved patches with compare-and-swap, backups, atomic writes, and
  validation;
- retain raw evidence and produce compact receipts.

This layer never decides what a scientific result means.

### Layer 2: reader-manager model

A bounded model reads the program-selected material and maintains the research
information layer. Its responsibilities include:

- read new reports, record deltas, literature notes, handoffs, and conflicts;
- identify which existing research entity and section each item belongs to;
- extract claims, measurements, sources, limitations, unresolved questions,
  and supersession relationships;
- update structured digests, cross-endpoint links, navigation, and proposed
  Markdown patches;
- maintain local/remote projection consistency without loading complete file
  trees into Codex;
- prepare writing drafts and synchronization conflict explanations;
- produce one compact Codex decision packet.

It may organize and propose. It may not approve an experiment, change a
scientific verdict, resolve an ambiguous semantic conflict, or authorize an
external disclosure.

### Layer 3: Codex controller

Codex receives only decision-relevant material:

- scientific claims whose status may change;
- exact measurements and evidence references needed for the decision;
- unresolved semantic conflicts;
- experiment or external-disclosure authorization requests;
- high-risk publication diffs and failed validations.

Codex decides meaning, scope, authorization, and acceptance. It should not need
to remember directory topology, reopen unchanged Markdown, repair routine
links, or manually propagate the same update across interfaces.

## Decision packet

The reader-manager output to Codex should normally be one compact packet:

```json
{
  "entity_id": "TASK-OBJECT-PART-REAL-REFERENCE",
  "changes_observed": [],
  "routine_actions_completed": [],
  "claims_requiring_judgment": [],
  "conflicts_requiring_choice": [],
  "authorizations_required": [],
  "evidence_refs": [],
  "receipts": []
}
```

If all decision arrays are empty, Codex should not be woken merely to receive a
success message.

## Entity map

Create a versioned mapping file such as `config/research_md_sync.yaml`:

```yaml
schema_version: 1
entities:
  TASK-OBJECT-PART-REAL-REFERENCE:
    local:
      path: research-vault/Discussions/Object-Part Real Reference.md
    remote:
      profile: EXAMPLE_WORKER_A_FAN
      record_id: TASK-OBJECT-PART-REAL-REFERENCE
      path: research/records/object-part-real-reference.md
    sections:
      execution_status:
        owner: remote
      artifact_links:
        owner: remote
      research_question:
        owner: local
      literature_synthesis:
        owner: local
      bounded_conclusion:
        owner: shared_reviewed
```

The mapping binds a stable entity ID to two projections. Path similarity must
never be used to infer identity.

## Content model

Parse Markdown into a deterministic intermediate representation:

- normalized frontmatter fields;
- heading tree and stable section IDs;
- explicit managed blocks;
- links and referenced artifact identities;
- body hash and per-block hash;
- source endpoint and authority class.

Where a section is synchronized, add stable markers without changing the
visible Markdown meaning:

```markdown
<!-- SYNC:bounded-conclusion:START -->
...
<!-- SYNC:bounded-conclusion:END -->
```

Existing historical markers can be mapped to stable section IDs during initial
adoption. Unmarked prose remains endpoint-owned and is never implicitly copied.

## Ownership classes

| Class | Behavior |
| --- | --- |
| `local` | Local changes may project remotely; remote edits conflict. |
| `remote` | Remote changes may project locally; local edits conflict. |
| `shared_reviewed` | Both sides may draft; concurrent changes always require review. |
| `derived` | Rebuild deterministically; never merge by hand. |
| `local_only` / `remote_only` | Never transfer content; transfer identity/hash only if needed. |

Recommended default ownership:

- remote: execution status, run links, receipt/artifact references, worker
  progress, completion state;
- local: literature notes, discussion history, Discourse Graph relations,
  canvases, long research synthesis;
- shared reviewed: current research question, bounded conclusion, limitations,
  next decision;
- derived: navigation pages, indexes, exports, UI projections.

## Synchronization protocol

### 1. Export

Each endpoint emits a small snapshot manifest containing entity IDs, section
hashes, current file hash, link inventory, and selected text blocks. Large
artifacts remain in place.

### 2. Plan

Compare `base`, `local`, and `remote` hashes:

- unchanged/unchanged: no operation;
- changed/unchanged: create one directional patch when ownership permits;
- unchanged/changed: create the opposite directional patch;
- changed/changed in disjoint owned sections: create two independent patches;
- changed/changed in the same or shared section: emit a conflict packet;
- missing or changed base: stop and rebuild the plan.

The plan must be deterministic JSON and include every precondition hash.

### 3. Review

Routine one-owner changes may be auto-approved by policy. Scientific
conclusions, status downgrades/upgrades, task authorization, deletion, path
renames, and link target changes require explicit review.

### 4. Apply

- Re-read the destination and verify its expected SHA-256.
- Create a backup before the first write.
- Apply one atomic patch.
- On remote ResearchFlow, prefer GET/SHA/PUT and stop on 409.
- When ResearchFlow is offline, apply only through an approved remote lifecycle
  stage, then run `researchctl.py scan` and `researchctl.py validate`.
- On the local vault, write atomically and validate affected Markdown links and
  entity mappings.

### 5. Receipt

Write a receipt with base/source/destination hashes, applied section IDs,
backup paths, validation results, and unresolved conflicts. Never include full
provider prompts or credentials.

## Conflict packet

A conflict is an artifact, not a reason to overwrite. It should contain:

- entity and section ID;
- common-base hash;
- local and remote hashes;
- compact three-way diff;
- source links and last receipts;
- suggested choices without selecting one automatically.

Store conflict packets in a dedicated inbox. Do not place conflict prose in the
authoritative research record until resolved.

## Transport and scheduling

- Use solution-agent lifecycle transport for remote snapshot/patch artifacts.
- Do not run a permanent bidirectional file watcher across SSH.
- Trigger a sync plan after a stage closes, after a ResearchFlow record commit,
  on an explicit local publish action, and through a low-frequency heartbeat
  that stays quiet when hashes are unchanged.
- A local filesystem watcher may debounce edits and refresh the local snapshot,
  but it must not push a write without a fresh remote hash.
- Never poll with a model loop. Deterministic code observes hashes; Codex wakes
  only for conflicts, validation failures, or review-required transitions.

## Reader-manager profiles

Reader work must also be typed rather than sent through one generic prompt:

| Profile | Purpose | Output |
| --- | --- | --- |
| `change_triage` | Classify new/changed files and map them to entities | routing manifest |
| `source_reader` | Extract claims, values, limitations, and source locations | evidence digest |
| `result_reader` | Compare a result package with task gates | acceptance proposal |
| `cross_endpoint_reconciler` | Compare local and remote projections | patch/conflict proposal |
| `collection_curator` | Maintain navigation, source inventory, and supersession | derived index updates |
| `record_maintainer` | Convert accepted deltas into bounded record patches | publication packet |
| `decision_brief` | Compress unresolved matters for Codex | Codex decision packet |

Each profile receives only deterministic selections and must cite file/block
hashes. Its output is stored on disk and reused while declared inputs are
unchanged.

## Writing router

Replace the single generic report route with explicit document profiles. Each
profile declares its evidence schema, privacy class, output schema, destination
authority, review gate, and provider policy.

Recommended profiles:

| Profile | Purpose | Default destination |
| --- | --- | --- |
| `record_delta` | Update one existing ResearchFlow/local managed section | draft patch + publication packet |
| `stage_handoff` | Close one bounded execution stage | run artifact |
| `result_report` | Full evidence-backed result report | remote `_worker_out` |
| `evidence_digest` | Summarize selected source files and limitations | local Sources or remote result folder |
| `literature_synthesis` | Compare sourced literature against one question | local vault draft |
| `taskbook` | Turn an approved objective into bounded tasks and gates | draft only until authorized |
| `paper_section` | Draft a paper section from approved claims | manuscript draft tree |
| `review_response` | Draft claim-by-claim reviewer response | review artifact tree |
| `bilingual_rewrite` | Translate/rewrite without changing claims | sibling draft |
| `ui_summary` | Short card/index text for Markdown UI | derived projection |

All profiles should produce:

- the Markdown draft or patch;
- a claim/source sidecar;
- writer/provider usage and model identity;
- input and output hashes;
- a validation report;
- a publication packet when a managed destination exists.

## Writer routing policy

1. Deterministic generation first for indexes, link lists, status cards, and
   machine-derived tables.
2. Use a writer model only for synthesis, restructuring, translation, or
   narrative drafting.
3. Keep restricted/private evidence local unless the user has authorized the
   specific external processor and payload class.
4. Send only selected evidence blocks, never the entire vault or living plan.
5. The controlling Codex reviews claim IDs, measurements, limitations, and the
   destination diff. It should not reread the full report when the verified
   review packet is sufficient.
6. A writer may propose phrasing but may not change task authorization,
   scientific status, acceptance gates, or measured values.

## Autonomy levels

Codex management load will not fall if every harmless update needs approval.
Use policy tiers:

| Level | Allowed behavior |
| --- | --- |
| `A0 deterministic` | Auto-apply regenerated indexes, hashes, link inventories, and receipts. |
| `A1 reversible management` | Auto-apply one-owner navigation/status projections with backup and CAS; report only failures. |
| `A2 batched review` | Batch model-proposed summaries, record deltas, and non-scientific reorganizations into one review packet. |
| `A3 explicit decision` | Require Codex/user approval for scientific status, task authorization, deletion, semantic conflict resolution, or external disclosure. |

Model-created content may enter an authoritative document automatically only
when the profile, section ownership, and autonomy policy all permit it. Other
content remains a draft artifact.

## Token-control design

- Maintain a deterministic local index of frontmatter, headings, links,
  relation IDs, hashes, and short endpoint-owned summaries.
- Select evidence by entity ID, relation, heading, and changed hash.
- Reuse summaries only while all declared source hashes match.
- Send changed sections and required linked evidence, not complete Markdown
  histories.
- Keep full writer output on disk; return claim inventory, validation findings,
  diff statistics, usage, and artifact hashes to Codex.
- Retrieve exact excerpts only for disputed claims or conflicts.
- Measure savings with provider usage; byte reduction is only a preflight
  diagnostic.
- Measure Codex management reduction separately: files opened by Codex,
  model-facing Markdown bytes, repeated unchanged sections, decision packets,
  and manual file operations.

## MCP surface

Do not expand the live harness surface in the first implementation. Register
sync and writer operations as allowlisted stages behind the existing tools:

- `stage_execute`: export, plan, validate, draft, or apply an approved plan;
- `stage_status`: compact deterministic progress;
- `stage_collect`: receipts, conflicts, hashes, and review packets.

This preserves the exact three-tool contract.

## Implementation phases

### Phase 0: inventory and dry-run only

- define entity mapping and ownership schema;
- build Markdown parser/normalizer;
- export local and remote manifests;
- detect candidate mappings and conflicts;
- generate plans, never write.

### Phase 1: one-way owned projections

- remote execution status/artifact links to local;
- approved local research question/task intent to remote;
- CAS, backups, validation, and receipts;
- no shared-section auto-merge.

### Phase 2: writing router

- add reader profiles `change_triage`, `source_reader`,
  `cross_endpoint_reconciler`, and `decision_brief`;
- add writer profiles `record_delta`, `stage_handoff`, `result_report`,
  `evidence_digest`, and `ui_summary`;
- retain external-provider approval and evidence minimization;
- verify output/claim hashes and destination patches.

### Phase 3: reviewed two-way merge

- three-way merge for `shared_reviewed` sections;
- conflict inbox and review UI artifacts;
- optional quiet heartbeat for changed hashes.

### Phase 4: broader authoring

- paper sections, review responses, bilingual rewrites, and literature
  synthesis;
- controlled A/B measurements for quality, correctness, and token usage.

## Acceptance gates

- Zero overwrite on stale destination hashes.
- Identical dry-run plan for identical inputs.
- Every applied write has a backup and receipt.
- Concurrent same-section edits always conflict.
- No local-only canvas/relations or remote-only large artifacts are copied by
  default.
- ResearchFlow GET/SHA/PUT/409 and local compare-and-swap remain authoritative.
- All changed links and ResearchFlow structure validate after apply.
- Writer claims resolve to declared evidence; unknown claims fail closed.
- No credential, prompt body, or unapproved private payload enters sync state.
- Token savings are reported only from provider usage with equivalent outcomes.
- A token-saving candidate passes only when paired runs use the same Codex
  model/reasoning configuration, provider-reported new-input plus output tokens
  are lower, and cache imbalance stays within the declared bound. Byte counts
  and tokenizer estimates are preflight diagnostics and can never satisfy this
  gate.
- Outcome equivalence is not prose similarity. The baseline and candidate must
  produce the same decision fingerprint over conflict IDs, required
  authorizations, evidence IDs, scientific/execution status, and next action.
  Any missing critical evidence or changed decision fails the candidate even
  when it uses fewer tokens.
- Phase 0 deliberately emits `provider_usage_measured: false` and
  `token_savings_claim_allowed: false`. A later controlled A/B may use only
  non-private synthetic fixtures until an evidence class is explicitly
  authorized for an external model.
- Codex receives judgment work rather than a displaced file-management queue;
  representative replay must reduce Codex-opened files and model-facing
  Markdown while preserving the same scientific decision.

## Decisions needed before write-capable implementation

1. Confirm the initial entity list; start with one main record and at most two
   branch notes.
2. Confirm ownership of `research_question`, `bounded_conclusion`, and
   `next_decision`.
3. Choose whether external Bailian may receive each writing profile's evidence
   class, or require a local-only route.
4. Decide which one-owner changes may auto-apply and which always require user
   review.
5. Decide whether local Obsidian receives remote result snapshots or only
   links/manifests to separately packaged artifacts.

# Harness v2 management compatibility

The harness is an execution and evidence boundary.  It does not replace the
remote workplace manager, ResearchFlow, or a local file owner.

The same compact execution/evidence contract runs on both sides. The local side
controls stage selection and scientific gates; the remote thin runtime captures
raw evidence, compacts status, hashes artifacts, and drafts reports where the
data already lives. See `HARNESS_V2_TWO_ENDED_TOKEN_CONTRACT.md`.

## Authority model

| Concern | Authority | Harness responsibility |
| --- | --- | --- |
| Remote task lifecycle | Existing solution-agent / remote workplace profile | Launch one allowlisted stage and return compact status |
| Research records | Existing ResearchFlow manager | Produce a draft and a SHA-bound publication packet only |
| Simple local Markdown | Existing local file owner | Produce a draft and a compare-and-swap publication packet only |
| Generated text | Bailian DeepSeek writer | Write a new artifact; never update the managed destination |
| Evidence | Collector and Token Guard receipts | Hash artifacts at collect time and re-verify them on every later collect |

`research/records` and `.research` below the workspace root are protected
automatically.  Extra managed paths can be declared in
`workspace.authority_roots`.

## Path contract

An execution spec can separate three path classes:

```yaml
workspace:
  root: /srv/example-workspaces/fan-workspace
  cwd: /srv/example-workspaces/fan-workspace
  input_roots:
    - /srv/example-workspaces/fan-workspace/research
  artifact_roots:
    - /srv/example-workspaces/fan-workspace/_worker_out
  authority_roots:
    - /srv/example-workspaces/fan-workspace/research/records
```

- `input_roots` are read-only sources for registry arguments marked as inputs.
- `artifact_roots` are allowed draft/result locations.
- `authority_roots` cannot be the report writer's output location.

The remote paths above are illustrative.  Use the initialized workplace
profile as the source of truth; do not guess or replace its configured root.

## Taskbook-declared source resolution and acquisition

When a user instruction, taskbook, lock, or execution spec supplies a GitHub
repository or release URL as a required input, the operator owns routine source
resolution. It must not ask the user to restate information already present in
the active materials.

Before any download, inspect the expected destination and established project
source roots. An existing checkout is reused without mutation when its origin,
resolved commit, required paths, and dirty state are compatible with the task.
Do not create a duplicate just to obtain a clean checkout. Never update, reset,
clean, or overwrite a dirty checkout; use it read-only only when the requested
version and files remain verifiable and the stage permits it.

Only an absent source proceeds to acquisition. Resolve the official public
source, requested ref or release, required paths, submodules, Git LFS use, and
the project's established destination. Acquire only into an absent or empty
destination through an existing project workflow or allowlisted stage. The
receipt records `reused` or `acquired`, the original URL, resolved commit or
release, destination, required-path verification, and relevant hashes.

Continue without unsolicited system feedback when resolution succeeds. Human
assistance is reported only after automatic checks prove a real blocker:
authentication, gated access or license acceptance, conflicting or ambiguous
source/ref/destination, an unsafe or incompatible existing checkout, unavailable
required assets, insufficient storage, or a missing allowlisted capability. A
repository URL authorizes public metadata inspection and ordinary code retrieval
for the declared task; it does not authorize model weights, datasets, execution,
dependency installation, or scope expansion. Credentials must remain in the
existing credential mechanism and may not appear in taskbooks, commands, logs,
receipts, or Markdown.

## Report writer handoff

Markdown publication is opt-in per taskbook. Merely producing a `.md` file does
not make it a synchronization target. A taskbook that intends to publish a full
research report or selected managed sections must declare a `managed_markdown`
entry with exact source and destination paths, authority, direction, and mode.
No declaration means that the Markdown remains a hashed artifact only.

A minimal declaration can be written in taskbook YAML or in an equivalent
machine-readable taskbook block:

```yaml
managed_markdown:
  - id: final-research-report
    source: _worker_out/report-draft/report.md
    destination_kind: local_file
    destination_path: results/example/REPORT.md
    authority: remote
    direction: draft_to_authority
    mode: full_document
    approval: required
```

The declaration limits scope; it is not permission to write. The writer still
produces a fresh draft and publication packet, collection still verifies hashes,
and the authority owner still performs the SHA-bound promotion. Globs and
directory-wide Markdown synchronization are not allowed. Use `full_document`
only for two endpoints representing the same report. A separate ResearchFlow
management record should normally receive only its declared status, summary,
and report-link sections rather than the report replacing the record wholesale.

Use a fresh output directory for every attempt.  The writer refuses existing
outputs and creates:

- one Markdown draft;
- one `.writer.json` claim/provenance sidecar;
- `report_writer_metrics.json`;
- `publication_packet.json`.

For ResearchFlow, pass:

```yaml
command:
  script_id: research_report_writer
  args:
    evidence_path: /path/to/compact-evidence.json
    output_dir: /path/to/_worker_out/report-draft-001
    destination_kind: researchflow
    destination_path: /path/to/research/records/example.md
    destination_record_id: example
    expected_destination_sha256: <sha256 from the current ResearchFlow GET>
    update_reason: adopt audited report-writer draft
```

The writer does not perform the update.  After `stage_collect` verifies the
draft, the existing ResearchFlow manager must GET the current Markdown and SHA,
compare the packet SHA, PUT the new content with the reason, reconcile any 409,
and retain its normal history/scan behaviour.

For a simple local file, use `destination_kind: local_file`.  The local owner
must compare the target with `expected_destination_sha256` before an atomic
promotion.  A mismatch is a stop condition, not permission to overwrite.

## Remote deployment invariant

Install the overlay into the existing solution-agent checkout.  Keep the
existing remote profile, run root, SSH alias, tmux policy, GPU policy, and
ResearchFlow files unchanged.  The plugin exposes only `stage_execute`,
`stage_status`, and `stage_collect`; it adds no competing scheduler or file
database.

API credentials remain environment-only.  Neither the execution spec,
publication packet, receipts, nor Markdown should contain the API key.

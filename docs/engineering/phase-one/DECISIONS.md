# P1 implementation decisions — 2026-10-12

Historical implementation decisions; current S1–S5 engineering verification and scope are recorded in [DELIVERY.md](DELIVERY.md). No live activation is included.
Baseline: bf8db034ee0e72967296b916968dfe2460e35cbe.

The compact layout retains Python solution_agent/, first-party plugins/, lifecycle + its actual goal integration, selected XATS upstream, and tools/research-background/. Only the real two-consumer provider becomes packages/report-provider/. This avoids mechanical migration of legacy prototypes and preserves all public names. No state format changes.

| Authority before | Authority after | Consumers / migration |
| --- | --- | --- |
| Writer lib/provider.mjs transport/config + core.mjs status/model constants | packages/report-provider/index.mjs + profiles/report-v1.json | Writer compatibility exports; Background direct default, explicit legacy module override remains |
| Writer draft prompt and DraftResponseSchema | unchanged Writer owner | Writer MCP and remote adapter |
| Background numeric usage filtering | report-provider numericUsage | Background receipt writer; filtering behavior preserved |
| terminal_commands.cancel / run_execution | same authority, corrected cancellation handshake | terminal_exec cancellation before PID registration |

Provider direction is consumer → shared provider; shared provider never imports Writer or Background. The fixed preset preserves existing model and request defaults; Background still sets 4096 tokens and disables SDK retries. Environment values retain priority over bounded env-file values. Alternative: keeping a configurable installed Writer dependency perpetuates an external installation requirement; copying transport would create two authorities.

Cancellation must persist its request even before child registration, and the execution owner must observe it before spawn and while waiting. Returning from cancel without recording an early request loses the user's cancellation. Existing command IDs, ownership checks, receipts and lease release remain authoritative. No new job is submitted. Rollback means selecting the previous code in a separate deployment directory while retaining original state and execution-release IDs; no DB conversion.

Historical source-map hashes are immutable. New provenance records old hash, new hash and authority changes, rather than rewriting extraction history. Supported deployment targets are Windows x64 client and Linux x64 Worker candidates; Linux-native, clean online dependencies and M1–M4 require real isolated targets before support can be certified. The extraction commit is a source baseline, not an available previously verified deployment package.

Standalone Writer plugin packaging bundles the actual shared Provider and its versioned preset from source, with licenses and source hashes. Generated plugin helpers are outputs, not editable authorities. Copying just raw Writer source into a plugin cache would not carry the shared package; the existing esbuild toolchain closes that distribution boundary. No new plugin bus or provider wrapper runtime is introduced.

Git checkout preserves exact committed bytes with .gitattributes (* -text). This is necessary for cross-platform release hashes and immutable extraction provenance; it does not renormalize historical source. Fresh and managed installers are delivered under their actual CLI names (install_bootstrap_release.py and install_overlay.py) and their help/dry-run surface is tested.

## Workplace acceptance follow-up

The nine legacy failures are missing synthetic test inputs after extraction, rather than justification to ship private research policies or add runtime defaults. The remote runner suite owns its mock conda profiles and bootstrap files in a temporary workspace; the three-agent staging suite owns an explicit bounded-probe policy. Original behavior assertions and fail-closed missing-policy behavior stay intact. This repairs reproducibility, not production configuration provisioning.

The source-only client archive also needs the generated release manifest consumed by the existing remote launcher. Its authority is the same Worker builder manifest, not a second authored identity. Add it only to the generated client ZIP, verify its bytes, and check a real Windows-to-Linux stdio connection. No installed plugin or live binding is replaced. A same-host isolated environment supplies useful matrix evidence, but does not prove a different physical Windows device. The original old release hashes its private deployment profile and requires its original absolute runtime root; isolated old-package activation must be investigated without changing those bytes or claiming a relocated package has the original identity.

## Acceptance-only path-preserving isolation

Use Linux namespaces and a delegated private systemd manager to activate the immutable original Harness package at its original paths. This is an explicit acceptance helper, not a new deployment transport or relaxed identity. Existing builders, exact-hash overlay plans, paired Client manifests, protocol/workplace checks and original IDs remain authoritative. Source stays read-only during execution; private mounts hold synthetic state. Real remote old/candidate/old and backward command reads passed; wider P1 coverage remains separate.

## Final delivery classification

The completed S1–S5 review classifies the declared target/baseline scope as engineering verified. The candidate builder may expose this classification only for a clean producer, an explicit completed five-batch review, all four matrix rows and exact matching current/runtime source identities. Dirty, incomplete or mismatched builds keep the unverified candidate status. This changes delivery metadata only; protocol 0.6, MCP schemas, runtime source hash and state formats are unchanged. Component receipts alone do not certify the whole delivery, and live/paid/message exclusions remain explicit.

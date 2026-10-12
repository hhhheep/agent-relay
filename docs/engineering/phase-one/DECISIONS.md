# P1 implementation decisions — 2026-10-12

Status: engineering in progress; no live activation, no P1 Verified claim.
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

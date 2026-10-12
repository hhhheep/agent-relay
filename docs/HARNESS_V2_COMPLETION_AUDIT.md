# Harness V2 Completion Audit

This audit is requirement based. A passing component test is not used as
evidence for a broader requirement that it does not exercise.

| Objective requirement | Status | Authoritative evidence |
| --- | --- | --- |
| Preserve the existing solution-agent lifecycle for local and Example 91 work | Proven | `config/remote_execution_profiles.yaml` contains the `EXAMPLE_WORKER_A`, `EXAMPLE_WORKER_A_GenD`, and `EXAMPLE_WORKER_A_B14` profiles. `docs/HARNESS_V2_ARCHITECTURE.md` keeps the registry as the single lifecycle authority. |
| Integrate Research Token Guard without hiding raw evidence | Proven | `solution_agent/harness/receipt_adapter.py`, execution-spec receipt requirements, collector fail-closed verification, raw stream hashes, and receipt tests. |
| Integrate Bailian-hosted DeepSeek report drafting | Proven | Fixed `deepseek-v4-flash-0731` registry stage, environment-only credentials, one canonical report plus claims sidecar, usage persistence, and report-writer contract tests. |
| Preserve existing local and ResearchFlow file authority | Proven | Input, artifact, and authority roots are distinct; `research/records` and `.research` are protected automatically; the writer is create-only and emits a SHA-bound publication packet instead of committing. ResearchFlow keeps its GET/SHA/PUT/409 workflow. |
| Apply token controls on both local and remote sides | Proven in package, remote activation pending | Both sides share execution-spec, receipt, artifact-hash, failure-fingerprint, usage, and publication schemas. The remote thin runtime retains large output where it is produced and returns compact verified state; it does not replace the remote scheduler or ResearchFlow. |
| Portable cached-plugin and provider runtime | Proven locally | The launcher and safe runner propagate checkout-local `SOLUTION_AGENT_ROOT`/`PYTHONPATH`; a cache-path launch test passes. Only the report-writer stage may load allowlisted provider keys from a restricted env file. Provider parsing supports content, structured content, reasoning content, and tool-call arguments with at most one corrective retry and metadata-only failure artifacts. |
| Remote deployment profile separation | Proven locally, remote activation pending | The approved user-scoped core path is `/home/agent-user/.local/opt/solution-agent`; the project and run roots remain separate on `/ssd2`. The validator rejects placeholders, historical run snapshots, root-role aliasing, and initialization without a declared bootstrap source. A complete credential-free bootstrap release and SHA-256 now exist; no remote apply has occurred. |
| Provide a narrow agent harness | Proven | The plugin exposes exactly `stage_execute`, `stage_status`, and `stage_collect`; plugin, Skill, MCP startup, and in-process MCP tests pass. |
| Single state/evidence contract | Proven | `solution_agent/harness/contracts.py`, registry events, receipt references, artifact hashes, terminal gate, and bounded handoff contract. |
| Real token/cost budget | Proven | `solution_agent/harness/usage.py` normalizes provider-reported input/cache/output/cost fields; `budget.py` fails closed when required measurements are missing or limits are exceeded. |
| Bounded context without dropping required evidence | Proven | `context_budget.py` raises on required overflow and records optional truncation/omission; the context gateway emits a selection manifest. |
| Selectively absorb current GitHub designs | Proven | `docs/UPSTREAM_DESIGN_SOURCES.md` records the adopted boundaries from Inspect AI, Aider repo map, SWE-agent ACI, MCP Python SDK, and OpenInference, and the deliberately rejected behaviors. |
| No correctness/evidence trade for token savings | Proven | Three paired synthetic `gpt-6-luna` cases and three paired real Bailian `deepseek-v4.1-flash` research cases have equivalent outcomes, verified evidence, cache-balanced provider usage, and no violations. The gate rejects changed outcomes, unverified evidence, provider/model mismatch, cache imbalance, or non-positive savings. |
| Automated regression verification | Proven | Final guarded suite: 1524 passed, 25 failed, 1 skipped, 5 subtests passed. The 25 failure identities equal the verified pre-change baseline; added regressions are zero. Verified receipt: `.token-guard/runs/20260926T123716Z-0c3d3fbd/receipt.json`, stdout SHA-256 `3c1a55c2eb79de400d5d52f0cc26a96d6213ac2f671d4530a981428f2dbbfebc`. The local-only Bailian A/B safety tests additionally pass 3/3. |
| Local and remote-new-task deployment package | Proven, not activated | The 64-file overlay has per-file base/overlay hashes, backups, atomic writes, no deletion, and conflict refusal. Its final dry-run against the dirty source checkout reports zero conflicts, zero unsafe actions, and zero applied files. Remote rollout remains a separate reviewed operation. |
| Empty-root bootstrap release | Proven locally, not transferred | `solution-agent-harness-bootstrap-v0.3.0.zip` contains 204 manifest-hashed files and no env, credential, runtime log, receipt, cache, or research-data paths. The installer refuses hash mismatch and non-empty targets, has no force mode, and passed dry-run plus initial-install tests. |
| Real research-task A/B acceptance | Proven | Fixed three-case Bailian preflight: `benchmarks/real_task_ab/preflight_manifest_bailian.json`, SHA-256 `fed91a4ef93432e35d251e531038ecd5fa7585f72aba58fd129731e9bed28d36`. Live result: `benchmarks/real_task_ab/runs_bailian/20260926T100805Z/real_task_ab_bailian_result.json`, SHA-256 `247c2a96e0a771b374a7b0ff6f5e5ef2fa62f29c42b812d9212bfe7097e06bab`. All 3 pairs matched expected outcomes; 38,834 -> 10,040 provider-reported input-plus-output tokens (74.146367% reduction), zero cached-input imbalance, zero violations, and no prompt bodies persisted. |

## Completion decision

All build-and-package requirements in this goal are complete and verified,
including a production-provider real-task A/B. Remote activation is
intentionally not performed in this task because the user requested deployment
from a new page; the deployment bundle, checks, and exact next-task prompt are
the required handoff artifacts.
